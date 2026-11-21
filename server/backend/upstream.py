"""Acceso a los datos de SPHEREx: metadatos por TAP de IRSA, recortes del bucket
publico de AWS S3 por rangos de bytes (con el servicio de recortes de IRSA de respaldo),
lectura de los MEF de SPHEREx y reproyeccion WCS a una rejilla TAN comun."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
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
from reproject import reproject_interp

import imaging
import s3fits

TAP_SYNC = os.environ.get("SPHEREX_TAP_URL", "https://irsa.ipac.caltech.edu/TAP/sync")
IRSA_BASE = "https://irsa.ipac.caltech.edu/"
FITS_DIR = os.environ.get("SPHEREX_FITS_DIR", "/var/cache/spherex/fits")
NATIVE_SCALE = 6.15  # arcsec/pixel
S3_BASE = os.environ.get("SPHEREX_S3_BASE", "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/")
_S3_PREFIX = "ibe/data/spherex/"
SOURCE = os.environ.get("SPHEREX_SOURCE", "s3").strip().lower()
_AWS_CUTOUT = os.environ.get("SPHEREX_AWS_CUTOUT", "").strip()  # caja de recortes en us-east-1

_session = requests.Session()
_session.headers["User-Agent"] = "spherex-wiseview/1.0 (NASA Space Apps 2026)"
_adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=96, max_retries=2)
_session.mount("https://", _adapter)
_range_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("SPHEREX_S3_THREADS", "48")))

_OBS_ID = re.compile(r"level2_(.+?)_spx")
_RELEASE = re.compile(r"/spherex/([a-z]+)(\d*)(_deep)?/")


# --------------------------------------------------------------------------- TAP
def _release_rank(uri: str) -> tuple[int, int]:
    """Prioridad entre publicaciones del mismo exposure: DR > QR, numero mayor primero."""
    m = _RELEASE.search(uri)
    if not m:
        return (0, 0)
    return (2 if m.group(1) == "dr" else 1, int(m.group(2) or 1))


def list_exposures(ra: float, dec: float) -> list[dict]:
    """Todas las imagenes espectrales de SPHEREx que contienen la posicion.

    Si el catalogo local esta listo, se usa SQLite local directamente (<0.01s).
    Si no, se intenta IRSA TAP y se cae a SQLite.
    """
    if _catalog_ready():
        report("catalog_local")
        return list_exposures_local(ra, dec)
    try:
        return _list_exposures_tap(ra, dec)
    except Exception as e:
        local = list_exposures_local(ra, dec)
        if local:
            report("tap_local", str(e)[:300])
            return local
        raise


def _list_exposures_tap(ra: float, dec: float) -> list[dict]:
    """El listado via IRSA TAP (el original)."""
    q = (
        "SELECT a.uri AS uri, p.time_bounds_lower AS mjd, p.energy_bandpassname AS band "
        "FROM spherex.artifact a JOIN spherex.plane p ON a.planeid = p.planeid "
        f"WHERE 1 = CONTAINS(POINT('ICRS', {ra:.6f}, {dec:.6f}), p.poly) "
        "ORDER BY p.time_bounds_lower"
    )
    r = _session.post(TAP_SYNC, data={"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "CSV", "QUERY": q},
                      timeout=90)
    r.raise_for_status()
    text = r.text
    if "QUERY_STATUS" in text[:2000] and "ERROR" in text[:2000]:
        raise RuntimeError("IRSA TAP devolvio un error: " + text[:300])
    best: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(text)):
        uri = (row.get("uri") or "").strip().strip('"')
        band = (row.get("band") or "").strip().strip('"')
        m = re.search(r"D([1-6])\b", band)
        if not uri or not uri.endswith(".fits") or uri.startswith("s3:") or not m:
            continue
        try:
            mjd = float(row["mjd"])
        except (TypeError, ValueError):
            continue
        oid = _OBS_ID.search(uri)
        key = oid.group(1) if oid else uri
        e = {"uri": uri, "mjd": mjd, "detector": int(m.group(1)), "obs": key}
        if key not in best or _release_rank(uri) > _release_rank(best[key]["uri"]):
            best[key] = e
    return sorted(best.values(), key=lambda e: e["mjd"])


# --------------------------------------------------------------------------- catalogo local (volcado de TAP)
_CATALOG_DB = os.environ.get("SPHEREX_CATALOG_DB", os.path.join(FITS_DIR, "catalog.db"))


def _catalog_ready() -> bool:
    """True si hay volcado completo del catalogo en disco (meta.complete = 1)."""
    if not os.path.exists(_CATALOG_DB):
        return False
    try:
        con = sqlite3.connect(f"file:{_CATALOG_DB}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT v FROM meta WHERE k = 'complete'").fetchone()
        finally:
            con.close()
        return bool(row and row[0] == "1")
    except sqlite3.Error:
        return False


def _point_in_poly(poly: list[float], ra: float, dec: float) -> bool:
    n = len(poly) // 2
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi, xj, yj = poly[2 * i], poly[2 * i + 1], poly[2 * j], poly[2 * j + 1]
        if (yi > dec) != (yj > dec) and ra < (xj - xi) * (dec - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def list_exposures_local(ra: float, dec: float) -> list[dict]:
    """El mismo listado que _list_exposures_tap, pero contra la copia SQLite local.

    Candidates por bbox del poligono (indice espacial) y comprobacion exacta
    punto-dentro-de-poligono; mismo dedupe por obs y prioridad de release.
    """
    ra_n = ra - 360.0 if ra > 180.0 else ra
    con = sqlite3.connect(f"file:{_CATALOG_DB}?mode=ro", uri=True)
    try:
        cand = con.execute(
            "SELECT c.uri, c.mjd, c.band, c.poly FROM rtree_idx r "
            "JOIN catalog c ON c.rowid = r.id "
            "WHERE ((r.ra_min <= ? AND r.ra_max >= ?) OR (r.ra_min <= ? AND r.ra_max >= ?)) "
            "AND r.dec_min <= ? AND r.dec_max >= ?",
            (ra_n, ra_n, ra, ra, dec, dec)).fetchall()
    finally:
        con.close()
    best: dict[str, dict] = {}
    for uri, mjd, band, poly in cand:
        m = re.search(r"D([1-6])\b", band or "")
        if not uri or not uri.endswith(".fits") or not m:
            continue
        if poly:
            verts = [float(v) for v in poly.split()]
            if len(verts) >= 6 and len(verts) % 2 == 0:
                if not (_point_in_poly(verts, ra_n, dec) or _point_in_poly(verts, ra, dec)):
                    continue
        try:
            mjd = float(mjd)
        except (TypeError, ValueError):
            continue
        oid = _OBS_ID.search(uri)
        key = oid.group(1) if oid else uri
        e = {"uri": uri, "mjd": mjd, "detector": int(m.group(1)), "obs": key}
        if key not in best or _release_rank(uri) > _release_rank(best[key]["uri"]):
            best[key] = e
    return sorted(best.values(), key=lambda e: e["mjd"])


# --------------------------------------------------------------------------- recortes

def cutout_url(uri: str, ra: float, dec: float, size_deg: float) -> str:
    base = uri if uri.startswith("http") else IRSA_BASE + uri.lstrip("/")
    return f"{base}?center={ra:.6f},{dec:.6f}d&size={size_deg:.5f}"


def fetch(url: str) -> bytes:
    """Descarga con cache en disco NVMe (escritura atomica)."""
    h = hashlib.sha1(url.encode()).hexdigest()
    path = os.path.join(FITS_DIR, h[:2], h + ".fits")
    try:
        with open(path, "rb") as f:
            data = f.read()
        os.utime(path, None)  # marca de uso para la poda LRU
        return data
    except OSError:
        pass
    last = None
    for attempt in range(3):
        try:
            r = _session.get(url, timeout=(10, 90))
            if r.status_code == 200 and r.content[:6] == b"SIMPLE":
                data = r.content
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    os.replace(tmp, path)
                except OSError:
                    pass
                return data
            last = RuntimeError(f"HTTP {r.status_code} en recorte IRSA")
            if 400 <= r.status_code < 500:
                break
        except requests.RequestException as e:
            last = e
        time.sleep(0.5 * (attempt + 1))
    raise last or RuntimeError("fallo de descarga")


def read_cutout(data: bytes, ra: float, dec: float) -> dict:
    """Lee un recorte MEF: imagen limpia, varianza, WCS espacial y longitud de onda en (ra, dec)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        warnings.simplefilter("ignore", RuntimeWarning)
        with fits.open(io.BytesIO(data), memmap=False) as hdul:
            hdr = hdul["IMAGE"].header
            image = np.asarray(hdul["IMAGE"].data, dtype=np.float32)
            flags = hdul["FLAGS"].data if "FLAGS" in hdul else None
            zodi = hdul["ZODI"].data if "ZODI" in hdul else None
            var = np.asarray(hdul["VARIANCE"].data, dtype=np.float32) if "VARIANCE" in hdul else None
            wcs = WCS(hdr)
            x, y = wcs.world_to_pixel(SkyCoord(ra, dec, unit="deg"))
            x, y = float(x), float(y)
            wl = bw = None
            try:
                sw = WCS(hdr, fobj=hdul, key="W")
                sw.sip = None
                w_, b_ = sw.pixel_to_world(x, y)
                wl, bw = float(w_.to(u.micrometer).value), float(b_.to(u.micrometer).value)
                if not np.isfinite(wl):
                    wl = bw = None
            except Exception:
                pass
            omega = float(proj_plane_pixel_area(wcs)) * (np.pi / 180.0) ** 2  # sr / pixel
    return {"image": imaging.clean_exposure(image, flags, zodi), "variance": var, "wcs": wcs,
            "x": x, "y": y, "wavelength": wl, "bandwidth": bw, "omega": omega}


