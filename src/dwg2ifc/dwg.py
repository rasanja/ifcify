"""DWG binary reader: file container, bit stream, object map.

Follows the Open Design Specification for .dwg files; section numbers in comments refer to it.
"""

import struct

# magic -> release; the on-disk layout changes at each of these
VERSIONS = {
    b"AC1012": "R13",
    b"AC1014": "R14",
    b"AC1015": "R2000",
    b"AC1018": "R2004",
    b"AC1021": "R2007",
    b"AC1024": "R2010",
    b"AC1027": "R2013",
    b"AC1032": "R2018",
}
# releases sharing the R2004 container (spec chapter 4)
_R2004_CONTAINER = {"R2004", "R2010", "R2013", "R2018"}
_ORDER = {name: i for i, name in enumerate(VERSIONS.values())}


def since(release: str, first: str) -> bool:
    """True if `release` is `first` or newer."""
    return _ORDER[release] >= _ORDER[first]


def version(data: bytes) -> str:
    try:
        return VERSIONS[data[:6]]
    except KeyError:
        raise ValueError(f"not a supported DWG file (magic {data[:6]!r})") from None


# --- R2004 container -------------------------------------------------------


def _header_key() -> bytes:
    seed, out = 1, bytearray()
    for _ in range(0x6C):
        seed = (seed * 0x343FD + 0x269EC3) & 0xFFFFFFFF
        out.append((seed >> 16) & 0xFF)
    return bytes(out)


_HEADER_KEY = _header_key()


def _ext(src: bytes, i: int, base: int) -> tuple[int, int]:
    """Run length: each zero byte adds 0xFF, the first non-zero byte adds itself and ends it."""
    while src[i] == 0:
        base += 0xFF
        i += 1
    return base + src[i], i + 1


