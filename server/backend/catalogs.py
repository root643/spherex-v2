"""Consultas a catalogos astronomicos externos (Gaia DR3 y AllWISE) via VizieR/CDS con soporte para multiples espejos y cache Redis/local."""
from __future__ import annotations

import csv
import io
import math
import os
import re
import urllib.parse
from typing import Any

import requests

VIZIER_MIRRORS = [
    "https://vizier.cfa.harvard.edu/viz-bin/asu-tsv",
    "https://vizier.hia.nrc.ca/viz-bin/asu-tsv",
    "https://vizier.iucaa.in/viz-bin/asu-tsv",
]

_session = requests.Session()
_session.headers["User-Agent"] = "spherex-wiseview-catalog/1.0 (NASA Space Apps 2026)"
_adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=32, max_retries=1)
_session.mount("https://", _adapter)


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s == "---" or s.lower() == "nan" or s == "null":
        return None
    try:
        f = float(s)
        return None if math.isnan(f) else f
    except (ValueError, TypeError):
        return None


def _query_vizier_tsv(params: dict[str, str], timeout: float = 8.0) -> list[dict[str, str]]:
    """Consulta VizieR asu-tsv probando espejos en orden hasta que uno responda 200."""
    last_err = None
    for mirror in VIZIER_MIRRORS:
        try:
            r = _session.get(mirror, params=params, timeout=timeout)
            if r.status_code == 200 and r.text:
                lines = [l for l in r.text.splitlines() if not l.startswith("#") and l.strip()]
                if not lines:
                    return []
                # lines[0] = columnas
                # lines[1] = unidades
                # lines[2] = separador dash
                # lines[3:] = datos
                cols = [c.strip() for c in lines[0].split("\t")]
                data_rows = []
                for line in lines[3:]:
                    parts = [p.strip() for p in line.split("\t")]
                    if len(parts) >= len(cols):
                        data_rows.append(dict(zip(cols, parts)))
                    elif len(parts) > 0:
                        # Si sobran columnas o faltan vacias
                        row = {col: (parts[i] if i < len(parts) else "") for i, col in enumerate(cols)}
                        data_rows.append(row)
                return data_rows
            else:
                last_err = RuntimeError(f"HTTP {r.status_code} desde {mirror}: {r.text[:80]}")
        except Exception as e:
            last_err = e
            continue
    if last_err:
        raise RuntimeError(f"Error consultando catálogo en VizieR: {last_err}")
    return []


def query_gaia(ra: float, dec: float, radius_arcsec: float = 180.0, limit: int = 150) -> list[dict]:
    """Obtiene estrellas de Gaia DR3 (I/355/gaiadr3) en un radio en segundos de arco."""
    radius_arcsec = max(10.0, min(1800.0, float(radius_arcsec)))
    limit = max(1, min(300, int(limit)))
    params = {
        "-source": "I/355/gaiadr3",
        "-c": f"{ra:.5f} {dec:+.5f}",
        "-c.r": str(round(radius_arcsec, 1)),
        "-c.u": "arcsec",
        "-out": "Source,RA_ICRS,DE_ICRS,pmRA,pmDE,Plx,Gmag,BPmag,RPmag,BP-RP",
        "-out.max": str(limit),
        "-sort": "Gmag",
    }
    rows = _query_vizier_tsv(params)
    sources = []
    for r in rows:
        s_ra = _safe_float(r.get("RA_ICRS"))
        s_dec = _safe_float(r.get("DE_ICRS"))
        if s_ra is None or s_dec is None:
            continue
        pmra = _safe_float(r.get("pmRA"))
        pmdec = _safe_float(r.get("pmDE"))
        pm_tot = None
        if pmra is not None and pmdec is not None:
            pm_tot = round(math.hypot(pmra, pmdec), 2)
        gmag = _safe_float(r.get("Gmag"))
        bpmag = _safe_float(r.get("BPmag"))
        rpmag = _safe_float(r.get("RPmag"))
        bp_rp = _safe_float(r.get("BP-RP"))
        if bp_rp is None and bpmag is not None and rpmag is not None:
            bp_rp = round(bpmag - rpmag, 3)
        plx = _safe_float(r.get("Plx"))

        sources.append({
            "id": r.get("Source", ""),
            "ra": round(s_ra, 6),
            "dec": round(s_dec, 6),
            "pmra": round(pmra, 2) if pmra is not None else None,
            "pmdec": round(pmdec, 2) if pmdec is not None else None,
            "pm_total": pm_tot,
            "gmag": round(gmag, 3) if gmag is not None else None,
            "bpmag": round(bpmag, 3) if bpmag is not None else None,
            "rpmag": round(rpmag, 3) if rpmag is not None else None,
            "bp_rp": round(bp_rp, 3) if bp_rp is not None else None,
            "parallax": round(plx, 2) if plx is not None else None,
        })
    return sources


def query_wise(ra: float, dec: float, radius_arcsec: float = 180.0, limit: int = 150) -> list[dict]:
    """Obtiene detecciones del catalogo AllWISE (II/328/allwise) en un radio en segundos de arco."""
    radius_arcsec = max(10.0, min(1800.0, float(radius_arcsec)))
    limit = max(1, min(300, int(limit)))
    params = {
        "-source": "II/328/allwise",
        "-c": f"{ra:.5f} {dec:+.5f}",
        "-c.r": str(round(radius_arcsec, 1)),
        "-c.u": "arcsec",
        "-out": "AllWISE,RAJ2000,DEJ2000,W1mag,W2mag,W3mag,W4mag,pmRA,pmDE",
        "-out.max": str(limit),
        "-sort": "_r",
    }
    rows = _query_vizier_tsv(params)
    sources = []
    for r in rows:
        s_ra = _safe_float(r.get("RAJ2000"))
        s_dec = _safe_float(r.get("DEJ2000"))
        if s_ra is None or s_dec is None:
            continue
        w1 = _safe_float(r.get("W1mag"))
        w2 = _safe_float(r.get("W2mag"))
        w3 = _safe_float(r.get("W3mag"))
        w4 = _safe_float(r.get("W4mag"))
        w1_w2 = None
        if w1 is not None and w2 is not None:
            w1_w2 = round(w1 - w2, 3)
        pmra = _safe_float(r.get("pmRA"))
        pmdec = _safe_float(r.get("pmDE"))
        pm_tot = None
        if pmra is not None and pmdec is not None:
            pm_tot = round(math.hypot(pmra, pmdec), 2)

        sources.append({
            "id": r.get("AllWISE", ""),
            "ra": round(s_ra, 6),
            "dec": round(s_dec, 6),
            "w1": round(w1, 3) if w1 is not None else None,
            "w2": round(w2, 3) if w2 is not None else None,
            "w3": round(w3, 3) if w3 is not None else None,
            "w4": round(w4, 3) if w4 is not None else None,
            "w1_w2": w1_w2,
            "pmra": round(pmra, 2) if pmra is not None else None,
            "pmdec": round(pmdec, 2) if pmdec is not None else None,
            "pm_total": pm_tot,
        })
    return sources
