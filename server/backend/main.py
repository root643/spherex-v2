"""Servidor traductor SPHEREx estilo WiseView (FastAPI).

Convierte recortes FITS de NASA/IPAC IRSA en fotogramas WebP reproyectados a una
rejilla TAN comun, con modo diferencia, coadicion y espectro de 102 canales.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
from astropy.io import fits
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response

import catalogs
import forum
import imaging
import targets
import upstream
import zarr_store
from cache import Cache, pack, unpack

VERSION = "2.0.0"
MAX_PER_FRAME = int(os.environ.get("SPHEREX_MAX_PER_FRAME", "16"))
MAX_PER_COADD = int(os.environ.get("SPHEREX_MAX_PER_COADD", "48"))
MAX_SPEC_PER_DET = int(os.environ.get("SPHEREX_MAX_SPEC_PER_DET", "24"))
TTL_OBS = 24 * 86400      # los metadatos de IRSA solo cambian cuando publica datos nuevos
TTL_ARR = 14 * 86400

app = FastAPI(title="SPHEREx WiseView Translation Server", version=VERSION,
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
cache = Cache()
# Pool dedicado exclusivamente a peticiones prioritarias del usuario (foreground)
pool = ThreadPoolExecutor(max_workers=int(os.environ.get("SPHEREX_FETCH_THREADS", "16")),
                          thread_name_prefix="fg_worker")
# Pool aislado de baja concurrencia para precalentamiento especulativo en segundo plano.
# 1 solo hilo para no competir nunca con el visor ni saturar los 4 nucleos del servidor.
prewarm_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("SPHEREX_PREWARM_THREADS", "1")),
                                  thread_name_prefix="prewarm_worker")

RA = Query(..., ge=0, le=360, description="Ascension recta J2000 (grados)")
DEC = Query(..., ge=-90, le=90, description="Declinacion J2000 (grados)")
FOV = Query(300, ge=60, le=1800, description="Campo de vision (segundos de arco)")


def _r(ra: float, dec: float) -> tuple[float, float]:
    ra = ra % 360.0
    return round(ra, 5), round(dec, 5)


def _hash(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()


# --------------------------------------------------------------------------- observaciones
def get_exposures(ra: float, dec: float) -> list[dict]:
    """Exposures que cubren la posicion, con su indice de epoca. Cache en Redis 24 h
    y copia en disco: si IRSA TAP cae (o se reinicia Redis), se sirve la copia aunque
    este expirada para no dejar el visor sin datos."""
    key = f"obs:{ra:.5f}:{dec:.5f}"
    raw = cache.get(key)
    if raw:
        return json.loads(raw)
    h = hashlib.sha1(f"{ra:.5f}:{dec:.5f}".encode()).hexdigest()
    path = os.path.join(upstream.FITS_DIR, "obs", h[:2], h + ".json")
    try:
        exps = upstream.list_exposures(ra, dec)
    except Exception as e:
        # sin TAP: se sirve la copia de disco aunque este caducada
        try:
            with open(path, "rb") as f:
                return json.loads(f.read())
        except OSError:
            pass
        raise HTTPException(502, f"No se pudo consultar IRSA TAP: {e}")
    labels = imaging.group_epochs([e["mjd"] for e in exps])
    for e, lab in zip(exps, labels):
        e["epoch"] = lab
    data = json.dumps(exps).encode()
    cache.set(key, data, TTL_OBS)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        pass
    return exps


def summarize_epochs(exps: list[dict]) -> list[dict]:
    out: dict[int, dict] = {}
    for e in exps:
        ep = out.setdefault(e["epoch"], {"epoch": e["epoch"], "mjd_start": e["mjd"], "mjd_end": e["mjd"],
                                         "n_exposures": 0, "detectors": {}})
        ep["mjd_end"] = e["mjd"]
        ep["n_exposures"] += 1
        d = f"D{e['detector']}"
        ep["detectors"][d] = ep["detectors"].get(d, 0) + 1
    for ep in out.values():
        ep["mjd_mid"] = round((ep["mjd_start"] + ep["mjd_end"]) / 2, 3)
        ep["date"] = imaging.mjd_to_iso(ep["mjd_mid"])
        ep["date_start"] = imaging.mjd_to_iso(ep["mjd_start"])
        ep["date_end"] = imaging.mjd_to_iso(ep["mjd_end"])
    return [out[k] for k in sorted(out)]


# --------------------------------------------------------------------------- construccion de matrices
def build_array(ra: float, dec: float, fov: float, det: int, lmin: float | None, lmax: float | None,
                exps: list[dict], method: str, cap: int, res: str = "high",
                executor: ThreadPoolExecutor | None = None) -> tuple[np.ndarray, dict]:
    """Descarga, limpia, reproyecta y apila los exposures dados. Cache en Redis."""
    exps = [e for e in exps if e["detector"] == det]
    exps = imaging.sample_evenly(exps, cap if lmin is None else max(cap, 40))
    if not exps:
        raise HTTPException(404, f"No hay imagenes de SPHEREx en D{det} para esa posicion/epoca")
    if res == "low":
        n, scale = upstream.grid_for(fov, min_scale=upstream.NATIVE_SCALE)
    else:
        n, scale = upstream.grid_for(fov)
    key = "arr:" + _hash(ra, dec, fov, det, lmin, lmax, method, n, ",".join(e["obs"] for e in exps))
    raw, meta_raw = cache.get(key), cache.get(key + ":m")
    if raw and meta_raw:
        return unpack(raw), json.loads(meta_raw)

    tw = upstream.target_wcs(ra, dec, n, scale)
    # margen 1.5x: el angulo de giro del satelite cambia entre epocas
    size_deg = (fov * 1.5 + 4 * upstream.NATIVE_SCALE) / 3600.0

    def one(e: dict):
        exp = upstream.load_cutout(e["uri"], ra, dec, size_deg, need_var=False)
        wl = exp["wavelength"]
        if lmin is not None and (wl is None or not lmin <= wl < lmax):
            return None
        arr = upstream.reproject_to(exp, tw, n)
        return (arr, wl, e["mjd"]) if np.isfinite(arr).any() else None

    active_pool = executor if executor is not None else pool
    frames, wls, mjds, errors = [], [], [], []
    for fut in [active_pool.submit(one, e) for e in exps]:
        try:
            r = fut.result()
        except Exception as ex:  # un exposure roto no tumba el fotograma
            errors.append(str(ex))
            continue
        if r is not None:
            frames.append(r[0])
            if r[1] is not None:
                wls.append(r[1])
            mjds.append(r[2])
    if not frames:
        if errors:
            raise HTTPException(502, f"No llegaron recortes validos de S3 ni de IRSA ({errors[0]})")
        raise HTTPException(404, "Sin datos validos en esa banda/canal para esa epoca")

    arr = imaging.stack(frames, method)
    meta = {"n": len(frames), "size": n, "scale": round(scale, 4), "res": res,
            "wavelength": round(float(np.mean(wls)), 4) if wls else None,
            "mjd": round(float(np.mean(mjds)), 3), "date": imaging.mjd_to_iso(float(np.mean(mjds))),
            "failed": len(errors)}
    cache.set(key, pack(arr), TTL_ARR)
    cache.set(key + ":m", json.dumps(meta).encode(), TTL_ARR)
    return arr, meta


def _epoch_exps(exps: list[dict], epoch: int) -> list[dict]:
    sel = [e for e in exps if e["epoch"] == epoch]
    if not sel:
        raise HTTPException(404, f"La epoca {epoch} no existe para esa posicion")
    return sel


def _image_response(arr: np.ndarray, meta: dict, stretch: str, invert: bool, diff: bool,
                    vmin: float | None, vmax: float | None) -> Response:
    if stretch not in imaging.STRETCHES:
        raise HTTPException(422, "stretch debe ser asinh, linear, log o sqrt")
    if vmin is None or vmax is None:
        vmin, vmax = imaging.auto_limits(arr, diff)
    body = imaging.render_webp(arr, vmin, vmax, stretch, invert, diff)
    headers = {
        "Cache-Control": "public, max-age=43200",
        "X-Spherex-Vmin": f"{vmin:.6g}", "X-Spherex-Vmax": f"{vmax:.6g}",
        "X-Spherex-N": str(meta["n"]), "X-Spherex-Date": str(meta["date"]),
        "X-Spherex-Mjd": str(meta["mjd"]), "X-Spherex-Scale": str(meta["scale"]),
        "X-Spherex-Size": str(meta["size"]), "X-Spherex-Res": meta.get("res", "high"),
        "X-Spherex-Wavelength": "" if meta["wavelength"] is None else str(meta["wavelength"]),
    }
    return Response(body, media_type="image/webp", headers=headers)


def _band(band: str) -> tuple[int, float | None, float | None]:
    try:
        return imaging.parse_band(band)
    except ValueError as e:
        raise HTTPException(422, str(e))


# --------------------------------------------------------------------------- endpoints
@app.get("/api/v1/health")
def health():
    return JSONResponse({"status": "ok", "version": VERSION, "redis": cache.redis_ok},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/v1/targets/search")
def targets_search(q: str = Query(..., min_length=1, max_length=120)):
    results = targets.search_local(q)
    if not results:
        c = targets.parse_coords(q)
        if c:
            results = [c]
    if not results:
        key = "ses:" + _hash(q.strip().lower())
        raw = cache.get(key)
        if raw:
            results = json.loads(raw)
        else:
            try:
                s = targets.search_sesame(q)
            except Exception as e:
                raise HTTPException(502, f"CDS Sesame no responde: {e}")
            results = [s] if s else []
            cache.set(key, json.dumps(results).encode(), 30 * 86400 if results else 3600)
    return JSONResponse({"query": q, "results": results}, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/v1/targets/random")
def targets_random(mode: str = Query("auto", pattern="^(auto|candidate|celestial)$")):
    """Genera coordenadas aleatorias: de alto interés científico o de la esfera celeste."""
    target = targets.random_target(mode)
    return JSONResponse(target, headers={"Cache-Control": "no-store"})


@app.get("/api/v1/catalogs/gaia")
def catalog_gaia(ra: float = RA, dec: float = DEC,
                 radius: float = Query(180.0, ge=10.0, le=1800.0, description="Radio de búsqueda en segundos de arco"),
                 limit: int = Query(150, ge=1, le=300, description="Máximo de fuentes")):
    """Estrellas de Gaia DR3 en el campo de visión con magnitudes y vectores de movimiento propio."""
    ra, dec = _r(ra, dec)
    radius = round(radius, 1)
    key = f"cat:gaia:{ra:.5f}:{dec:.5f}:{radius:.1f}:{limit}"
    raw = cache.get(key)
    if raw:
        return Response(raw, media_type="application/json",
                        headers={"Cache-Control": "public, max-age=86400", "X-Catalog-Source": "cache"})
    try:
        sources = catalogs.query_gaia(ra, dec, radius_arcsec=radius, limit=limit)
    except Exception as e:
        raise HTTPException(502, f"Error consultando catálogo Gaia DR3: {e}")

    payload = json.dumps({
        "ra": ra, "dec": dec, "radius_arcsec": radius, "catalog": "Gaia DR3 (I/355/gaiadr3)",
        "count": len(sources), "sources": sources
    }).encode()
    if sources:
        cache.set(key, payload, 7 * 86400)
    return Response(payload, media_type="application/json",
                    headers={"Cache-Control": "public, max-age=86400", "X-Catalog-Source": "vizier"})


@app.get("/api/v1/catalogs/wise")
def catalog_wise(ra: float = RA, dec: float = DEC,
                 radius: float = Query(180.0, ge=10.0, le=1800.0, description="Radio de búsqueda en segundos de arco"),
                 limit: int = Query(150, ge=1, le=300, description="Máximo de fuentes")):
    """Detecciones infrarrojas de AllWISE en el campo de visión (W1 3.4 µm, W2 4.6 µm, W3, W4)."""
    ra, dec = _r(ra, dec)
    radius = round(radius, 1)
    key = f"cat:wise:{ra:.5f}:{dec:.5f}:{radius:.1f}:{limit}"
    raw = cache.get(key)
    if raw:
        return Response(raw, media_type="application/json",
                        headers={"Cache-Control": "public, max-age=86400", "X-Catalog-Source": "cache"})
    try:
        sources = catalogs.query_wise(ra, dec, radius_arcsec=radius, limit=limit)
    except Exception as e:
        raise HTTPException(502, f"Error consultando catálogo AllWISE: {e}")

    payload = json.dumps({
        "ra": ra, "dec": dec, "radius_arcsec": radius, "catalog": "AllWISE (II/328/allwise)",
        "count": len(sources), "sources": sources
    }).encode()
    if sources:
        cache.set(key, payload, 7 * 86400)
    return Response(payload, media_type="application/json",
                    headers={"Cache-Control": "public, max-age=86400", "X-Catalog-Source": "vizier"})


@app.get("/api/v1/observations")
def observations(ra: float = RA, dec: float = DEC):
    ra, dec = _r(ra, dec)
    exps = get_exposures(ra, dec)
    dets: dict[str, int] = {}
    for e in exps:
        dets[f"D{e['detector']}"] = dets.get(f"D{e['detector']}", 0) + 1
    return JSONResponse({"ra": ra, "dec": dec, "n_exposures": len(exps), "detectors": dets,
                         "epochs": summarize_epochs(exps)},
                        headers={"Cache-Control": "public, max-age=1800"})


@app.get("/api/v1/cutout/frame")
def cutout_frame(ra: float = RA, dec: float = DEC, fov: float = FOV,
                 epoch: int = Query(0, ge=0), band: str = Query("D4"),
                 stretch: str = Query("asinh"), invert: bool = False,
                 diff: bool = False, ref: int = Query(0, ge=0),
                 res: str = Query("high", pattern="^(low|high)$"),
                 vmin: Optional[float] = None, vmax: Optional[float] = None):
    """Fotograma de una epoca (o diferencia epoca - ref), reproyectado y estirado, en WebP.
    res=low saca la version de baja resolucion (rejilla nativa) para vista rapida."""
    ra, dec = _r(ra, dec)
    fov = round(float(fov), 2)
    det, lmin, lmax = _band(band)
    exps = get_exposures(ra, dec)
    arr, meta = build_array(ra, dec, fov, det, lmin, lmax, _epoch_exps(exps, epoch), "median",
                            MAX_PER_FRAME, res)
    if diff:
        ref_arr, _ = build_array(ra, dec, fov, det, lmin, lmax, _epoch_exps(exps, ref), "median",
                                 MAX_PER_FRAME, res)
        arr = arr - ref_arr
    return _image_response(arr, meta, stretch, invert, diff, vmin, vmax)


@app.get("/api/v1/cutout/coadd")
def cutout_coadd(ra: float = RA, dec: float = DEC, fov: float = FOV, band: str = Query("D4"),
                 stretch: str = Query("asinh"), invert: bool = False,
                 method: str = Query("median", pattern="^(median|mean)$"),
                 res: str = Query("high", pattern="^(low|high)$"),
                 vmin: Optional[float] = None, vmax: Optional[float] = None):
    """Coadicion temporal de todas las epocas."""
    ra, dec = _r(ra, dec)
    fov = round(float(fov), 2)
    det, lmin, lmax = _band(band)
    arr, meta = build_array(ra, dec, fov, det, lmin, lmax, get_exposures(ra, dec), method,
                            MAX_PER_COADD, res)
    return _image_response(arr, meta, stretch, invert, False, vmin, vmax)


@app.get("/api/v1/cutout/raw")
def cutout_raw(ra: float = RA, dec: float = DEC, fov: float = FOV,
               epoch: int = Query(0, ge=0), band: str = Query("D4"),
               format: str = Query("zfp", pattern="^(zfp|blosc|raw|fits)$"),
               tolerance: float = Query(1e-4, ge=1e-8, le=1.0),
               coadd: bool = False,
               res: str = Query("high", pattern="^(low|high)$")):
    """Endpoint de streaming cientifico con datos float32 puros y compresion ZFP / Blosc.

    Permite a cuadernos Jupyter, scripts en Python (xarray, astropy) y clientes avanzados
    hacer analisis espectral y ajustes de modelos astrofisicos sin perder rango dinamico.
    """
    ra, dec = _r(ra, dec)
    fov = round(float(fov), 2)
    det, lmin, lmax = _band(band)
    exps = get_exposures(ra, dec)
    target_exps = exps if coadd else _epoch_exps(exps, epoch)
    cap = MAX_PER_COADD if coadd else MAX_PER_FRAME
    arr, meta = build_array(ra, dec, fov, det, lmin, lmax, target_exps, "median", cap, res)

    ny, nx = arr.shape
    scale = meta["scale"]
    wcs_obj = upstream.target_wcs(ra, dec, ny, scale)
    wcs_hdr = wcs_obj.to_header()

    if format == "fits":
        bio = io.BytesIO()
        phdu = fits.PrimaryHDU(arr.astype(np.float32), header=wcs_hdr)
        phdu.writeto(bio)
        body = bio.getvalue()
        headers = {
            "Content-Type": "application/fits",
            "Content-Disposition": f'attachment; filename="spherex_raw_{ra:.4f}_{dec:.4f}_{band}.fits"',
            "X-Spherex-Format": "fits",
            "X-Spherex-Shape": f"{ny},{nx}",
            "X-Spherex-Scale": str(scale),
        }
        return Response(body, media_type="application/fits", headers=headers)

    compressed, codec = zarr_store.compress_float32(arr, method=format, tolerance=tolerance)
    headers = {
        "Content-Type": "application/octet-stream",
        "Cache-Control": "public, max-age=43200",
        "X-Spherex-Codec": codec,
        "X-Spherex-Dtype": "float32",
        "X-Spherex-Shape": f"{ny},{nx}",
        "X-Spherex-Scale": str(scale),
        "X-Spherex-Tolerance": str(tolerance),
        "X-Spherex-Date": str(meta.get("date", "")),
        "X-Spherex-Mjd": str(meta.get("mjd", "")),
        "X-Spherex-Wcs-Crval1": str(wcs_hdr.get("CRVAL1", ra)),
        "X-Spherex-Wcs-Crval2": str(wcs_hdr.get("CRVAL2", dec)),
        "X-Spherex-Wcs-Cdelt1": str(wcs_hdr.get("CDELT1", "")),
        "X-Spherex-Wcs-Cdelt2": str(wcs_hdr.get("CDELT2", "")),
    }
    return Response(compressed, media_type="application/octet-stream", headers=headers)


@app.get("/api/v1/cube/zarr/info")
def zarr_cube_info(ra: float = RA, dec: float = DEC, fov: float = FOV):
    """Estado y metadatos del hipercubo espectral 3D en formato Cloud-Native Zarr."""
    ra, dec = _r(ra, dec)
    fov = round(float(fov), 2)
    path = zarr_store.cube_path(ra, dec, fov)
    exists = os.path.exists(path)
    cid = zarr_store.cube_id(ra, dec, fov)
    res = {
        "cube_id": cid,
        "ra": ra, "dec": dec, "fov": fov,
        "cached": exists,
        "chunk_shape": [17, 64, 64],
        "format": "zarr-v3",
        "compression": "zstd-blosc"
    }
    if exists:
        root = zarr_store.open_zarr_cube(path)
        if root and "data" in root:
            res["shape"] = list(root["data"].shape)
            res["attrs"] = dict(root.attrs)
    return JSONResponse(res, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/v1/cube/zarr/spectrum")
def zarr_cube_spectrum(ra: float = RA, dec: float = DEC, fov: float = FOV,
                       x: int = Query(..., ge=0), y: int = Query(..., ge=0)):
    """Consulta instantanea (<10ms) de los 102 canales sobre el cubo Zarr cacheado."""
    ra, dec = _r(ra, dec)
    fov = round(float(fov), 2)
    path = zarr_store.cube_path(ra, dec, fov)
    spec = zarr_store.extract_spectrum_from_zarr(path, x, y)
    if spec is None:
        raise HTTPException(404, "El cubo Zarr no esta generado o las coordenadas de pixel caen fuera")
    return JSONResponse(spec, headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/v1/cube/zarr/build")
def zarr_cube_build(ra: float = RA, dec: float = DEC, fov: float = FOV,
                    res: str = Query("high", pattern="^(low|high)$")):
    """Genera y almacena el hipercubo espectral 3D en Zarr particionado en bloques (17, 64, 64)."""
    ra, dec = _r(ra, dec)
    fov = round(float(fov), 2)
    path = zarr_store.cube_path(ra, dec, fov)
    cid = zarr_store.cube_id(ra, dec, fov)
    if os.path.exists(path):
        return JSONResponse({"status": "ready", "path": path, "cube_id": cid})

    exps = get_exposures(ra, dec)
    if res == "low":
        n, scale = upstream.grid_for(fov, min_scale=upstream.NATIVE_SCALE)
    else:
        n, scale = upstream.grid_for(fov)
    cube_data = np.zeros((102, n, n), dtype=np.float32)
    wavelengths = []
    wcs_hdr = upstream.target_wcs(ra, dec, n, scale).to_header()
    wcs_info = {k: str(wcs_hdr.get(k, "")) for k in ("CRVAL1", "CRVAL2", "CDELT1", "CDELT2", "CTYPE1", "CTYPE2")}

    for ch in range(1, 103):
        det, lmin, lmax = imaging.channel_range(ch)
        wavelengths.append(round((lmin + lmax) / 2.0, 4))
        try:
            arr, _ = build_array(ra, dec, fov, det, lmin, lmax, exps, "median", cap=8, res=res)
            if arr.shape == (n, n):
                cube_data[ch - 1] = arr
            else:
                min_y, min_x = min(n, arr.shape[0]), min(n, arr.shape[1])
                cube_data[ch - 1, :min_y, :min_x] = arr[:min_y, :min_x]
        except Exception:
            pass

    zarr_store.save_zarr_cube(path, cube_data, ra, dec, fov, wavelengths, wcs_info)
    return JSONResponse({"status": "created", "path": path, "cube_id": cid, "shape": [102, n, n]},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/v1/hips/surveys")
def hips_surveys():
    """Listado de teselados HiPS disponibles para navegacion de todo el firmamento a 60 FPS."""
    surveys = [
        {"id": "CDS/P/allWISE/color", "name": "AllWISE Color (Infrared)", "band": "Infrared (3.4 / 4.6 µm)", "default": True},
        {"id": "CDS/P/2MASS/color", "name": "2MASS JHK (Near-IR)", "band": "Near-IR (1.25 - 2.16 µm)", "default": False},
        {"id": "CDS/P/DSS2/color", "name": "DSS2 Color (Optical)", "band": "Optical", "default": False},
        {"id": "CDS/P/PanSTARRS/DR1/color-z-zg-g", "name": "Pan-STARRS DR1", "band": "Optical/NIR", "default": False},
    ]
    return JSONResponse({"status": "ok", "surveys": surveys}, headers={"Cache-Control": "public, max-age=86400"})


def _sky_frac(ra0: float, dec0: float, ra: float, dec: float, span_arcsec: float) -> tuple[float, float]:
    """Posicion de (ra, dec) en una vista TAN centrada en (ra0, dec0), como fraccion del
    ancho/alto desde la esquina superior izquierda (norte arriba, este a la izquierda)."""
    d2r = np.pi / 180.0
    a, d, d0 = (ra - ra0) * d2r, dec * d2r, dec0 * d2r
    cosc = np.sin(d0) * np.sin(d) + np.cos(d0) * np.cos(d) * np.cos(a)
    xi = np.cos(d) * np.sin(a) / cosc
    eta = (np.cos(d0) * np.sin(d) - np.sin(d0) * np.cos(d) * np.cos(a)) / cosc
    span = span_arcsec / 3600.0 * d2r
    return float(0.5 - xi / span), float(0.5 - eta / span)


def make_gif(ra: float, dec: float, fov: float, band: str, mode: str = "blink", stretch: str = "asinh",
             invert: bool = False, ref: int = 0, speed: int = 500, res: str = "high",
             watermark: bool = True, vmin: float | None = None, vmax: float | None = None,
             scale_to: int | None = None, cross: bool = False,
             marker: tuple[float, float] | None = None, common: bool = True) -> tuple[bytes, int]:
    """GIF de las epocas de una posicion (blink, diferencia o coadicion de un fotograma).
    Recupera los arrays de Redis si ya se vieron en el visor. Devuelve (bytes, n_fotogramas).
    marker: (ra, dec) de un objeto a rodear con un circulo."""
    if stretch not in imaging.STRETCHES:
        raise HTTPException(422, "stretch debe ser asinh, linear, log o sqrt")
    ra, dec = _r(ra, dec)
    fov = round(float(fov), 2)
    det, lmin, lmax = _band(band)
    exps = get_exposures(ra, dec)
    usable_epochs = sorted({e["epoch"] for e in exps if e["detector"] == det})
    if not usable_epochs:
        raise HTTPException(404, f"No hay observaciones de SPHEREx en {band} para esa posicion")
    sub = f'SPHEREx · {band} · {int(fov)}"'

    frames_data: list[tuple[np.ndarray, dict]] = []
    if mode == "coadd":
        arr, meta = build_array(ra, dec, fov, det, lmin, lmax, exps, "median", MAX_PER_COADD, res)
        meta_copy = dict(meta)
        meta_copy["date"] = f"Coadicion · {len(usable_epochs)} epocas"
        meta_copy["label_sub"] = sub
        frames_data.append((arr, meta_copy))
    elif mode == "diff":
        if len(usable_epochs) < 2:
            raise HTTPException(400, "Hacen falta al menos 2 epocas para el modo diferencia")
        if ref not in usable_epochs:
            ref = usable_epochs[0]
        try:
            ref_arr, _ = build_array(ra, dec, fov, det, lmin, lmax, _epoch_exps(exps, ref), "median",
                                     MAX_PER_FRAME, res)
        except Exception as e:
            raise HTTPException(404, f"No se pudo cargar la epoca de referencia {ref}: {e}")
        for ep in usable_epochs:
            if ep == ref:
                continue
            try:
                arr, meta = build_array(ra, dec, fov, det, lmin, lmax, _epoch_exps(exps, ep), "median",
                                        MAX_PER_FRAME, res)
                diff_arr = arr - ref_arr
                meta_copy = dict(meta)
                meta_copy["date"] = f"{meta['date']} - ref"
                meta_copy["label_sub"] = sub
                frames_data.append((diff_arr, meta_copy))
            except Exception:
                continue
    else:
        for ep in usable_epochs:
            try:
                arr, meta = build_array(ra, dec, fov, det, lmin, lmax, _epoch_exps(exps, ep), "median",
                                        MAX_PER_FRAME, res)
                meta_copy = dict(meta)
                meta_copy["label_sub"] = sub
                frames_data.append((arr, meta_copy))
            except Exception:
                continue

    if not frames_data:
        raise HTTPException(404, f"No hay imagenes validas en {band} para generar el GIF animado")

    mfrac = None
    if marker is not None:
        m0 = frames_data[0][1]
        mfrac = _sky_frac(ra, dec, marker[0], marker[1], float(m0["size"]) * float(m0["scale"]))
    body = imaging.render_gif(frames_data, stretch=stretch, invert=invert, diff=(mode == "diff"),
                              duration_ms=speed, watermark=watermark, vmin=vmin, vmax=vmax,
                              scale_to=scale_to, cross=cross, marker=mfrac, common=common)
    return body, len(frames_data)


@app.get("/api/v1/cutout/gif")
def cutout_gif(ra: float = RA, dec: float = DEC, fov: float = FOV, band: str = Query("D4"),
               mode: str = Query("blink", pattern="^(blink|diff|coadd)$"),
               stretch: str = Query("asinh"), invert: bool = False,
               ref: int = Query(0, ge=0), speed: int = Query(500, ge=50, le=2000),
               res: str = Query("high", pattern="^(low|high)$"),
               watermark: bool = Query(True),
               vmin: Optional[float] = None, vmax: Optional[float] = None):
    """Genera al vuelo una animacion GIF de las epocas de esa posicion (blink o diferencia).
    Ultra-rapido: recupera los arrays de Redis y compone el GIF en milisegundos."""
    body, n = make_gif(ra, dec, fov, band, mode, stretch, invert, ref, speed, res, watermark, vmin, vmax)
    ra, dec = _r(ra, dec)
    filename = f"spherex_ra{ra:.4f}_dec{dec:.4f}_{band}_fov{int(round(fov))}.gif"
    headers = {
        "Content-Disposition": f'inline; filename="{filename}"',
        "Content-Type": "image/gif",
        "Cache-Control": "public, max-age=43200",
        "X-Spherex-Frames": str(n),
        "X-Spherex-Fov": str(int(round(fov))),
        "X-Spherex-Band": band,
    }
    return Response(body, media_type="image/gif", headers=headers)


# --------------------------------------------------------------------------- prewarm
_prewarming: set[tuple] = set()
_prewarm_lock = threading.Lock()
MAX_PREWARM_PENDING = 4


def _prewarm_epoch(ra: float, dec: float, fov: float, det: int, lmin: float | None,
                   lmax: float | None, epoch: int, token: tuple) -> None:
    """Construye en segundo plano el array de una epoca usando prewarm_pool (1 hilo).
    Precalienta baja y alta resolucion en Redis sin competir con peticiones interactivas."""
    try:
        exps = get_exposures(ra, dec)
        ep_exps = _epoch_exps(exps, epoch)
        # 1. Baja resolucion para respuesta rapida si el usuario cambia de banda
        build_array(ra, dec, fov, det, lmin, lmax, ep_exps, "median", MAX_PER_FRAME, "low", executor=prewarm_pool)
        # 2. Alta resolucion para nitidez completa
        build_array(ra, dec, fov, det, lmin, lmax, ep_exps, "median", MAX_PER_FRAME, "high", executor=prewarm_pool)
    except Exception:
        pass  # el prewarm es oportunista
    finally:
        with _prewarm_lock:
            _prewarming.discard(token)


@app.get("/api/v1/cutout/prewarm")
def cutout_prewarm(background_tasks: BackgroundTasks, ra: float = RA, dec: float = DEC,
                   fov: float = FOV, band: str = Query("D4")):
    """Encola la construccion de epocas en segundo plano de forma dosificada y no invasiva."""
    ra, dec = _r(ra, dec)
    fov = round(float(fov), 2)

    with _prewarm_lock:
        if len(_prewarming) >= MAX_PREWARM_PENDING:
            return JSONResponse({"status": "busy", "message": "Cola de prewarm saturada"},
                                headers={"Cache-Control": "no-store"})

    exps = get_exposures(ra, dec)
    enqueued = 0

    if band.lower() == "all":
        targets = [(d, None, None) for d in sorted({e["detector"] for e in exps})]
    else:
        det, lmin, lmax = _band(band)
        targets = [(det, lmin, lmax)]

    for det, lmin, lmax in targets:
        det_exps = [e for e in exps if e["detector"] == det]
        for epoch in sorted({e["epoch"] for e in det_exps}):
            token = (ra, dec, fov, det, epoch)
            with _prewarm_lock:
                if token in _prewarming or len(_prewarming) >= MAX_PREWARM_PENDING:
                    continue
                _prewarming.add(token)
            background_tasks.add_task(_prewarm_epoch, ra, dec, fov, det, lmin, lmax, epoch, token)
            enqueued += 1
    return JSONResponse({"status": "ok", "band": band, "epochs": enqueued},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/v1/spectrum")
def spectrum(ra: float = RA, dec: float = DEC):
    """Fotometria de apertura en todos los exposures -> SED de 102 canales (uJy)."""
    return Response(make_spectrum(ra, dec), media_type="application/json",
                    headers={"Cache-Control": "public, max-age=43200"})


def make_spectrum(ra: float, dec: float) -> bytes:
    """Espectro de 102 canales en (ra, dec) como JSON (bytes), con cache en Redis."""
    ra, dec = _r(ra, dec)
    exps = get_exposures(ra, dec)
    if not exps:
        raise HTTPException(404, "SPHEREx aun no tiene imagenes publicas de esa posicion")
    sel: list[dict] = []
    for det in range(1, 7):
        sel += imaging.sample_evenly([e for e in exps if e["detector"] == det], MAX_SPEC_PER_DET)
    key = "sed:" + _hash(ra, dec, ",".join(e["obs"] for e in sel))
    raw = cache.get(key)
    if raw:
        return raw

    size_deg = 24 * upstream.NATIVE_SCALE / 3600.0

    def one(e: dict):
        exp = upstream.load_cutout(e["uri"], ra, dec, size_deg)
        if exp["wavelength"] is None:
            return None
        ph = imaging.aperture_photometry(exp["image"], exp["variance"], exp["x"], exp["y"])
        if ph is None:
            return None
        k = exp["omega"] * 1e12  # MJy/sr por pixel -> uJy
        return {"wavelength": round(exp["wavelength"], 5), "bandwidth": exp["bandwidth"],
                "flux": ph[0] * k, "err": ph[1] * k, "mjd": e["mjd"], "detector": e["detector"]}

    points, failed = [], 0
    for fut in [pool.submit(one, e) for e in sel]:
        try:
            p = fut.result()
        except Exception:
            failed += 1
            continue
        if p is not None and np.isfinite(p["flux"]) and np.isfinite(p["err"]):
            points.append(p)
    if not points:
        raise HTTPException(502 if failed else 404, "No se pudo medir el espectro en esa posicion")
    points.sort(key=lambda p: p["wavelength"])
    body = json.dumps({
        "ra": ra, "dec": dec, "units": "uJy", "aperture_radius_px": 2.0,
        "n_exposures": len(points), "n_failed": failed,
        "points": [{"wavelength": p["wavelength"], "flux_ujy": round(p["flux"], 3),
                    "err_ujy": round(p["err"], 3), "mjd": p["mjd"], "detector": p["detector"]} for p in points],
        "channels": imaging.bin_channels(points),
        "diagnostics": imaging.diagnostics(points),
    }).encode()
    cache.set(key, body, TTL_ARR)
    return body


_disk = {"t": 0.0, "v": {}}


def _disk_stats() -> dict:
    if time.time() - _disk["t"] > 60:
        files = size = 0
        try:
            for sub in os.scandir(upstream.FITS_DIR):
                if sub.is_dir():
                    for f in os.scandir(sub.path):
                        files += 1
                        size += f.stat().st_size
            du = shutil.disk_usage(upstream.FITS_DIR)
            _disk["v"] = {"fits_files": files, "fits_bytes": size, "disk_free_bytes": du.free,
                          "disk_total_bytes": du.total}
        except OSError as e:
            _disk["v"] = {"error": str(e)}
        _disk["t"] = time.time()
    return _disk["v"]


def _report_source(kind: str, detail: str | None = None) -> None:
    cache.incr("stat:src:" + kind)
    if detail and kind != "s3":
        cache.set("stat:src:last:" + kind, detail.encode(), 7 * 86400)


upstream.report = _report_source


def _source_stats() -> dict:
    """De donde han salido los recortes: bucket S3 de AWS o servicio de recortes de IRSA."""
    out: dict = {"mode": upstream.SOURCE, "s3_base": upstream.S3_BASE}
    for kind in ("s3", "aws", "irsa", "s3_missing", "s3_error", "aws_error"):
        out[kind] = int(cache.get("stat:src:" + kind) or 0)
    for kind in ("s3_missing", "s3_error", "aws_error"):
        last = cache.get("stat:src:last:" + kind)
        if last:
            out["last_" + kind] = last.decode("utf-8", "replace")
    return out


@app.get("/api/v1/source/test")
def source_test(ra: float = RA, dec: float = DEC, n: int = Query(3, ge=1, le=8),
                compare: bool = Query(False, description="medir tambien el mismo recorte en IRSA")):
    """Diagnostico: lee n recortes directamente del bucket S3, sin cache de matrices,
    y dice cuanto tarda cada uno o por que falla. Cada recorte se lee dos veces: la
    primera incluye leer las cabeceras del fichero (solo pasa una vez por imagen) y
    la segunda es lo que cuesta despues. Con compare=true mide ademas IRSA."""
    ra, dec = _r(ra, dec)
    exps = imaging.sample_evenly(get_exposures(ra, dec), n)
    size_deg = 0.1
    out = []
    for e in exps:
        item = {"uri": e["uri"], "s3_url": upstream.s3_url(e["uri"])}
        for label in ("seconds", "seconds_again"):
            t0 = time.time()
            try:
                c = upstream.s3_cutout(e["uri"], ra, dec, size_deg)
                item.update(ok=True, shape=list(c["image"].shape), wavelength=c["wavelength"],
                            finite_fraction=round(float(np.isfinite(c["image"]).mean()), 3))
            except Exception as ex:
                item.update(ok=False, error=f"{type(ex).__name__}: {ex}"[:400])
            item[label] = round(time.time() - t0, 3)
        if compare:
            t0 = time.time()
            try:
                r = upstream._session.get(upstream.cutout_url(e["uri"], ra, dec, size_deg), timeout=(10, 90))
                item["irsa_status"], item["irsa_bytes"] = r.status_code, len(r.content)
            except Exception as ex:
                item["irsa_error"] = f"{type(ex).__name__}: {ex}"[:200]
            item["irsa_seconds"] = round(time.time() - t0, 3)
        out.append(item)
    return JSONResponse({"mode": upstream.SOURCE, "results": out}, headers={"Cache-Control": "no-store"})


@app.get("/api/v1/cache/stats")
def cache_stats():
    return JSONResponse({"version": VERSION, "redis": cache.info(), "disk": _disk_stats(),
                         "source": _source_stats()},
                        headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- foro
def _forum_gif(view: dict, obj: tuple[float, float] | None) -> tuple[bytes, int]:
    """El GIF de una publicacion: las mismas opciones que tenia el visor del autor,
    ampliado a 480 px, con la reticula si la tenia y un circulo en el objeto analizado."""
    mode = view["mode"]
    return make_gif(view["ra"], view["dec"], view["fov"], view["band"], mode, view["stretch"],
                    view["invert"], view["ref"], view["speed"], "high", True,
                    view.get("vmin"), view.get("vmax"), scale_to=480, cross=view.get("cross", True),
                    marker=obj, common=view.get("common", True))


forum.init(_forum_gif, make_spectrum)
app.include_router(forum.router)
