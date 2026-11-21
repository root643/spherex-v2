"""Constructor en segundo plano del cielo celeste HiPS completo de SPHEREx (hasta Orden 7).

Construye progresivamente todas las baldosas de Norder 0 hasta Norder 7 y las guarda
en /var/www/spherex-hips/spherex_dr1/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import hips_store

try:
    import redis
    _r = redis.Redis.from_url(os.environ.get("SPHEREX_REDIS_URL", "redis://127.0.0.1:6379/0"))
except Exception:
    _r = None


def report_progress(order: int, current: int, total: int, grand_done: int, grand_total: int, status: str = "running"):
    info = {
        "order": order,
        "current": current,
        "total": total,
        "percent": round((current / max(1, total)) * 100, 2),
        "grand_done": grand_done,
        "grand_total": grand_total,
        "grand_percent": round((grand_done / max(1, grand_total)) * 100, 2),
        "status": status,
        "timestamp": time.time(),
    }
    if _r is not None:
        try:
            _r.set("stat:hips:builder", json.dumps(info))
        except Exception:
            pass
    print(f"[{status.upper()}] Order {order}: {current}/{total} tiles ({info['percent']}%) | Total: {grand_done}/{grand_total} ({info['grand_percent']}%)", flush=True)


def build_order(order: int, max_workers: int = 16, force: bool = False, grand_done_offset: int = 0, grand_total: int = 262140) -> int:
    n_tiles = 12 * (4 ** order)
    print(f"\n=== Iniciando construccion de Orden {order} ({n_tiles} baldosas) ===", flush=True)
    done = 0
    start_t = time.time()

    def process_tile(pix: int):
        try:
            path = hips_store.tile_path(order, pix)
            if not force and os.path.exists(path) and os.path.getsize(path) > 0:
                return True
            hips_store.get_tile_png(order, pix)
            return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for _ in pool.map(process_tile, range(n_tiles)):
            done += 1
            if done % 100 == 0 or done == n_tiles:
                report_progress(order, done, n_tiles, grand_done_offset + done, grand_total)

    elapsed = time.time() - start_t
    print(f"=== Orden {order} completado: {done} baldosas en {elapsed:.1f}s ({done / max(1, elapsed):.1f} tiles/s) ===\n", flush=True)
    return done


def main():
    parser = argparse.ArgumentParser(description="SPHEREx HiPS All-Sky Builder (hasta orden 7)")
    parser.add_argument("--max-order", type=int, default=7, help="Orden maximo a generar (por defecto 7)")
    parser.add_argument("--workers", type=int, default=24, help="Hilos concurrentes de descarga y compresion")
    parser.add_argument("--force", action="store_true", help="Sobrescribir baldosas existentes")
    args = parser.parse_args()

    max_order = min(7, max(0, args.max_order))
    hips_store.ensure_properties()
    print(f"Iniciando generador HiPS de SPHEREx DR1 hasta orden {max_order} con {args.workers} workers", flush=True)

    # Total de baldosas hasta max_order: sum(12 * 4^k for k in 0..max_order)
    grand_total = sum(12 * (4 ** k) for k in range(max_order + 1))
    accumulated_done = 0

    # Estrategia de optimizacion de rendimiento:
    # 1. Si max_order >= 5: Construir primero Orden 5 (capa base nativa en S3)
    if max_order >= 5:
        build_order(5, max_workers=args.workers, force=args.force, grand_done_offset=accumulated_done, grand_total=grand_total)
        accumulated_done += 12 * (4 ** 5)

        # 2. Descender progresivamente de orden 4 a 0 (100% calculo local en milisegundos)
        for o in range(4, -1, -1):
            build_order(o, max_workers=args.workers, force=args.force, grand_done_offset=accumulated_done, grand_total=grand_total)
            accumulated_done += 12 * (4 ** o)

        # 3. Generar Allsky.png
        hips_store.get_allsky_png()
        print("Mapa Allsky.png generado con exito.", flush=True)

        # 4. Escalar progresivamente a ordenes superiores (6 y 7)
        for o in range(6, max_order + 1):
            build_order(o, max_workers=args.workers, force=args.force, grand_done_offset=accumulated_done, grand_total=grand_total)
            accumulated_done += 12 * (4 ** o)
    else:
        # Si max_order < 5, procesar directamente
        for o in range(max_order, -1, -1):
            build_order(o, max_workers=args.workers, force=args.force, grand_done_offset=accumulated_done, grand_total=grand_total)
            accumulated_done += 12 * (4 ** o)
        hips_store.get_allsky_png()

    report_progress(max_order, 12 * (4 ** max_order), 12 * (4 ** max_order), grand_total, grand_total, status="finished")
    print(f"Construccion de encuesta HiPS de SPHEREx DR1 completada con exito hasta orden {max_order}.", flush=True)


if __name__ == "__main__":
    main()
