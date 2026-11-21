"""Lectura parcial de ficheros FITS remotos por rangos de bytes (solo numpy).

Los level2 de SPHEREx del bucket publico de AWS pesan ~71 MB y un recorte solo
necesita unas pocas filas de cada HDU. Este modulo localiza los HDU leyendo
unicamente las cabeceras y despues trae las filas pedidas de cada imagen.

No depende de astropy ni de la red: recibe una funcion get_range(a, b) que
devuelve los bytes [a, b] (ambos incluidos), asi que se puede probar en local.
"""
from __future__ import annotations

import io
from typing import Callable

import numpy as np

BLOCK = 2880
CARD = 80
_DTYPES = {8: "u1", 16: ">i2", 32: ">i4", 64: ">i8", -32: ">f4", -64: ">f8"}

GetRange = Callable[[int, int], bytes]


def _value(text: str):
    s = text.strip()
    if s.startswith("'"):
        out, i = [], 1
        while i < len(s):
            if s[i] == "'":
                if i + 1 < len(s) and s[i + 1] == "'":   # comilla escapada
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(s[i])
            i += 1
        return "".join(out).rstrip()
    s = s.split("/", 1)[0].strip()
    if s == "T":
        return True
    if s == "F":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s.replace("D", "E").replace("d", "e"))
    except ValueError:
        return s


def parse_cards(raw: bytes | str) -> dict:
    """Cabecera FITS (cards de 80 caracteres seguidos) -> {clave: valor}."""
    text = raw.decode("latin1") if isinstance(raw, (bytes, bytearray)) else raw
    cards: dict = {}
    for i in range(0, len(text) - CARD + 1, CARD):
        card = text[i:i + CARD]
        key = card[:8].strip()
        if key == "END":
            break
        if not key or card[8:10] != "= ":
            continue   # COMMENT, HISTORY, CONTINUE, HIERARCH...
        cards.setdefault(key, _value(card[10:]))
    return cards


def _end_offset(buf: bytes) -> int:
    """Posicion del card END dentro de buf, o -1 si aun no ha llegado."""
    for i in range(0, len(buf) - CARD + 1, CARD):
        if buf[i:i + 8] == b"END     " and not buf[i + 8:i + CARD].strip():
            return i
    return -1


def read_header(get_range: GetRange, off: int, total: int, chunk_blocks: int = 4) -> tuple[str, int]:
    """Lee la cabecera que empieza en off. Devuelve (texto de los cards, bytes que ocupa)."""
    buf = b""
    while True:
        a = off + len(buf)
        if a >= total:
            raise ValueError("cabecera FITS sin END antes del final del fichero")
        part = get_range(a, min(a + chunk_blocks * BLOCK, total) - 1)
        if not part:
            raise ValueError("lectura vacia al leer una cabecera FITS")
        buf += part
        end = _end_offset(buf)
        if end >= 0:
            hsize = (end + CARD + BLOCK - 1) // BLOCK * BLOCK
            return buf[:end + CARD].decode("latin1"), hsize
        if len(buf) > 200 * BLOCK:
            raise ValueError("cabecera FITS demasiado larga o corrupta")


def data_size(cards: dict) -> int:
    """Bytes de datos de un HDU, sin el relleno a multiplo de 2880."""
    naxis = int(cards.get("NAXIS", 0) or 0)
    if naxis == 0:
        return 0
    nelem = 1
    for ax in range(1, naxis + 1):
        nelem *= int(cards[f"NAXIS{ax}"])
    gcount = int(cards.get("GCOUNT", 1) or 1)
    pcount = int(cards.get("PCOUNT", 0) or 0)
    return abs(int(cards["BITPIX"])) // 8 * gcount * (pcount + nelem)


def padded(n: int) -> int:
    return (n + BLOCK - 1) // BLOCK * BLOCK


def walk(get_range: GetRange, total: int) -> dict:
    """Recorre todos los HDU leyendo solo cabeceras.

    Devuelve {nombre: {text, hdroff, hsize, dataoff, dsize}}; el HDU primario
    se llama "PRIMARY" y el resto por su EXTNAME.
    """
    exts: dict[str, dict] = {}
    off, index = 0, 0
    while off < total:
        text, hsize = read_header(get_range, off, total, 8 if index == 0 else 4)
        cards = parse_cards(text)
        if index == 0 and cards.get("SIMPLE") is not True:
            raise ValueError("no es un fichero FITS")
        dsize = data_size(cards)
        name = "PRIMARY" if index == 0 else str(cards.get("EXTNAME", f"HDU{index}")).strip()
        exts.setdefault(name, {"text": text, "hdroff": off, "hsize": hsize,
                               "dataoff": off + hsize, "dsize": dsize})
        off += hsize + padded(dsize)
        index += 1
    return exts


