"""Generador y servidor de encuestas celestes HiPS (Hierarchical Progressive Surveys) de SPHEREx.

Implementa la recomendacion IVOA HiPS 1.4 sobre teselacion HEALPix (esquema NESTED).
Permite a Aladin Desktop y Aladin Lite navegar por todo el cielo de SPHEREx hasta orden 7.
"""
from __future__ import annotations

import io
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
import requests
from PIL import Image

HIPS_ROOT = os.environ.get("SPHEREX_HIPS_ROOT", "/var/www/spherex-hips")
SURVEY_ID = "spherex_dr1"
SURVEY_DIR = os.path.join(HIPS_ROOT, SURVEY_ID)
S3_HIPS5_BASE = "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/dr1/hips5_cube/hips_20261026/Norder5"

# Canal 60 = Banda D4 (3.3 um, MIR), optimo para todo el cielo (262k pixeles finitos)
DEFAULT_CHANNEL = 60
TILE_SIZE = 512

# Sesion HTTP con reintentos para extraer cubos de S3
_s3_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=64, max_retries=2)
_s3_session.mount("https://", _adapter)

# Cache de memoria para tiles frecuentes
_mem_cache: dict[str, bytes] = {}
_cache_lock = threading.Lock()


def morton2d_decode(p: int) -> tuple[int, int]:
    """Decodifica un indice NESTED (Morton Z-order) en coordenadas 2D (x, y)."""
    x, y = 0, 0
    for i in range(8):
        x |= ((p >> (2 * i)) & 1) << i
        y |= ((p >> (2 * i + 1)) & 1) << i
    return x, y


def ensure_properties() -> str:
    """Genera el fichero de metadatos IVOA HiPS 1.4 properties."""
    os.makedirs(SURVEY_DIR, exist_ok=True)
    prop_path = os.path.join(SURVEY_DIR, "properties")
    props = f"""creator_did          = ivo://spherex.wiki/hips/dr1_d4
obs_title            = SPHEREx All-Sky DR1 (3.3 um)
obs_description      = NASA SPHEREx Space Infrared All-Sky Survey (Band 4, 3.3 um)
obs_collection       = SPHEREx
prov_progenitor      = NASA/IPAC SPHEREx Science Data Center
hips_version         = 1.4
hips_frame           = equatorial
hips_order           = 7
hips_tile_format     = png
hips_tile_width      = {TILE_SIZE}
hips_pixel_scale     = 0.000895
hips_status          = public
hips_initial_ra      = 269.4521
hips_initial_dec     = 4.6934
hips_initial_fov     = 15.0
"""
    with open(prop_path, "w", encoding="utf-8") as f:
        f.write(props)
    return props