# --------------------------------------------------------------------------- recortes desde S3
# El bucket publico nasa-irsa-spherex (AWS, us-east-1) tiene los level2 completos:
# decenas de MB por imagen. En vez de pedir el recorte al servicio de IRSA
# (que abre y recorta el fichero en sus servidores) o bajar el fichero entero, se
# leen por HTTP Range solo las cabeceras (una vez por fichero, quedan en disco) y
# las filas del recorte de IMAGE, ZODI y VARIANCE, mas el HDU FLAGS comprimido.

class S3Unavailable(Exception):
    """El fichero no esta en el bucket o no se puede leer por filas: usar IRSA."""


def report(kind: str, detail: str | None = None) -> None:
    """Contador de origen de los recortes (s3 / irsa / s3_missing / s3_error).
    main.py lo redirige a Redis para que /cache/stats lo ensene."""


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
_FIRST_READ = 12 * s3fits.BLOCK   # suele cubrir el HDU primario y la cabecera de IMAGE
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
    """Lo mismo que read_cutout(fetch(cutout_url(...))) pero leyendo del bucket por rangos.

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

        flags_packed = "FLAGS" in cards and not s3fits.is_plain_image(cards["FLAGS"])

        def _fetch_part(k: str):
            c = cards[k]
            if k == "FLAGS" and flags_packed:
                # FLAGS viene comprimido con Rice (~50-100 KB total).
                # Usar cache local en SSD: se descarga de una sola vez y se reusa en 0 ms.
                fb = _hdu_file_bytes(url, lay, "FLAGS", "flags")
                if fb is not None:
                    with fits.open(io.BytesIO(fb), memmap=False) as fhdul:
                        try:
                            fl = fhdul[1].section[y0:y1, x0:x1]
                        except Exception:
                            fl = fhdul[1].data[y0:y1, x0:x1]
                        return np.array(fl)
            if s3fits.is_plain_image(c):
                return s3fits.rows(get, exts[k], y0, y1, c)[:, x0:x1]
            elif s3fits.is_compressed_image(c):
                # Soporte de teselas fpack mediante Range Requests selectivos
                return s3fits.read_compressed_section(get, exts[k], y0, y1, x0, x1, c)
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
    return {"image": imaging.clean_exposure(image, part.get("FLAGS"), part.get("ZODI")), "variance": var,
            "wcs": wcs, "x": x - x0, "y": y - y0, "wavelength": wl, "bandwidth": bw, "omega": omega}


_s3_streak = 0
_s3_off_until = 0.0


# --------------------------------------------------------------------------- recortes via la caja AWS
_aws_streak = 0
_aws_off_until = 0.0


def aws_cutout(uri: str, ra: float, dec: float, size_deg: float, need_var: bool = True) -> dict:
    """Recorte via la caja de us-east-1 (tunel SSH), que lee el bucket en su region.

    Devuelve lo mismo que s3_cutout. Lanza S3Unavailable si no responde (el llamante
    cae al S3 directo); si falla muchas veces seguidas se deja de intentar 10 min.
    """
    global _aws_streak, _aws_off_until
    if not _AWS_CUTOUT:
        raise S3Unavailable("SPHEREX_AWS_CUTOUT no configurado")
    if time.time() < _aws_off_until:
        raise S3Unavailable("aws apagada por fallos recientes")
    try:
        r = _session.get(_AWS_CUTOUT, params={"uri": uri, "ra": ra, "dec": dec,
                                              "size_deg": size_deg, "need_var": "true" if need_var else "false"},
                         timeout=(2, 20))
    except requests.RequestException as e:
        _aws_streak += 1
        if _aws_streak >= 12:
            _aws_streak, _aws_off_until = 0, time.time() + 600
        raise S3Unavailable(f"aws no responde: {e}") from e
    _aws_streak = 0
    if r.status_code in (502, 503):
        raise S3Unavailable(f"aws: {r.json().get('detail', r.status_code)}")
    if r.status_code == 404:
        raise ValueError("la posicion no cae en esta imagen (aws)")
    if r.status_code != 200:
        raise S3Unavailable(f"aws HTTP {r.status_code}")
    npz = np.load(io.BytesIO(r.content), allow_pickle=False)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FITSFixedWarning)
            warnings.simplefilter("ignore", RuntimeWarning)
            wcs = WCS(fits.Header.fromstring(npz["hdr"].item().decode("latin1")))
        meta = json.loads(npz["meta"].item())
        out = {"image": np.asarray(npz["image"], dtype=np.float32),
               "variance": np.asarray(npz["variance"], dtype=np.float32) if "variance" in npz else None,
               "wcs": wcs, "x": meta["x"], "y": meta["y"],
               "wavelength": meta["wl"], "bandwidth": meta["bw"], "omega": meta["omega"]}
    finally:
        npz.close()
    return out


def load_cutout(uri: str, ra: float, dec: float, size_deg: float, need_var: bool = True) -> dict:
    """Recorte de una imagen: caja AWS (us-east-1) -> bucket S3 directo -> servicio de IRSA.

    SPHEREX_SOURCE=s3 (por defecto) | irsa (solo IRSA) | s3only (solo S3 directo, sin AWS ni IRSA).
    """
    global _s3_streak, _s3_off_until, _aws_streak, _aws_off_until
    if SOURCE != "irsa" and (SOURCE == "s3only" or time.time() >= _s3_off_until):
        if _AWS_CUTOUT and SOURCE != "s3only" and time.time() >= _aws_off_until:
            try:
                out = aws_cutout(uri, ra, dec, size_deg, need_var)
                report("aws")
                return out
            except S3Unavailable as e:
                report("aws_error", str(e)[:300])
            except Exception as e:
                report("aws_error", f"{type(e).__name__}: {e}"[:300])
                _aws_streak += 1
                if _aws_streak >= 12:
                    _aws_streak, _aws_off_until = 0, time.time() + 600
        try:
            out = s3_cutout(uri, ra, dec, size_deg, need_var)
            _s3_streak = 0
            report("s3")
            return out
        except S3Unavailable as e:
            report("s3_missing", str(e)[:300])
            if SOURCE == "s3only":
                raise
        except Exception as e:
            report("s3_error", f"{type(e).__name__}: {e}"[:300])
            if SOURCE == "s3only":
                raise
            # si S3 falla muchas veces seguidas se deja de intentar 10 min para no pagar
            # dos esperas por recorte
            _s3_streak += 1
            if _s3_streak >= 12:
                _s3_streak, _s3_off_until = 0, time.time() + 600
    out = read_cutout(fetch(cutout_url(uri, ra, dec, size_deg)), ra, dec)
    report("irsa")
    return out


# --------------------------------------------------------------------------- rejilla comun
def grid_for(fov_arcsec: float, max_pixels: int = 512, min_scale: float | None = None) -> tuple[int, float]:
    """(N, escala arcsec/px). Por defecto 2x sobremuestreo de la escala nativa, limitado a max_pixels.
    Con min_scale mayor (p. ej. la escala nativa) sale la version de baja resolucion.
    Garantiza que n * scale == fov_arcsec exactamente."""
    target_scale = max(min_scale if min_scale is not None else NATIVE_SCALE / 2.0, fov_arcsec / max_pixels)
    n = max(8, int(np.round(fov_arcsec / target_scale)))
    scale = fov_arcsec / float(n)
    return n, scale


def target_wcs(ra: float, dec: float, n: int, scale_arcsec: float) -> WCS:
    """TAN estandar, norte arriba, este a la izquierda, centrada en (ra, dec)."""
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [(n + 1) / 2.0, (n + 1) / 2.0]
    w.wcs.cdelt = [-scale_arcsec / 3600.0, scale_arcsec / 3600.0]
    w.wcs.cunit = ["deg", "deg"]
    return w


def reproject_to(exp: dict, tw: WCS, n: int) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        arr, _ = reproject_interp((exp["image"], exp["wcs"]), tw, shape_out=(n, n), order="bilinear")
    return arr.astype(np.float32)
