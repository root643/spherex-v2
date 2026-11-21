"""Pruebas de la lectura parcial de FITS por rangos (solo numpy, sin red).
Ejecutar:  python3 tests/test_s3fits.py

Construye a mano un MEF con la misma estructura que un level2 de SPHEREx
(primario, IMAGE, FLAGS comprimido, VARIANCE, ZODI, EPSF, WCS-WAVE) y comprueba que
s3fits lo recorre y devuelve exactamente las filas pedidas.
"""
import os, sys
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server", "backend"))
import s3fits

B = 2880


def card(key, val, comment=""):
    if isinstance(val, bool):
        v = f"{'T' if val else 'F':>20}"
    elif isinstance(val, str):
        v = "'" + val.replace("'", "''").ljust(8) + "'"
    else:
        v = f"{val:>20}"
    s = f"{key:<8}= {v}"
    if comment:
        s += " / " + comment
    return s.ljust(80)[:80]


def hdu(cards, data=b""):
    text = "".join(cards) + "END".ljust(80)
    text = text.ljust(s3fits.padded(len(text)))
    return text.encode("ascii") + data + b"\0" * (-len(data) % B)


def image_hdu(name, arr, extra=()):
    bitpix = {"f4": -32, "i4": 32, "f8": -64, "i2": 16}[arr.dtype.str[1:]]
    cards = [card("XTENSION", "IMAGE"), card("BITPIX", bitpix), card("NAXIS", arr.ndim)]
    cards += [card(f"NAXIS{i + 1}", n) for i, n in enumerate(arr.shape[::-1])]
    cards += [card("PCOUNT", 0), card("GCOUNT", 1), card("EXTNAME", name), *extra]
    return hdu(cards, arr.astype(arr.dtype.newbyteorder(">")).tobytes())


