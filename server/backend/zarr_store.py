"""Almacen Cloud-Native en Zarr y streaming cientifico de cubos espectrales (ZFP / Blosc).

Permite almacenar cubos espectrales multidimensionales (lambda, y, x) en formato Zarr
particionados en bloques (17, 64, 64) y servir planos o cubos con compresion ZFP/Blosc
para investigacion cientifica a maxima fidelidad numerica float32.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import warnings
from typing import Optional, Tuple

import numpy as np
import zarr

ZARR_DIR = os.environ.get("SPHEREX_ZARR_DIR", os.path.join(tempfile.gettempdir(), "spherex_zarr"))

# Deteccion de codec ZFP nativo o fallback Blosc-Zstd
_HAS_ZFP = False
try:
    import zfpy
    _HAS_ZFP = True
except Exception:
    _HAS_ZFP = False

_HAS_NUMCODECS = False
try:
    from numcodecs import Blosc, Zstd
    _HAS_NUMCODECS = True
except Exception:
    _HAS_NUMCODECS = False


def compress_float32(arr: np.ndarray, method: str = "zfp", tolerance: float = 1e-4) -> tuple[bytes, str]:
    """Comprime una matriz float32 (2D o 3D) usando ZFP o Blosc-ZSTD.

    Devuelve (bytes_comprimidos, codec_utilizado).
    """
    arr = np.asarray(arr, dtype=np.float32)
    m = method.lower().strip()

    if m == "zfp" and _HAS_ZFP:
        try:
            return zfpy.compress_numpy(arr, tolerance=tolerance), "zfp"
        except Exception:
            pass

    if m in ("zfp", "blosc") and _HAS_NUMCODECS:
        try:
            codec = Blosc(cname="zstd", clevel=5, shuffle=Blosc.BITSHUFFLE)
            return codec.encode(arr), "blosc-zstd"
        except Exception:
            pass

    if m == "raw":
        return arr.tobytes(), "raw"

    # Fallback estandar
    return arr.tobytes(), "raw"


def decompress_float32(data: bytes, shape: tuple[int, ...], method: str, dtype: np.dtype = np.float32) -> np.ndarray:
    """Descomprime un buffer de bytes a un array NumPy segun el codec empleado."""
    m = method.lower().strip()
    if m == "zfp" and _HAS_ZFP:
        return zfpy.decompress_numpy(data)
    if m == "blosc-zstd" and _HAS_NUMCODECS:
        codec = Blosc(cname="zstd", clevel=5, shuffle=Blosc.BITSHUFFLE)
        decoded = codec.decode(data)
        return np.frombuffer(decoded, dtype=dtype).reshape(shape)
    if m == "raw":
        return np.frombuffer(data, dtype=dtype).reshape(shape)
    return np.frombuffer(data, dtype=dtype).reshape(shape)


def cube_id(ra: float, dec: float, fov: float) -> str:
    """Identificador determinista para el cubo Zarr."""
    key = f"{ra:.5f}:{dec:.5f}:{fov:.2f}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def cube_path(ra: float, dec: float, fov: float) -> str:
    os.makedirs(ZARR_DIR, exist_ok=True)
    cid = cube_id(ra, dec, fov)
    return os.path.join(ZARR_DIR, f"cube_{cid}.zarr")


def save_zarr_cube(path: str, cube: np.ndarray, ra: float, dec: float, fov: float,
                   wavelengths: list[float], wcs_info: dict | None = None) -> str:
    """Crea y persiste un cubo 3D (n_lambda, ny, nx) en formato Zarr.

    Utiliza chunks de (17, min(64, ny), min(64, nx)) para alineacion optima con
    los detectores de SPHEREx y el acceso espacial.
    """
    cube = np.asarray(cube, dtype=np.float32)
    n_ch, ny, nx = cube.shape
    chunk_ch = min(17, n_ch)
    chunk_y = min(64, ny)
    chunk_x = min(64, nx)

    if os.path.exists(path):
        try:
            shutil.rmtree(path)
        except OSError:
            pass

    root = zarr.open_group(path, mode="w")
    arr = root.create_array(name="data", shape=(n_ch, ny, nx),
                            chunks=(chunk_ch, chunk_y, chunk_x), dtype="float32")
    arr[:] = cube

    # Metadatos del hipercubo
    root.attrs["ra"] = float(ra)
    root.attrs["dec"] = float(dec)
    root.attrs["fov"] = float(fov)
    root.attrs["wavelengths"] = [float(w) for w in wavelengths]
    root.attrs["n_channels"] = int(n_ch)
    root.attrs["shape"] = [int(n_ch), int(ny), int(nx)]
    if wcs_info:
        root.attrs["wcs"] = wcs_info
    return path


def open_zarr_cube(path: str):
    """Abre el cubo Zarr en modo lectura."""
    if not os.path.exists(path):
        return None
    try:
        return zarr.open_group(path, mode="r")
    except Exception:
        return None


def extract_spectrum_from_zarr(path: str, x: int, y: int) -> dict | None:
    """Extrae instantaneamente (<10ms) el perfil espectral en el pixel (x, y) desde el cubo Zarr."""
    root = open_zarr_cube(path)
    if root is None or "data" not in root:
        return None
    arr = root["data"]
    n_ch, ny, nx = arr.shape
    if not (0 <= x < nx and 0 <= y < ny):
        return None
    spec_values = arr[:, y, x]
    wls = root.attrs.get("wavelengths", [])
    points = []
    for ch_idx, val in enumerate(spec_values):
        wl = wls[ch_idx] if ch_idx < len(wls) else None
        points.append({
            "channel": ch_idx + 1,
            "wavelength": wl,
            "flux": float(val) if np.isfinite(val) else None
        })
    return {
        "x": x, "y": y,
        "n_channels": n_ch,
        "spectrum": points
    }


def extract_plane_from_zarr(path: str, channel: int) -> np.ndarray | None:
    """Devuelve el corte 2D de un canal espectral concreto (1-indexed)."""
    root = open_zarr_cube(path)
    if root is None or "data" not in root:
        return None
    arr = root["data"]
    n_ch = arr.shape[0]
    idx = channel - 1
    if not (0 <= idx < n_ch):
        return None
    return np.array(arr[idx, :, :])