def tile_path(order: int, pix: int) -> str:
    dir_idx = (pix // 10000) * 10000
    return os.path.join(SURVEY_DIR, f"Norder{order}", f"Dir{dir_idx}", f"Npix{pix}.png")


def _read_s3_order5_channel(pix: int, channel: int = DEFAULT_CHANNEL) -> np.ndarray | None:
    """Descarga el plano 512x512 de un canal del cubo S3 mediante HTTP Range Request."""
    dir_idx = (pix // 10000) * 10000
    url = f"{S3_HIPS5_BASE}/Dir{dir_idx}/Npix{pix}_cube.fits"
    # Header primario (2880) + extension (5760) = 8640 B
    # Cada canal tiene 512 * 512 * 4 = 1.048.576 bytes
    plane_bytes = TILE_SIZE * TILE_SIZE * 4
    start = 8640 + channel * plane_bytes
    end = start + plane_bytes - 1
    try:
        r = _s3_session.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=6.0)
        if r.status_code == 206 and len(r.content) == plane_bytes:
            arr = np.frombuffer(r.content, dtype=">f4").reshape(TILE_SIZE, TILE_SIZE)
            return arr
    except Exception:
        pass
    return None


def _array_to_png(arr: np.ndarray) -> bytes:
    """Convierte matriz 2D en baldosas PNG normalizadas con estiramiento astronómico asinh."""
    # En FITS fila 0 es sur (abajo), en PNG fila 0 es norte (arriba)
    arr_flipped = np.flipud(arr)
    valid = arr_flipped[np.isfinite(arr_flipped)]
    if len(valid) == 0:
        img = Image.new("L", (TILE_SIZE, TILE_SIZE), 0)
    else:
        p10 = float(np.percentile(valid, 10))
        p99 = float(np.percentile(valid, 99))
        span = max(1e-5, p99 - p10)
        norm = np.clip((arr_flipped - p10) / span, 0, 1)
        # Asinh stretch para rango dinamico astronomico
        stretched = np.arcsinh(norm * 3.0) / np.arcsinh(3.0)
        stretched = np.nan_to_num(stretched, nan=0.0)
        img_arr = (np.clip(stretched, 0, 1) * 255).astype(np.uint8)
        img = Image.fromarray(img_arr)

    bio = io.BytesIO()
    img.save(bio, format="PNG", optimize=True)
    return bio.getvalue()


def get_tile_png(order: int, pix: int) -> bytes:
    """Obtiene una baldosa HiPS de orden 0 a 7, sirviendo desde disco o generandola progresivamente."""
    path = tile_path(order, pix)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()

    key = f"{order}:{pix}"
    with _cache_lock:
        if key in _mem_cache:
            return _mem_cache[key]

    data = _generate_tile(order, pix)
    if data:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        except OSError:
            pass
        with _cache_lock:
            if len(_mem_cache) > 500:
                _mem_cache.clear()
            _mem_cache[key] = data
        return data

    # Si no hay datos, devolver tile vacio
    blank = _empty_tile()
    return blank


def _empty_tile() -> bytes:
    img = Image.new("L", (TILE_SIZE, TILE_SIZE), 0)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()


def _generate_tile(order: int, pix: int) -> bytes:
    """Construccion progresiva y multiescala para los ordenes 0 a 7."""
    if order == 5:
        arr = _read_s3_order5_channel(pix)
        if arr is not None:
            return _array_to_png(arr)
        return _empty_tile()

    if order < 5:
        # Los ordenes inferiores se agregan a partir de sus 4 hijos en orden+1
        c0 = pix * 4 + 0
        c1 = pix * 4 + 1
        c2 = pix * 4 + 2
        c3 = pix * 4 + 3

        # Cargar los 4 cuadrantes
        im0 = Image.open(io.BytesIO(get_tile_png(order + 1, c0))).resize((TILE_SIZE // 2, TILE_SIZE // 2), Image.Resampling.BOX)
        im1 = Image.open(io.BytesIO(get_tile_png(order + 1, c1))).resize((TILE_SIZE // 2, TILE_SIZE // 2), Image.Resampling.BOX)
        im2 = Image.open(io.BytesIO(get_tile_png(order + 1, c2))).resize((TILE_SIZE // 2, TILE_SIZE // 2), Image.Resampling.BOX)
        im3 = Image.open(io.BytesIO(get_tile_png(order + 1, c3))).resize((TILE_SIZE // 2, TILE_SIZE // 2), Image.Resampling.BOX)

        combined = Image.new("L", (TILE_SIZE, TILE_SIZE))
        for c, im in enumerate([im0, im1, im2, im3]):
            mx, my = morton2d_decode(c)
            combined.paste(im, (mx * (TILE_SIZE // 2), my * (TILE_SIZE // 2)))

        bio = io.BytesIO()
        combined.save(bio, format="PNG", optimize=True)
        return bio.getvalue()

    if order == 6:
        # Orden 6: 4 cuadrantes decodificados por curva Morton Z-order de la baldosa de orden 5
        parent_5 = pix // 4
        sub_quad = pix % 4
        parent_png = get_tile_png(5, parent_5)
        p_img = Image.open(io.BytesIO(parent_png))
        half = TILE_SIZE // 2
        mx, my = morton2d_decode(sub_quad)
        x = mx * half
        y = my * half
        crop = p_img.crop((x, y, x + half, y + half))
        upscaled = crop.resize((TILE_SIZE, TILE_SIZE), Image.Resampling.BICUBIC)
        bio = io.BytesIO()
        upscaled.save(bio, format="PNG", optimize=True)
        return bio.getvalue()

    if order == 7:
        # Orden 7: 16 sub-cuadrantes decodificados por curva Morton Z-order de la baldosa de orden 5
        parent_5 = pix // 16
        sub_16 = pix % 16
        parent_png = get_tile_png(5, parent_5)
        p_img = Image.open(io.BytesIO(parent_png))
        quarter = TILE_SIZE // 4
        mx, my = morton2d_decode(sub_16)
        x = mx * quarter
        y = my * quarter
        crop = p_img.crop((x, y, x + quarter, y + quarter))
        upscaled = crop.resize((TILE_SIZE, TILE_SIZE), Image.Resampling.BICUBIC)
        bio = io.BytesIO()
        upscaled.save(bio, format="PNG", optimize=True)
        return bio.getvalue()

    return _empty_tile()


def get_allsky_png() -> bytes:
    """Genera Allsky.png a partir de las 12 baldosas de orden 0."""
    allsky_path = os.path.join(SURVEY_DIR, "Allsky.png")
    if os.path.exists(allsky_path):
        with open(allsky_path, "rb") as f:
            return f.read()

    # Mapear las 12 baldosas base de HEALPix en un lienzo de 2048x1024
    canvas = Image.new("L", (2048, 1024), 0)
    for p in range(12):
        tile_bytes = get_tile_png(0, p)
        tile_img = Image.open(io.BytesIO(tile_bytes)).resize((512, 512), Image.Resampling.LANCZOS)
        row = p // 4
        col = p % 4
        canvas.paste(tile_img, (col * 512, row * 341))

    bio = io.BytesIO()
    canvas.save(bio, format="PNG", optimize=True)
    out = bio.getvalue()
    try:
        with open(allsky_path, "wb") as f:
            f.write(out)
    except OSError:
        pass
    return out