def build(packed_len=256 * 7, extra_cards=0):
    rng = np.random.default_rng(1)
    nx, ny = 50, 40
    img = rng.normal(size=(ny, nx)).astype("f4")
    flags = rng.integers(0, 2**21, size=(ny, nx)).astype("i4")
    var = rng.random((ny, nx)).astype("f4")
    zodi = rng.random((ny, nx)).astype("f4")
    psf = rng.random((5, 11, 11)).astype(">f4")
    # cabecera primaria larga (2 bloques) y cabecera IMAGE con muchas claves tipo SIP (3 bloques)
    prim = [card("SIMPLE", True), card("BITPIX", 8), card("NAXIS", 0), card("EXTEND", True)]
    prim += [card(f"KEY{i}", i, "relleno") for i in range(40)]
    sip = [card("CTYPE1", "RA---TAN-SIP"), card("CRPIX1", 1020.5), card("CRVAL1", 269.45),
           card("OBSERVER", "O'Brien / JPL", "comilla y barra"), card("CDELT1", -1.7083e-3)]
    sip += [card(f"A_{i}_{j}", 1.5e-7 * (i + j)) for i in range(8) for j in range(8)]
    sip += ["COMMENT " + "x" * 72, "HISTORY = no es una clave".ljust(80)]
    sip += [card(f"X{i}", i) for i in range(extra_cards)]
    table = bytes(range(256)) * 12            # tabla binaria ficticia: 3072 bytes
    wave = hdu([card("XTENSION", "BINTABLE"), card("BITPIX", 8), card("NAXIS", 2), card("NAXIS1", 1024),
                card("NAXIS2", 2), card("PCOUNT", 1024), card("GCOUNT", 1), card("TFIELDS", 1),
                card("EXTNAME", "WCS-WAVE")], table)
    # como en los ficheros reales: FLAGS comprimido por tiles (tabla binaria con ZIMAGE) y EPSF en tabla
    packed = (bytes(reversed(range(256))) * (packed_len // 256 + 1))[:packed_len]
    cflags = hdu([card("XTENSION", "BINTABLE"), card("BITPIX", 8), card("NAXIS", 2), card("NAXIS1", 8),
                  card("NAXIS2", ny), card("PCOUNT", len(packed) - 8 * ny), card("GCOUNT", 1),
                  card("TFIELDS", 1), card("ZIMAGE", True), card("ZBITPIX", 32), card("ZNAXIS", 2),
                  card("ZNAXIS1", nx), card("ZNAXIS2", ny), card("EXTNAME", "FLAGS")], packed)
    epsf = hdu([card("XTENSION", "BINTABLE"), card("BITPIX", 8), card("NAXIS", 2), card("NAXIS1", 605),
                card("NAXIS2", 4), card("PCOUNT", 0), card("GCOUNT", 1), card("TFIELDS", 1),
                card("EXTNAME", "EPSF")], psf.tobytes())
    blob = (hdu(prim) + image_hdu("IMAGE", img, sip) + cflags + image_hdu("VARIANCE", var)
            + image_hdu("ZODI", zodi) + epsf + wave)
    return blob, {"IMAGE": img, "VARIANCE": var, "ZODI": zodi}, table


class Remote:
    """Simula el bucket: sirve rangos de bytes y cuenta las peticiones."""
    def __init__(self, blob):
        self.blob, self.calls, self.bytes = blob, 0, 0

    def get(self, a, b):
        assert 0 <= a <= b, (a, b)
        self.calls += 1
        out = self.blob[a:b + 1]
        self.bytes += len(out)
        return out


def test_parse_cards():
    c = s3fits.parse_cards(card("SIMPLE", True) + card("BITPIX", -32) + card("CDELT1", -1.7083e-3)
                           + card("OBSERVER", "O'Brien / JPL", "c") + card("EXTNAME", "IMAGE", "nombre")
                           + "CRVAL1  =       2.6945D+02 / exponente D".ljust(80)
                           + "COMMENT   BITPIX  = 99".ljust(80) + "END".ljust(80) + card("BITPIX", 8))
    assert c["SIMPLE"] is True and c["BITPIX"] == -32 and abs(c["CDELT1"] + 1.7083e-3) < 1e-12
    assert c["OBSERVER"] == "O'Brien / JPL" and c["EXTNAME"] == "IMAGE"
    assert abs(c["CRVAL1"] - 269.45) < 1e-9


def test_walk_layout():
    blob, arrs, table = build()
    r = Remote(blob)
    exts = s3fits.walk(r.get, len(blob))
    assert list(exts) == ["PRIMARY", "IMAGE", "FLAGS", "VARIANCE", "ZODI", "EPSF", "WCS-WAVE"], list(exts)
    assert exts["PRIMARY"]["hsize"] == 2 * B and exts["PRIMARY"]["dsize"] == 0
    assert exts["IMAGE"]["hsize"] == 3 * B and exts["IMAGE"]["dsize"] == 50 * 40 * 4
    assert exts["EPSF"]["dsize"] == 605 * 4 and exts["FLAGS"]["dsize"] == 256 * 7
    assert not s3fits.is_plain_image(s3fits.parse_cards(exts["FLAGS"]["text"]))
    w = exts["WCS-WAVE"]
    assert w["dsize"] == 1024 * 2 + 1024 and blob[w["dataoff"]:w["dataoff"] + w["dsize"]] == table
    assert w["dataoff"] + s3fits.padded(w["dsize"]) == len(blob)
    # solo cabeceras: una peticion por HDU y ni un byte de datos de imagen de mas
    assert r.calls == 7, r.calls
    assert r.bytes < 7 * 8 * B, r.bytes


def test_rows_match():
    blob, arrs, _ = build()
    r = Remote(blob)
    exts = s3fits.walk(r.get, len(blob))
    r.calls = 0
    for name, arr in arrs.items():
        for y0, y1 in ((0, 40), (7, 19), (39, 40)):
            got = s3fits.rows(r.get, exts[name], y0, y1)
            assert got.dtype == arr.dtype and got.dtype.isnative, (name, got.dtype)
            assert np.array_equal(got, arr[y0:y1]), (name, y0, y1)
    assert r.calls == 9           # una peticion por lectura de filas
    for bad in ((-1, 5), (5, 5), (30, 41)):
        try:
            s3fits.rows(r.get, exts["IMAGE"], *bad)
            raise AssertionError("deberia fallar")
        except ValueError:
            pass
    try:
        s3fits.rows(r.get, exts["FLAGS"], 0, 2)   # comprimido: no se lee por filas
        raise AssertionError("deberia fallar")
    except ValueError:
        pass


def test_unsigned_flags_and_compressed():
    # enteros sin signo al estilo FITS: int32 con BZERO = 2^31
    true = np.array([[0, 1, 2**31 + 5, 2**32 - 1]], dtype=np.uint32)
    stored = (true.astype(np.int64) - 2**31).astype("i4")
    blob = hdu([card("SIMPLE", True), card("BITPIX", 8), card("NAXIS", 0)]) + image_hdu(
        "FLAGS", stored, [card("BSCALE", 1), card("BZERO", 2147483648)])
    r = Remote(blob)
    exts = s3fits.walk(r.get, len(blob))
    got = s3fits.rows(r.get, exts["FLAGS"], 0, 1)
    assert np.array_equal(got, true.astype(np.int64)), got
    assert not s3fits.is_plain_image({"XTENSION": "BINTABLE", "ZIMAGE": True, "NAXIS": 2, "BITPIX": 8})
    assert s3fits.is_plain_image(s3fits.parse_cards(exts["FLAGS"]["text"]))


def test_truncated_and_primary():
    blob, _, _ = build()
    r = Remote(blob[:3 * B + 100])            # fichero cortado a mitad de la cabecera IMAGE
    try:
        s3fits.walk(r.get, len(blob))
        raise AssertionError("deberia fallar")
    except ValueError:
        pass
    p = s3fits.minimal_primary()
    assert len(p) == B and s3fits.parse_cards(p) == {"SIMPLE": True, "BITPIX": 8, "NAXIS": 0, "EXTEND": True}


def test_layout_prefetch_like_upstream():
    """Igual que upstream._layout: la primera lectura grande sirve las primeras cabeceras."""
    blob, _, _ = build()
    r = Remote(blob)
    head = r.get(0, 12 * B - 1)
    get = lambda a, b: head[a:b + 1] if b < len(head) else r.get(a, b)
    exts = s3fits.walk(get, len(blob))
    assert exts == s3fits.walk(Remote(blob).get, len(blob))
    assert r.calls < 7, r.calls


class Many:
    """get_many simulado: cuenta tandas y peticiones."""
    def __init__(self, blob):
        self.blob, self.rounds, self.calls = blob, 0, 0

    def __call__(self, ranges):
        self.rounds += 1
        self.calls += len(ranges)
        return [self.blob[a:b + 1] for a, b in ranges]


def test_walk_fast():
    a, _, _ = build()
    tmpl = s3fits.template_of(s3fits.walk(Remote(a).get, len(a)))
    assert [t[0] for t in tmpl] == ["PRIMARY", "IMAGE", "FLAGS", "VARIANCE", "ZODI", "EPSF", "WCS-WAVE"]
    # otro fichero del mismo detector: misma estructura, FLAGS comprimido de otro tamano
    for plen in (256 * 7, 5000, 123457, 2880 * 3):
        b, _, _ = build(packed_len=plen)
        m = Many(b)
        res = s3fits.walk_fast(m, tmpl)
        assert res is not None, plen
        exts, end = res
        assert end == len(b)
        assert exts == s3fits.walk(Remote(b).get, len(b)), plen
        # 2 tandas: (primario+IMAGE juntos, FLAGS) y (VARIANCE, ZODI, EPSF, WCS-WAVE)
        assert m.rounds == 2 and m.calls == 6, (m.rounds, m.calls)
    # estructura distinta (cabecera IMAGE con un bloque mas): debe rechazar, no inventar
    c, _, _ = build(extra_cards=40)
    assert s3fits.walk(Remote(c).get, len(c))["IMAGE"]["hsize"] != tmpl[1][1]
    assert s3fits.walk_fast(Many(c), tmpl) is None
    # fichero cortado y plantillas inservibles
    assert s3fits.walk_fast(Many(a[:len(a) // 2]), tmpl) is None
    assert s3fits.walk_fast(Many(a), [t for t in tmpl if t[0] != "FLAGS"]) is None
    bad = [list(t) for t in tmpl]
    bad[4][2] += 2880                              # ZODI con otro tamano
    assert s3fits.walk_fast(Many(a), bad) is None


def test_fits_tile_compression_remote():
    """Prueba el soporte de FITS Tile Compression remoto (fpack / CompImageHDU) en s3fits."""
    import io
    from astropy.io import fits

    nx, ny = 128, 128
    data = np.arange(ny * nx, dtype=np.float32).reshape(ny, nx)
    hdr = fits.Header()
    hdr["EXTNAME"] = "COMP_IMAGE"
    comp_hdu = fits.CompImageHDU(data, header=hdr, compression_type="RICE_1", tile_shape=(32, 32))
    hdul = fits.HDUList([fits.PrimaryHDU(), comp_hdu])
    bio = io.BytesIO()
    hdul.writeto(bio)
    raw = bio.getvalue()

    remote = Remote(raw)
    exts = s3fits.walk(remote.get, len(raw))
    assert "COMP_IMAGE" in exts

    cards = s3fits.parse_cards(exts["COMP_IMAGE"]["text"])
    assert s3fits.is_compressed_image(cards) is True
    assert s3fits.is_plain_image(cards) is False

    # Lectura selectiva de una caja interna [20:50, 30:70]
    remote_sec = Remote(raw)
    sub = s3fits.read_compressed_section(remote_sec.get, exts["COMP_IMAGE"], 20, 50, 30, 70, cards)
    assert sub.shape == (30, 40)
    assert np.allclose(sub, data[20:50, 30:70])

    # Lectura de filas completas sin especificar columnas (x0=None, x1=None)
    sub_all_cols = s3fits.read_compressed_section(remote_sec.get, exts["COMP_IMAGE"], 10, 20)
    assert sub_all_cols.shape == (10, nx)
    assert np.allclose(sub_all_cols, data[10:20, :])

    # Verificar que solo se leyeron teselas de forma selectiva
    assert remote_sec.bytes < len(raw)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
    print("todo correcto")