def template_of(exts: dict) -> list:
    """Plantilla [[nombre, bytes de cabecera, bytes de datos], ...] en el orden del fichero."""
    order = sorted(exts.items(), key=lambda kv: kv[1]["hdroff"])
    return [[name, e["hsize"], e["dsize"]] for name, e in order]


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int, list[int]]]:
    """Une rangos contiguos (p. ej. primario + cabecera de IMAGE) en una sola lectura."""
    out: list[tuple[int, int, list[int]]] = []
    for i, (a, b) in enumerate(ranges):
        if out and out[-1][1] + 1 == a:
            pa, _, idx = out[-1]
            out[-1] = (pa, b, idx + [i])
        else:
            out.append((a, b, [i]))
    return out


def _read_many(get_many, ranges: list[tuple[int, int]]) -> list[bytes]:
    merged = _merge(ranges)
    blobs = get_many([(a, b) for a, b, _ in merged])
    out: list[bytes] = [b""] * len(ranges)
    for (a, _, idx), blob in zip(merged, blobs):
        for i in idx:
            ra, rb = ranges[i]
            out[i] = blob[ra - a:rb - a + 1]
    return out


def _check(buf: bytes, index: int, name: str, hsize: int) -> dict | None:
    """Comprueba que buf es exactamente la cabecera esperada; devuelve sus cards o None."""
    if len(buf) != hsize or buf[:8] != (b"SIMPLE  " if index == 0 else b"XTENSION"):
        return None
    end = _end_offset(buf)
    if end < 0 or (end + CARD + BLOCK - 1) // BLOCK * BLOCK != hsize:
        return None
    cards = parse_cards(buf[:end + CARD])
    got = "PRIMARY" if index == 0 else str(cards.get("EXTNAME", f"HDU{index}")).strip()
    return cards if got == name else None


def walk_fast(get_many, template: list, variable: str = "FLAGS") -> tuple[dict, int] | None:
    """Como walk() pero en dos tandas de lecturas en paralelo, a partir de una plantilla.

    Todos los level2 de un mismo detector tienen la misma estructura salvo el tamano
    del HDU comprimido (variable). Con la plantilla de un fichero ya recorrido se
    adivina donde cae cada cabecera: primero las que hay hasta el HDU variable, y
    con su tamano real, el resto. Cada cabecera leida se comprueba; si algo no
    cuadra devuelve None y el llamante hace el recorrido normal.

    get_many([(a, b), ...]) devuelve los bytes de cada rango (en paralelo).
    Devuelve (exts, final), donde final debe coincidir con el tamano del fichero.
    """
    names = [t[0] for t in template]
    if variable not in names or names[0] != "PRIMARY":
        return None
    iv = names.index(variable)
    exts: dict[str, dict] = {}

    def stage(first: int, last: int, off: int) -> int | None:
        offs = []
        for name, hs, ds in template[first:last]:
            offs.append(off)
            off += hs + padded(ds)
        bufs = _read_many(get_many, [(o, o + template[first + k][1] - 1) for k, o in enumerate(offs)])
        end = None
        for k, (o, buf) in enumerate(zip(offs, bufs)):
            name, hs, ds = template[first + k]
            cards = _check(buf, first + k, name, hs)
            if cards is None:
                return None
            real = data_size(cards)
            if real != ds and name != variable:
                return None
            exts[name] = {"text": buf[:_end_offset(buf) + CARD].decode("latin1"), "hdroff": o,
                          "hsize": hs, "dataoff": o + hs, "dsize": real}
            end = o + hs + padded(real)
        return end

    end = stage(0, iv + 1, 0)
    if end is None:
        return None
    if iv + 1 < len(template):
        end = stage(iv + 1, len(template), end)
        if end is None:
            return None
    return exts, end


def is_plain_image(cards: dict) -> bool:
    """True si el HDU es una imagen 2D sin comprimir (legible por filas)."""
    if cards.get("ZIMAGE") is True or str(cards.get("XTENSION", "IMAGE")).strip() not in ("IMAGE", ""):
        return False
    return int(cards.get("NAXIS", 0) or 0) == 2 and int(cards.get("BITPIX", 0)) in _DTYPES


def is_compressed_image(cards: dict) -> bool:
    """True si el HDU es una imagen FITS comprimida por teselas (fpack / CompImageHDU)."""
    zimg = cards.get("ZIMAGE")
    return bool(zimg is True or zimg in ("T", "t", "True", "true", 1))


