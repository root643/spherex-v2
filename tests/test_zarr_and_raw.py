"""Pruebas del almacenamiento Cloud-Native en Zarr y streaming de recortes float32 (ZFP/Blosc)."""
import os
import sys
import tempfile
import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server", "backend"))
import zarr_store
from main import app


def test_compress_and_decompress_float32():
    data = np.linspace(-5.0, 15.0, 64 * 64, dtype=np.float32).reshape(64, 64)

    # 1. Compresion Raw
    raw_bytes, codec = zarr_store.compress_float32(data, method="raw")
    assert codec == "raw"
    restored = zarr_store.decompress_float32(raw_bytes, (64, 64), codec)
    assert np.array_equal(data, restored)

    # 2. Compresion Blosc-Zstd (o fallback)
    blosc_bytes, codec_b = zarr_store.compress_float32(data, method="blosc")
    assert codec_b in ("blosc-zstd", "raw")
    restored_b = zarr_store.decompress_float32(blosc_bytes, (64, 64), codec_b)
    assert np.allclose(data, restored_b, atol=1e-6)
    # Comprobar que descompresion pasando el nombre del metodo "blosc" tambien funciona
    restored_b2 = zarr_store.decompress_float32(blosc_bytes, (64, 64), method="blosc")
    assert np.allclose(data, restored_b2, atol=1e-6)

    # 3. Compresion ZFP (o fallback a blosc-zstd)
    zfp_bytes, codec_z = zarr_store.compress_float32(data, method="zfp", tolerance=1e-4)
    assert codec_z in ("zfp", "blosc-zstd", "raw")
    restored_z = zarr_store.decompress_float32(zfp_bytes, (64, 64), codec_z)
    assert np.allclose(data, restored_z, atol=1e-3)
    # Comprobar que descompresion pasando el nombre del metodo "zfp" tambien funciona sin fallar
    restored_z2 = zarr_store.decompress_float32(zfp_bytes, (64, 64), method="zfp")
    assert np.allclose(data, restored_z2, atol=1e-3)


def test_zarr_cube_operations():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "cube_test.zarr")
        n_ch, ny, nx = 102, 64, 64
        cube_arr = np.random.rand(n_ch, ny, nx).astype(np.float32)
        wavelengths = [round(0.75 + i * (4.25 / 102.0), 4) for i in range(102)]
        wcs_info = {"CRVAL1": "269.45", "CRVAL2": "4.69", "CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN"}

        saved_path = zarr_store.save_zarr_cube(path, cube_arr, 269.45, 4.69, 300.0, wavelengths, wcs_info)
        assert os.path.exists(saved_path)

        # Apertura del cubo
        root = zarr_store.open_zarr_cube(saved_path)
        assert root is not None
        assert "data" in root
        assert root["data"].shape == (102, 64, 64)
        assert root.attrs["ra"] == 269.45
        assert root.attrs["n_channels"] == 102

        # Extraccion instantanea de espectro punto a punto
        spec_res = zarr_store.extract_spectrum_from_zarr(saved_path, 32, 32)
        assert spec_res is not None
        assert spec_res["x"] == 32 and spec_res["y"] == 32
        assert len(spec_res["spectrum"]) == 102
        first_pt = spec_res["spectrum"][0]
        assert abs(first_pt["flux"] - float(cube_arr[0, 32, 32])) < 1e-6

        # Extraccion de plano 2D
        plane = zarr_store.extract_plane_from_zarr(saved_path, 1)
        assert plane.shape == (64, 64)
        assert np.allclose(plane, cube_arr[0, :, :])


def test_api_hips_surveys_and_cube_info():
    client = TestClient(app)

    # HiPS surveys endpoint
    resp = client.get("/api/v1/hips/surveys")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert len(data["surveys"]) >= 3
    survey_ids = [s["id"] for s in data["surveys"]]
    assert "CDS/P/allWISE/color" in survey_ids

    # Cube info endpoint
    resp_info = client.get("/api/v1/cube/zarr/info?ra=269.45&dec=4.69&fov=300")
    assert resp_info.status_code == 200
    info_data = resp_info.json()
    assert "cube_id" in info_data
    assert info_data["format"] == "zarr-v3"


