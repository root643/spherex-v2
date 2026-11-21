"""Servicio de recortes SPHEREx para la caja AWS (us-east-1, junto al bucket).

Lee los level2 del bucket publico nasa-irsa-spherex por HTTP Range (misma logica
que el modulo upstream del backend de Oracle) y devuelve los arrays ya recortados
y limpios en un .npz (~0.8 MB por fotograma). Oracle hace el resto: reproyeccion,
apilado, coadd, espectro y web.

Endpoint:
  GET /health
  GET /cutout?uri=...&ra=...&dec=...&size_deg=...&need_var=true&token=...

Errores: 503 si el fichero no se puede servir desde S3 (el cliente cae al camino
S3-directo de Oracle), 404 si la posicion no cae en la imagen.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from astropy.wcs.utils import proj_plane_pixel_area, proj_plane_pixel_scales
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

import imaging
import s3fits

FITS_DIR = os.environ.get("SPHEREX_FITS_DIR", "/var/cache/spherex-aws")
NATIVE_SCALE = 6.15  # arcsec/pixel
S3_BASE = os.environ.get("SPHEREX_S3_BASE", "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/")
_S3_PREFIX = "ibe/data/spherex/"
_TOKEN = os.environ.get("SPHEREX_AWS_TOKEN", "")

_session = requests.Session()
_session.headers["User-Agent"] = "spherex-aws-cutout/1.0"
_adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=96, max_retries=2)
_session.mount("https://", _adapter)
_range_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("SPHEREX_S3_THREADS", "48")))


class S3Unavailable(Exception):
    """El fichero no esta en el bucket o no se puede leer por filas."""


def s3_url(uri: str) -> str | None:
    i = uri.find(_S3_PREFIX)
    return None if i < 0 else S3_BASE + uri[i + len(_S3_PREFIX):]


def _s3_get(url: str, a: int, b: int) -> requests.Response:
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = _session.get(url, headers={"Range": f"bytes={a}-{b}"}, timeout=(8, 60))
            if r.status_code == 206:
                return r
            if r.status_code in (403, 404):
                raise S3Unavailable(f"HTTP {r.status_code}: no esta en el bucket")
            last = RuntimeError(f"HTTP {r.status_code} en rango S3 {a}-{b}")
            if r.status_code == 416:
                break
        except requests.RequestException as e:
            last = e
        time.sleep(0.25 * (attempt + 1))
    raise last or RuntimeError("fallo de rango S3")


def _s3_range(url: str, a: int, b: int) -> bytes:
    return _s3_get(url, a, b).content


def _cache_path(kind: str, url: str, ext: str) -> str:
    h = hashlib.sha1(url.encode()).hexdigest()
    return os.path.join(FITS_DIR, kind, h[:2], h + ext)


def _disk_read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            data = f.read()
        os.utime(path, None)
        return data
    except OSError:
        return None


def _disk_write(path: str, data: bytes) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        pass


_layouts: dict[str, dict] = {}
_layout_lock = threading.Lock()
_FIRST_READ = 12 * s3fits.BLOCK  # suele cubrir el HDU primario y la cabecera de IMAGE
_TEMPLATE_KEY = re.compile(r"/(l2b-[^/]+)/.*D([1-6])_spx")   # version del procesado + detector
_templates: dict[str, list] | None = None


def _template_path() -> str:
    return os.path.join(FITS_DIR, "layout", "templates.json")


def _load_templates() -> dict[str, list]:
    global _templates
    if _templates is None:
        try:
            _templates = json.loads(_disk_read(_template_path()) or b"{}")
        except ValueError:
            _templates = {}
    return _templates


def _template_key(url: str) -> str:
    m = _TEMPLATE_KEY.search(url)
    return f"{m.group(1)}-D{m.group(2)}" if m else "default"


def _walk_remote(url: str) -> dict:
    """Localiza los HDU del fichero remoto leyendo solo cabeceras.

    Si ya se conoce la estructura de otra imagen del mismo detector y procesado, las
    cabeceras se leen en 2 tandas en paralelo (y se comprueban); si no, o si no
    cuadra, se recorren una detras de otra (unas 6 lecturas).
    """
    key = _template_key(url)
    tmpl = _load_templates().get(key)
    if tmpl:
        totals: list[int] = []

        def get_many(ranges: list[tuple[int, int]]) -> list[bytes]:
            rs = list(_range_pool.map(lambda ab: _s3_get(url, ab[0], ab[1]), ranges))
            totals.append(int(rs[0].headers["Content-Range"].rsplit("/", 1)[1]))
            return [r.content for r in rs]

        try:
            res = s3fits.walk_fast(get_many, tmpl)
        except S3Unavailable:
            raise
        except Exception:
            res = None
        if res is not None and totals and res[1] == totals[0]:
            return {"total": totals[0], "exts": res[0]}

    first = _s3_get(url, 0, _FIRST_READ - 1)
    head = first.content
    total = int(first.headers["Content-Range"].rsplit("/", 1)[1])

    def get(a: int, b: int) -> bytes:
        return head[a:b + 1] if b < len(head) else _s3_range(url, a, b)

    exts = s3fits.walk(get, total)
    new = s3fits.template_of(exts)
    if new != tmpl:
        with _layout_lock:
            templates = dict(_load_templates())
            templates[key] = new
            globals()["_templates"] = templates
        _disk_write(_template_path(), json.dumps(templates).encode())
    return {"total": total, "exts": exts}


def _layout(url: str) -> dict:
    """Mapa de HDUs del fichero: {"total", "exts": {EXTNAME: {text, hdroff, hsize, dataoff, dsize}}}.

    Solo se calcula la primera vez que se toca cada imagen; despues sale de memoria o de disco.
    """
    lay = _layouts.get(url)
    if lay is None:
        path = _cache_path("layout", url, ".json")
        raw = _disk_read(path)
        if raw:
            try:
                lay = json.loads(raw)
            except ValueError:
                lay = None
        if lay is None:
            lay = _walk_remote(url)
            _disk_write(path, json.dumps(lay).encode())
        with _layout_lock:
            if len(_layouts) > 4096:
                _layouts.clear()
            _layouts[url] = lay
    return lay


def _hdu_file_bytes(url: str, lay: dict, name: str, kind: str) -> bytes | None:
    """Un HDU entero (cabecera + datos) precedido de un primario vacio: un FITS valido
    que astropy puede abrir. Sirve para WCS-WAVE (tabla de longitudes de onda, ~1 kB)
    y para FLAGS comprimido. Queda en disco: solo se baja una vez por imagen."""
    ext = lay["exts"].get(name)
    if ext is None:
        return None
    path = _cache_path(kind, url, ".fits")
    data = _disk_read(path)
    if data is None:
        end = min(ext["dataoff"] + s3fits.padded(ext["dsize"]), lay["total"])
        body = _s3_range(url, ext["hdroff"], end - 1)
        data = s3fits.minimal_primary() + body + b"\0" * (-len(body) % s3fits.BLOCK)
        _disk_write(path, data)
    return data


def s3_cutout(uri: str, ra: float, dec: float, size_deg: float, need_var: bool = True) -> dict:
    """Recorte por rangos del bucket: imagen limpia, varianza, WCS y lambda en (ra, dec).

    Lanza S3Unavailable si ese fichero no se puede servir desde S3.
    """
    url = s3_url(uri)
    if url is None:
        raise S3Unavailable("uri fuera del arbol de SPHEREx")
    lay = _layout(url)
    exts = lay["exts"]
    cards = {k: s3fits.parse_cards(exts[k]["text"]) for k in ("IMAGE", "FLAGS", "ZODI", "VARIANCE") if k in exts}
    if "IMAGE" not in cards:
        raise S3Unavailable("el fichero no tiene HDU IMAGE")
    # Compatibilidad universal con FITS: soporte tanto de imagenes planas (filas contiguas)
    # como comprimidas por teselas (fpack / CompImageHDU) por rangos de bytes directos a S3.
    for k in ("IMAGE", "ZODI", "VARIANCE", "FLAGS"):
        if k in cards and not (s3fits.is_plain_image(cards[k]) or s3fits.is_compressed_image(cards[k])):
            raise S3Unavailable(f"el HDU {k} no tiene un formato de imagen 2D ni fpack soportado")

    img_cards = cards["IMAGE"]
    if s3fits.is_plain_image(img_cards):
        n1, n2 = int(img_cards["NAXIS1"]), int(img_cards["NAXIS2"])
    else:
        n1 = int(img_cards.get("ZNAXIS1", img_cards.get("NAXIS1", 2048)))
        n2 = int(img_cards.get("ZNAXIS2", img_cards.get("NAXIS2", 2048)))

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        warnings.simplefilter("ignore", RuntimeWarning)
        hdr0 = fits.Header.fromstring(exts["IMAGE"]["text"])
        if hdr0.get("ZIMAGE") is True:
            hdr0["XTENSION"] = "IMAGE"
            hdr0["NAXIS"] = int(hdr0.get("ZNAXIS", 2))
            hdr0["NAXIS1"] = n1
            hdr0["NAXIS2"] = n2
            hdr0["BITPIX"] = int(hdr0.get("ZBITPIX", -32))
        wcs0 = WCS(hdr0)
        x, y = wcs0.world_to_pixel(SkyCoord(ra, dec, unit="deg"))
        x, y = float(x), float(y)
        if not (np.isfinite(x) and np.isfinite(y)):
            raise ValueError("la posicion no cae en esta imagen")
        try:
            pix = float(np.mean(proj_plane_pixel_scales(wcs0))) * 3600.0
        except Exception:
            pix = NATIVE_SCALE
        if not 1.0 < pix < 60.0:
            pix = NATIVE_SCALE
        half = int(np.ceil(size_deg * 3600.0 / (2.0 * pix))) + 2
        xi, yi = int(round(x)), int(round(y))
        x0, x1 = max(0, xi - half), min(n1, xi + half + 1)
        y0, y1 = max(0, yi - half), min(n2, yi + half + 1)
        if x1 - x0 < 3 or y1 - y0 < 3:
            raise ValueError("la posicion queda fuera del detector")

        def get(a: int, b: int) -> bytes:
            return _s3_range(url, a, b)

        def _fetch_part(k: str):
            c = cards[k]
            if s3fits.is_plain_image(c):
                return s3fits.rows(get, exts[k], y0, y1, c)[:, x0:x1]
            elif s3fits.is_compressed_image(c):
                try:
                    return s3fits.read_compressed_section(get, exts[k], y0, y1, x0, x1, c)
                except Exception:
                    if k == "FLAGS":
                        fb = _hdu_file_bytes(url, lay, "FLAGS", "flags")
                        if fb is not None:
                            with fits.open(io.BytesIO(fb), memmap=False) as fhdul:
                                return np.array(fhdul[1].section[y0:y1, x0:x1])
                    raise
            raise S3Unavailable(f"Formato no soportado para HDU {k}")

        want = ["IMAGE", "ZODI"] + (["FLAGS"] if "FLAGS" in cards else []) + (["VARIANCE"] if need_var and "VARIANCE" in cards else [])
        futs = {k: _range_pool.submit(_fetch_part, k) for k in want if k in cards}
        wave_fut = _range_pool.submit(_hdu_file_bytes, url, lay, "WCS-WAVE", "wave")
        part = {k: f.result() for k, f in futs.items()}

        image = part["IMAGE"].astype(np.float32)
        var = part["VARIANCE"].astype(np.float32) if "VARIANCE" in part else None

        # misma geometria que el servicio de recortes: solo se desplaza CRPIX
        hdr = hdr0.copy()
        hdr["NAXIS1"], hdr["NAXIS2"] = x1 - x0, y1 - y0
        hdr["CRPIX1"] = float(hdr["CRPIX1"]) - x0
        hdr["CRPIX2"] = float(hdr["CRPIX2"]) - y0
        wcs = WCS(hdr)

        wl = bw = None
        try:
            wbytes = wave_fut.result()
            if wbytes is not None:
                with fits.open(io.BytesIO(wbytes), memmap=False) as whdul:
                    sw = WCS(hdr0, fobj=whdul, key="W")   # coordenadas del detector completo
                    sw.sip = None
                    w_, b_ = sw.pixel_to_world(x, y)
                    wl, bw = float(w_.to(u.micrometer).value), float(b_.to(u.micrometer).value)
                    if not np.isfinite(wl):
                        wl = bw = None
        except Exception:
            pass
        omega = float(proj_plane_pixel_area(wcs)) * (np.pi / 180.0) ** 2
    clean = imaging.clean_exposure(image, part.get("FLAGS"), part.get("ZODI"))
    return {"image": clean, "variance": var, "wcs": wcs, "x": x - x0, "y": y - y0,
            "wavelength": wl, "bandwidth": bw, "omega": omega}


app = FastAPI()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/cutout")
def cutout(request: Request, uri: str, ra: float, dec: float, size_deg: float, need_var: bool = True) -> Response:
    if _TOKEN and request.query_params.get("token") != _TOKEN:
        raise HTTPException(status_code=403, detail="token invalido")
    try:
        out = s3_cutout(uri, ra, dec, size_deg, need_var)
    except S3Unavailable as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"error leyendo S3: {e}") from e
    buf = io.BytesIO()
    meta = {"x": out["x"], "y": out["y"], "wl": out["wavelength"],
            "bw": out["bandwidth"], "omega": out["omega"]}
    arrays = {"image": out["image"].astype(np.float32),
              "hdr": out["wcs"].to_header(relax=True).tostring().encode("latin1"),
              "meta": json.dumps(meta)}
    if out["variance"] is not None:
        arrays["variance"] = out["variance"].astype(np.float32)
    np.savez(buf, **arrays)
    return Response(content=buf.getvalue(), media_type="application/octet-stream")