class CompImageRangeStream(io.RawIOBase):
    """Fichero virtual seekable sobre rangos HTTP de un HDU comprimido FITS.

    Antepone un HDU primario minimo (2880 bytes) para que astropy.io.fits.open
    lo reconozca como un MEF FITS estandar y acceda selectivamente a las teselas
    del HDU comprimido mediante HTTP Range requests sin descargar el resto.
    Utiliza una cache de bloques indexada para no volver a pedir rangos ya leidos.
    """
    def __init__(self, get_range: GetRange, ext: dict, block_size: int = 4096):
        self._get_range = get_range
        self._ext = ext
        self._hdroff = int(ext["hdroff"])
        dsize_pad = padded(int(ext["dsize"]))
        self._ext_len = int(ext["hsize"]) + dsize_pad
        self._primary = minimal_primary()
        self._pri_len = len(self._primary)
        self._total = self._pri_len + self._ext_len
        self._pos = 0
        self._block_size = block_size
        self._blocks: dict[int, bytes] = {}

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._total + offset
        else:
            raise ValueError(f"whence invalido: {whence}")
        self._pos = max(0, self._pos)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def readinto(self, b) -> int:
        if self._pos >= self._total:
            return 0
        want = min(len(b), self._total - self._pos)
        out = bytearray()
        cur_pos = self._pos

        if cur_pos < self._pri_len:
            n_pri = min(want, self._pri_len - cur_pos)
            out.extend(self._primary[cur_pos:cur_pos + n_pri])
            cur_pos += n_pri
            want -= n_pri

        if want > 0:
            rel_start = cur_pos - self._pri_len
            rel_end = rel_start + want - 1
            b_first = rel_start // self._block_size
            b_last = rel_end // self._block_size
            for bi in range(b_first, b_last + 1):
                if bi not in self._blocks:
                    boff = bi * self._block_size
                    bstop = min(self._ext_len, boff + self._block_size) - 1
                    if bstop >= boff:
                        chunk = self._get_range(self._hdroff + boff, self._hdroff + bstop)
                        self._blocks[bi] = chunk
                    else:
                        self._blocks[bi] = b""
            for bi in range(b_first, b_last + 1):
                boff = bi * self._block_size
                blk = self._blocks[bi]
                start_in_blk = max(0, rel_start - boff)
                end_in_blk = min(len(blk), rel_end - boff + 1)
                if end_in_blk > start_in_blk:
                    out.extend(blk[start_in_blk:end_in_blk])

        b[:len(out)] = out
        self._pos += len(out)
        return len(out)


def read_compressed_section(get_range: GetRange, ext: dict, y0: int, y1: int,
                            x0: int | None = None, x1: int | None = None,
                            cards: dict | None = None) -> np.ndarray:
    """Lee y descomprime selectivamente una region [y0:y1, x0:x1] de un HDU comprimido (fpack/CompImageHDU).

    Solo transfiere por HTTP Range los bytes de las teselas que intersecan la region,
    reduciendo el ancho de banda necesario hasta en un 99%.
    """
    from astropy.io import fits
    import warnings
    from astropy.wcs import FITSFixedWarning

    stream = CompImageRangeStream(get_range, ext)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        warnings.simplefilter("ignore", RuntimeWarning)
        with fits.open(stream, memmap=False) as hdul:
            comp_hdu = hdul[1]
            if x0 is not None and x1 is not None:
                sec = comp_hdu.section[y0:y1, x0:x1]
            else:
                sec = comp_hdu.section[y0:y1, :]
            return np.array(sec)


def rows(get_range: GetRange, ext: dict, y0: int, y1: int, cards: dict | None = None) -> np.ndarray:
    """Filas [y0, y1) completas de un HDU imagen 2D, ya en orden de bytes nativo.

    FITS guarda los datos en big-endian y por filas, asi que un bloque de filas
    es un unico rango de bytes contiguo. Aplica BSCALE/BZERO si los hay.
    """
    cards = cards if cards is not None else parse_cards(ext["text"])
    if not is_plain_image(cards):
        raise ValueError("el HDU no es una imagen 2D sin comprimir")
    n1, n2 = int(cards["NAXIS1"]), int(cards["NAXIS2"])
    if not 0 <= y0 < y1 <= n2:
        raise ValueError(f"filas fuera de la imagen: {y0}-{y1} de {n2}")
    dtype = np.dtype(_DTYPES[int(cards["BITPIX"])])
    row_bytes = n1 * dtype.itemsize
    a = ext["dataoff"] + y0 * row_bytes
    want = (y1 - y0) * row_bytes
    data = get_range(a, a + want - 1)
    if len(data) != want:
        raise ValueError(f"rango incompleto: {len(data)} de {want} bytes")
    arr = np.frombuffer(data, dtype=dtype).reshape(y1 - y0, n1)
    bscale, bzero = cards.get("BSCALE", 1), cards.get("BZERO", 0)
    if bscale in (1, 1.0) and bzero in (0, 0.0):
        return arr.astype(dtype.newbyteorder("="))
    if dtype.kind in "iu" and float(bscale).is_integer() and float(bzero).is_integer():
        return arr.astype(np.int64) * int(bscale) + int(bzero)   # p. ej. enteros sin signo
    return arr.astype(np.float64) * float(bscale) + float(bzero)


def minimal_primary() -> bytes:
    """HDU primario vacio, para anteponerlo a una extension suelta y que sea un FITS valido."""
    def card(key: str, val: str) -> str:
        return f"{key:<8}= {val:>20}".ljust(CARD)
    text = card("SIMPLE", "T") + card("BITPIX", "8") + card("NAXIS", "0") + card("EXTEND", "T") + "END".ljust(CARD)
    return text.ljust(BLOCK).encode("ascii")