def test_api_cutout_raw(monkeypatch):
    client = TestClient(app)

    fake_arr = np.linspace(1.0, 50.0, 64 * 64, dtype=np.float32).reshape(64, 64)
    fake_meta = {"n": 5, "size": 64, "scale": 1.5, "date": "2025-05-01", "mjd": 60800.5}

    monkeypatch.setattr("main.get_exposures", lambda ra, dec: [{"epoch": 0, "detector": 4, "uri": "test.fits"}])
    monkeypatch.setattr("main.build_array", lambda *args, **kwargs: (fake_arr, fake_meta))

    # 1. Format raw
    r_raw = client.get("/api/v1/cutout/raw?ra=269.45&dec=4.69&fov=120&band=D4&format=raw")
    assert r_raw.status_code == 200
    assert r_raw.headers["X-Spherex-Dtype"] == "float32"
    assert r_raw.headers["X-Spherex-Shape"] == "64,64"
    restored = np.frombuffer(r_raw.content, dtype=np.float32).reshape(64, 64)
    assert np.allclose(restored, fake_arr)

    # 2. Format blosc
    r_blosc = client.get("/api/v1/cutout/raw?ra=269.45&dec=4.69&fov=120&band=D4&format=blosc")
    assert r_blosc.status_code == 200
    assert r_blosc.headers["X-Spherex-Codec"] in ("blosc-zstd", "raw")

    # 3. Format zfp
    r_zfp = client.get("/api/v1/cutout/raw?ra=269.45&dec=4.69&fov=120&band=D4&format=zfp&tolerance=0.001")
    assert r_zfp.status_code == 200
    assert r_zfp.headers["X-Spherex-Codec"] in ("zfp", "blosc-zstd", "raw")

    # 4. Format fits
    import io
    from astropy.io import fits
    r_fits = client.get("/api/v1/cutout/raw?ra=269.45&dec=4.69&fov=120&band=D4&format=fits")
    assert r_fits.status_code == 200
    assert r_fits.headers["Content-Type"] == "application/fits"
    assert len(r_fits.content) > 2880
    with fits.open(io.BytesIO(r_fits.content)) as hdul:
        assert len(hdul) == 1
        assert hdul[0].data.shape == (64, 64)
        assert np.allclose(hdul[0].data, fake_arr)
        assert "CRVAL1" in hdul[0].header


def test_api_zarr_cube_build_and_spectrum(monkeypatch):
    """Comprueba la construccion del cubo 3D y la posterior extraccion de espectro sin ceros fantasma."""
    client = TestClient(app)

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr("zarr_store.ZARR_DIR", td)
        monkeypatch.setattr("main.get_exposures", lambda ra, dec: [{"epoch": 0, "detector": 1, "uri": "test.fits"}])

        def mock_build_array(ra, dec, fov, det, lmin, lmax, exps, method, cap, res):
            # Devolver array coherente con la resolucion solicitada
            import upstream
            if res == "low":
                n, _ = upstream.grid_for(fov, min_scale=upstream.NATIVE_SCALE)
            else:
                n, _ = upstream.grid_for(fov)
            arr = np.full((n, n), float(det * 10.0), dtype=np.float32)
            meta = {"n": 1, "size": n, "scale": 3.0, "date": "2025-05-01", "mjd": 60800.0}
            return arr, meta

        monkeypatch.setattr("main.build_array", mock_build_array)

        # Construir cubo Zarr en baja resolucion
        r_build = client.post("/api/v1/cube/zarr/build?ra=269.45&dec=4.69&fov=120&res=low")
        assert r_build.status_code == 200
        bdata = r_build.json()
        assert bdata["status"] == "created"
        assert bdata["shape"][0] == 102

        # Consultar espectro punto a punto
        r_spec = client.get(f"/api/v1/cube/zarr/spectrum?ra=269.45&dec=4.69&fov=120&x=5&y=5")
        assert r_spec.status_code == 200
        sdata = r_spec.json()
        assert sdata["n_channels"] == 102
        # Verificar que los canales tienen flujos reales y no ceros por errores de broadcast
        fluxes = [p["flux"] for p in sdata["spectrum"]]
        assert any(f is not None and f > 0 for f in fluxes)