def decompress(src: bytes) -> bytearray:
    """R2004 LZ77 variant (spec 4.7)."""
    out = bytearray()
    op, i = src[0], 1
    lit = 0
    while True:
        # a 2-bit literal count of 0 means the length is in the next byte(s), unless that byte is an opcode
        if not lit and not op & 0xF0:
            lit, i = _ext(src, i, 0x12) if op == 0 else (op + 3, i)
        if lit:
            out += src[i : i + lit]
            i += lit
            op, i = src[i], i + 1
        if op == 0x11:
            return out
        if op >= 0x40:
            n = (op >> 4) - 1
            off = ((src[i] << 2) | ((op >> 2) & 3)) + 1
            lit = op & 3
            i += 1
        else:
            if op >= 0x21:
                n, base = op - 0x1E, 1
            elif op == 0x20:
                (n, i), base = _ext(src, i, 0x21), 1
            elif op >= 0x12:
                n, base = (op & 0x0F) + 2, 0x4000
            elif op == 0x10:
                (n, i), base = _ext(src, i, 9), 0x4000
            else:
                raise ValueError(f"bad compression opcode {op:#x}")
            off = ((src[i] >> 2) | (src[i + 1] << 6)) + base
            lit = src[i] & 3
            i += 2
        start = len(out) - off
        if start < 0:
            raise ValueError("compressed data points before the start of the page")
        if off >= n:
            out += out[start : start + n]
        else:  # overlapping copy repeats the last `off` bytes
            out += (out[start:] * (n // off + 1))[:n]
        if not lit:
            op, i = src[i], i + 1


def _system_page(data: bytes, addr: int, kind: int) -> bytes:
    typ, _usize, csize, ctype, _chk = struct.unpack_from("<5I", data, addr)
    if typ != kind:
        raise ValueError(f"expected system page {kind:#x} at {addr:#x}, found {typ:#x}")
    raw = data[addr + 0x14 : addr + 0x14 + csize]
    return decompress(raw) if ctype == 2 else raw


def _sections_r2004(data: bytes) -> dict[str, bytes]:
    if struct.unpack_from("<I", data, 0x18)[0] & 1:
        raise ValueError("password-protected DWG files are not supported")
    hdr = bytes(a ^ b for a, b in zip(data[0x80:0xEC], _HEADER_KEY))
    if hdr[:11] != b"AcFssFcAJMB":
        raise ValueError("corrupt DWG file header")
    page_map_addr = struct.unpack_from("<Q", hdr, 0x54)[0] + 0x100
    section_map_id = struct.unpack_from("<I", hdr, 0x5C)[0]

    # page map (4.4): page number -> file address
    pm = _system_page(data, page_map_addr, 0x41630E3B)
    pages, addr, i = {}, 0x100, 0
    while i + 8 <= len(pm):
        num, size = struct.unpack_from("<ii", pm, i)
        i += 8
        if num < 0:
            i += 16  # gap: parent, left, right, 0
        else:
            pages[num] = addr
        addr += size

    # section map (4.5)
    sm = _system_page(data, pages[section_map_id], 0x4163003B)
    out = {}
    i = 0x14
    for _ in range(struct.unpack_from("<I", sm, 0)[0]):
        size, page_count, max_size, _unk, compressed, _id, _enc = struct.unpack_from("<QIIIIII", sm, i)
        name = bytes(sm[i + 0x20 : i + 0x60]).split(b"\0")[0].decode("ascii", "replace")
        i += 0x60
        if size > page_count * max_size + max_size * 0x10000:
            raise ValueError(f"implausible size for section {name}")
        buf = bytearray(size)
        for _ in range(page_count):
            num, dsize, start = struct.unpack_from("<IIQ", sm, i)
            i += 16
            addr = pages[num]
            # masked page header (4.6): only the type is needed, the rest repeats the section map
            ptype = struct.unpack_from("<I", data, addr)[0] ^ 0x4164536B ^ (addr & 0xFFFFFFFF)
            if ptype != 0x4163043B:
                raise ValueError(f"bad data page header at {addr:#x}")
            raw = data[addr + 32 : addr + 32 + dsize]
            page = (decompress(raw) if compressed == 2 else raw)[: max(0, size - start)]
            buf[start : start + len(page)] = page
        out[name] = bytes(buf)
    return out


# --- R2007 container (spec chapter 5) ---------------------------------------

# Literal runs are stored with their bytes shuffled (5.10.1). Each entry is "count@source offset";
# a 2 or 3 byte piece is reversed and a 16 byte piece has its two halves swapped.
_LITERAL_PIECES = (
    "1@0|1@1 1@0|1@2 1@1 1@0|1@0 1@1 1@2 1@3|1@4 4@0|1@5 4@1 1@0|2@5 4@1 1@0|4@0 4@4|1@8 8@0|1@9 8@1 1@0|"
    "2@9 8@1 1@0|4@8 8@0|1@12 4@8 8@0|1@13 4@9 8@1 1@0|2@13 4@9 8@1 1@0|8@8 8@0|8@9 1@8 8@0|1@17 16@1 1@0|"
    "3@16 16@0|4@16 16@0|1@20 4@16 16@0|2@20 4@16 16@0|3@20 4@16 16@0|8@16 16@0|8@17 1@16 16@0|"
    "1@25 8@17 1@16 16@0|2@25 8@17 1@16 16@0|4@24 8@16 16@0|1@28 4@24 8@16 16@0|2@28 4@24 8@16 16@0|"
    "1@30 4@26 8@18 16@2 2@0|16@16 16@0"
)


def _literal_orders() -> list[list[int]]:
    orders = [[]]
    for pieces in _LITERAL_PIECES.split("|"):
        order = []
        for piece in pieces.split():
            n, off = map(int, piece.split("@"))
            if n in (2, 3):
                order += range(off + n - 1, off - 1, -1)
            elif n == 16:
                order += [*range(off + 8, off + 16), *range(off, off + 8)]
            else:
                order += range(off, off + n)
        orders.append(order)
    return orders


_LITERAL_ORDER = _literal_orders()  # index: run length 0..32 -> source index for each output byte


def decompress_r2007(src: bytes) -> bytearray:
    """R2007 LZ77 variant (spec 5.10)."""
    out = bytearray()
    n = len(src)
    op, i = src[0], 1
    length = 0
    if op & 0xF0 == 0x20:
        length = src[3] & 7
        i = 4
    while i < n:
        if length == 0:  # literal length from the opcode
            length = op + 8
            if length == 0x17:
                k = src[i]
                i += 1
                length += k
                if k == 0xFF:
                    while True:
                        k = src[i] | (src[i + 1] << 8)
                        i += 2
                        length += k
                        if k != 0xFFFF:
                            break
        while length >= 32:
            out += bytes(src[i + k] for k in _LITERAL_ORDER[32])
            i += 32
            length -= 32
        out += bytes(src[i + k] for k in _LITERAL_ORDER[length])
        i += length
        length = 0
        if i >= n:
            break
        op, i = src[i], i + 1
        while True:  # one or more copies from the output
            kind = op >> 4
            if kind == 0:
                length, offset, op = (op & 0xF) + 0x13, src[i], src[i + 1]
                i += 2
                length += (op >> 3) & 0x10
                offset += ((op & 0x78) << 5) + 1
            elif kind == 1:
                length, offset, op = (op & 0xF) + 3, src[i], src[i + 1]
                i += 2
                offset += ((op & 0xF8) << 5) + 1
            elif kind == 2:
                offset = src[i] | (src[i + 1] << 8)
                i += 2
                length = op & 7
                if not op & 8:
                    op, i = src[i], i + 1
                    length += op & 0xF8
                else:
                    offset += 1
                    length += src[i] << 3
                    op = src[i + 1]
                    i += 2
                    length += ((op & 0xF8) << 8) + 0x100
            else:
                length, offset = op >> 4, op & 15
                op, i = src[i], i + 1
                offset += ((op & 0xF8) << 1) + 1
            start = len(out) - offset
            if start < 0:
                raise ValueError("compressed data points before the start of the page")
            if offset >= length:
                out += out[start : start + length]
            else:  # overlapping copy repeats the last `offset` bytes
                out += (out[start:] * (length // offset + 1))[:length]
            length = op & 7
            if length or i >= n:
                break
            op, i = src[i], i + 1
            if op >> 4 == 0:
                break
            if op >> 4 == 15:
                op &= 15
    return out


def _data_bytes(src: bytes, blocks: int, k: int) -> bytes:
    """Data part of interleaved Reed-Solomon (255, k) blocks (5.13.2). Parity is ignored, not checked."""
    return b"".join(src[j : j + k * blocks : blocks] for j in range(blocks))


def _system_page_r2007(data: bytes, addr: int, comp: int, uncomp: int, repeat: int) -> bytes:
    blocks = (((comp + 7) & ~7) * repeat + 238) // 239
    raw = _data_bytes(data[addr : addr + blocks * 255], blocks, 239)
    return decompress_r2007(raw[:comp]) if comp < uncomp else raw[:uncomp]


def _sections_r2007(data: bytes) -> dict[str, bytes]:
    raw = _data_bytes(data[0x80 : 0x80 + 3 * 255], 3, 239)
    comp = struct.unpack_from("<i", raw, 0x18)[0]
    h = struct.unpack("<34Q", bytes(decompress_r2007(raw[0x20 : 0x20 + comp]) if comp > 0 else raw[0x20 : 0x20 - comp])[:0x110])
    (map_crc, map_repeat, _, _, _, map_offset, _, _, map_comp, map_uncomp) = h[2:12]
    sec_comp, _, sec_id, sec_uncomp, _, sec_repeat = h[22:28]

    pm = _system_page_r2007(data, map_offset + 0x480, map_comp, map_uncomp, map_repeat)
    pages, offset = {}, 0x480
    for i in range(0, len(pm) - 15, 16):
        size, page_id = struct.unpack_from("<qq", pm, i)
        pages[abs(page_id)] = offset
        offset += size

    sm = _system_page_r2007(data, pages[sec_id], sec_comp, sec_uncomp, sec_repeat)
    out, i = {}, 0
    while i + 0x40 <= len(sm):
        size, _max, _enc, _hash, name_len, _unk, encoding, page_count = struct.unpack_from("<8Q", sm, i)
        i += 0x40
        name = bytes(sm[i : i + name_len]).decode("utf-16-le", "replace").rstrip("\0")
        i += name_len
        if size > len(data) * 0x1000:
            raise ValueError(f"implausible size for section {name}")
        buf = bytearray(size)
        for _ in range(page_count):
            start, _psize, page_id, uncomp, comp, _chk, _crc = struct.unpack_from("<7Q", sm, i)
            i += 56
            addr = pages[page_id]
            blocks = (((comp + 7) & ~7) + 250) // 251
            if encoding == 4:
                raw = _data_bytes(data[addr : addr + blocks * 255], blocks, 251)
            else:  # not interleaved: all data blocks first, parity after
                raw = data[addr : addr + comp]
            page = (decompress_r2007(raw[:comp]) if comp < uncomp else raw)[: min(uncomp, max(0, size - start))]
            buf[start : start + len(page)] = page
        out[name] = bytes(buf)
    return out


def _sections_r13(data: bytes) -> dict[str, bytes]:
    """R13-R2000 (spec chapter 3): sections sit uncompressed in the file, found by locator records at 0x15."""
    names = {0: "AcDb:Header", 1: "AcDb:Classes", 2: "AcDb:Handles", 4: "AcDb:Template"}
    out = {"AcDb:AcDbObjects": data}  # the object map holds absolute file offsets
    for i in range(struct.unpack_from("<I", data, 0x15)[0]):
        number, seeker, size = struct.unpack_from("<BII", data, 0x19 + 9 * i)
        if number in names:
            out[names[number]] = data[seeker : seeker + size]
    return out


def sections(data: bytes) -> dict[str, bytes]:
    """Named, decompressed sections of the file, e.g. 'AcDb:AcDbObjects'."""
    release = version(data)
    try:
        if release in _R2004_CONTAINER:
            return _sections_r2004(data)
        if release == "R2007":
            return _sections_r2007(data)
        return _sections_r13(data)
    except (IndexError, KeyError, struct.error) as e:
        raise ValueError(f"corrupt DWG container: {e!r}") from None


# --- bit stream (spec chapter 2) --------------------------------------------


class Bits:
    """MSB-first bit reader over a section. `p` is the position in bits."""

    __slots__ = ("d", "p")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.d = data
        self.p = pos

    def bits(self, n: int) -> int:
        start, end = self.p, self.p + n
        if end > len(self.d) * 8:
            raise ValueError("read past the end of the DWG data")
        self.p = end
        last = (end + 7) >> 3
        return (int.from_bytes(self.d[start >> 3 : last], "big") >> ((last << 3) - end)) & ((1 << n) - 1)

    def raw(self, n: int) -> bytes:
        return self.bits(8 * n).to_bytes(n, "big")

    def b(self) -> int:
        return self.bits(1)

    def bb(self) -> int:
        return self.bits(2)

    def rc(self) -> int:
        return self.bits(8)

    def rs(self) -> int:
        return int.from_bytes(self.raw(2), "little")

    def rl(self) -> int:
        return int.from_bytes(self.raw(4), "little")

    def rd(self) -> float:
        return struct.unpack("<d", self.raw(8))[0]

    def bs(self) -> int:
        code = self.bits(2)
        return self.rs() if code == 0 else self.rc() if code == 1 else 0 if code == 2 else 256

    def bl(self) -> int:
        code = self.bits(2)
        return self.rl() if code == 0 else self.rc() if code == 1 else 0

    def bll(self) -> int:
        return int.from_bytes(self.raw(self.bits(3)), "little")

    def bd(self) -> float:
        code = self.bits(2)
        return self.rd() if code == 0 else 1.0 if code == 1 else 0.0

    def dd(self, default: float) -> float:
        """Double stored as a patch over `default`."""
        code = self.bits(2)
        if code == 0:
            return default
        if code == 3:
            return self.rd()
        d = bytearray(struct.pack("<d", default))
        if code == 2:
            d[4:6] = self.raw(2)
        d[0:4] = self.raw(4)
        return struct.unpack("<d", d)[0]

    def bt(self) -> float:
        return 0.0 if self.b() else self.bd()

    def rd2(self) -> tuple[float, float]:
        return self.rd(), self.rd()

    def bd3(self) -> tuple[float, float, float]:
        return self.bd(), self.bd(), self.bd()

    def be(self) -> tuple[float, float, float]:
        return (0.0, 0.0, 1.0) if self.b() else self.bd3()

    def ot(self) -> int:
        code = self.bits(2)
        return self.rc() if code == 0 else self.rc() + 0x1F0 if code == 1 else self.rs()

    def h(self, owner: int = 0) -> int:
        """Handle reference, resolved to an absolute handle (relative codes need `owner`)."""
        code, n = self.bits(4), self.bits(4)
        value = self.bits(8 * n) if n else 0
        if code == 6:
            return owner + 1
        if code == 8:
            return owner - 1
        if code == 0xA:
            return owner + value
        if code == 0xC:
            return owner - value
        return value

    def tu(self) -> str:
        return self.raw(2 * self.bs()).decode("utf-16-le", "replace").rstrip("\0")

    def t(self) -> str:
        # shortcut: pre-R2007 text is decoded as cp1252, use the file's codepage (header 0x13) for other scripts
        return self.raw(self.bs()).decode("cp1252", "replace").rstrip("\0")


def _modular(data: bytes, i: int, signed: bool) -> tuple[int, int]:
    """Modular char: 7 bits per byte, low group first, high bit means another byte follows."""
    value = shift = 0
    while True:
        byte = data[i]
        i += 1
        if not byte & 0x80:
            if signed:
                value |= (byte & 0x3F) << shift
                return (-value if byte & 0x40 else value), i
            return value | (byte << shift), i
        value |= (byte & 0x7F) << shift
        shift += 7


def object_map(handles: bytes) -> dict[int, int]:
    """AcDb:Handles: object handle -> offset in AcDb:AcDbObjects."""
    out, i = {}, 0
    while True:
        size = int.from_bytes(handles[i : i + 2], "big")
        if size <= 2:
            return out
        j, end = i + 2, i + size
        handle = loc = 0
        while j < end:
            dh, j = _modular(handles, j, False)
            dl, j = _modular(handles, j, True)
            handle += dh
            loc += dl
            out[handle] = loc
        i = end + 2  # skip CRC


def _string_stream(data: bytes, end: int) -> Bits | None:
    """R2007+ string stream that ends at bit `end` (spec 20.1), or None if the flag bit is clear."""
    r = Bits(data, end - 1)
    if not r.b():
        return None
    r.p = pos = end - 17
    size = r.rs()
    if size & 0x8000:
        r.p = pos = pos - 16
        size = (size & 0x7FFF) | (r.rs() << 15)
    return Bits(data, pos - size)


def classes(section: bytes, release: str, maintenance: int) -> dict[int, tuple[str, bool]]:
    """AcDb:Classes (spec 4.x: 10.1, 10.2, 5.8): object type number -> (DXF name, is an entity), for types 500 and up."""
    start = 16 + 4 + (4 if release == "R2018" or (since(release, "R2010") and maintenance > 3) else 0)
    r = Bits(section, start * 8)
    strings, count = r, None
    if since(release, "R2007"):
        end = start * 8 + r.rl()
        strings = _string_stream(section, end)
        if strings is None:
            return {}
    if since(release, "R2004"):
        count = r.bs() - 499  # from the maximum class number
        r.rc(), r.rc(), r.b()
    text = strings.tu if since(release, "R2007") else strings.t
    limit = (20 + struct.unpack_from("<I", section, 16)[0]) * 8
    out = {}
    while count if count is not None else limit - r.p > 13:  # older files just run to the end of the data
        number = r.bs()
        r.bs()  # proxy flags
        text(), text()  # app name, C++ class name
        name = text()
        r.b()  # was a zombie
        out[number] = (name, r.bs() == 0x1F2)
        if count is not None:
            count -= 1
            r.bl(), r.bl(), r.bl(), r.bl(), r.bl()  # instance count, versions (longs, not the shorts of spec 10.2), unknowns
    return out


# --- objects (spec chapter 20) ----------------------------------------------

FIXED_TYPES = {
    0x01: "TEXT", 0x02: "ATTRIB", 0x03: "ATTDEF", 0x04: "BLOCK", 0x05: "ENDBLK", 0x06: "SEQEND",
    0x07: "INSERT", 0x08: "MINSERT", 0x0A: "VERTEX_2D", 0x0B: "VERTEX_3D", 0x0C: "VERTEX_MESH",
    0x0D: "VERTEX_PFACE", 0x0E: "VERTEX_PFACE_FACE", 0x0F: "POLYLINE_2D", 0x10: "POLYLINE_3D",
    0x11: "ARC", 0x12: "CIRCLE", 0x13: "LINE", 0x14: "DIMENSION_ORDINATE", 0x15: "DIMENSION_LINEAR",
    0x16: "DIMENSION_ALIGNED", 0x17: "DIMENSION_ANG3PT", 0x18: "DIMENSION_ANG2LN",
    0x19: "DIMENSION_RADIUS", 0x1A: "DIMENSION_DIAMETER", 0x1B: "POINT", 0x1C: "3DFACE",
    0x1D: "POLYLINE_PFACE", 0x1E: "POLYLINE_MESH", 0x1F: "SOLID", 0x20: "TRACE", 0x21: "SHAPE",
    0x22: "VIEWPORT", 0x23: "ELLIPSE", 0x24: "SPLINE", 0x25: "REGION", 0x26: "3DSOLID", 0x27: "BODY",
    0x28: "RAY", 0x29: "XLINE", 0x2A: "DICTIONARY", 0x2B: "OLEFRAME", 0x2C: "MTEXT", 0x2D: "LEADER",
    0x2E: "TOLERANCE", 0x2F: "MLINE", 0x30: "BLOCK_CONTROL", 0x31: "BLOCK_HEADER",
    0x32: "LAYER_CONTROL", 0x33: "LAYER", 0x34: "STYLE_CONTROL", 0x35: "STYLE", 0x38: "LTYPE_CONTROL",
    0x39: "LTYPE", 0x3C: "VIEW_CONTROL", 0x3D: "VIEW", 0x3E: "UCS_CONTROL", 0x3F: "UCS",
    0x40: "VPORT_CONTROL", 0x41: "VPORT", 0x42: "APPID_CONTROL", 0x43: "APPID",
    0x44: "DIMSTYLE_CONTROL", 0x45: "DIMSTYLE", 0x46: "VP_ENT_HDR_CONTROL", 0x47: "VP_ENT_HDR",
    0x48: "GROUP", 0x49: "MLINESTYLE", 0x4A: "OLE2FRAME", 0x4C: "LONG_TRANSACTION",
    0x4D: "LWPOLYLINE", 0x4E: "HATCH", 0x4F: "XRECORD", 0x50: "ACDBPLACEHOLDER",
    0x51: "VBA_PROJECT", 0x52: "LAYOUT", 0x1F2: "ACAD_PROXY_ENTITY", 0x1F3: "ACAD_PROXY_OBJECT",
}  # fmt: skip
_FIXED_ENTITIES = {*range(0x01, 0x2A), 0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x4A, 0x4D, 0x4E, 0x1F2}


class Obj:
    """One object's header plus readers positioned on its data, string and handle streams."""

    __slots__ = ("type", "handle", "r", "strings", "handles", "end", "unicode", "release", "is_entity", "start")

    def set_size(self, bits: int) -> None:
        """R13/R14 store the data size after the header fields, so the handle stream is located late."""
        self.end = self.start + bits
        self.handles = Bits(self.r.d, self.end)

    def text(self) -> str:
        """Next TV field. R2007+ keeps text in the string stream; before that it is inline in the data."""
        if self.unicode:
            return self.strings.tu() if self.strings else ""
        return self.r.t()


def read_object(objects: bytes, loc: int, release: str, names: dict[int, tuple[str, bool]]) -> Obj:
    # MS size: 15 bits per little-endian short, high bit means another short follows
    size = shift = 0
    while True:
        word = int.from_bytes(objects[loc : loc + 2], "little")
        loc += 2
        size |= (word & 0x7FFF) << shift
        shift += 15
        if not word & 0x8000:
            break
    o = Obj()
    o.release = release
    o.unicode = since(release, "R2007")
    o.strings = None
    o.start = loc * 8
    o.r = Bits(objects, loc * 8)
    end = None
    if since(release, "R2010"):
        handle_bits, loc = _modular(objects, loc, False)
        o.r.p = loc * 8
        number = o.r.ot()
        end = (loc + size) * 8 - handle_bits
    else:
        number = o.r.bs()
        if since(release, "R2000"):
            end = o.start + o.r.rl()
    o.type, o.is_entity = names.get(number) or (FIXED_TYPES.get(number, f"TYPE_{number}"), number in _FIXED_ENTITIES)
    o.handle = o.r.h()
    while eed_size := o.r.bs():  # extended entity data: app handle + bytes, repeated
        o.r.h()
        o.r.p += eed_size * 8
    o.end = o.handles = None  # R13/R14: set_size() fills these once the size field is reached
    if end is not None:
        o.handles = Bits(objects, end)
        if o.unicode:
            o.strings = _string_stream(objects, end)
        # the string-stream flag is the last data bit
        o.end = o.strings.p if o.strings else end - 1 if o.unicode else end
    return o
