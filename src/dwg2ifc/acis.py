"""ACIS B-rep reader for 3DSOLID, REGION, BODY and surface entities.

DWG stores these as an ACIS save file: SAT text (obfuscated) up to R2004, SAB binary after.
Both are turned into the same token records here, then into plain faces, loops and edges.
Nothing in the DWG spec covers the content; the layouts below were worked out by saving the same
solids as R2000 (text) and R2010 (binary) and comparing.
"""

import struct

SAB_MAGIC = b"ACIS BinaryFile"
_ENDS = ("End-of-ACIS-data", "End-of-ASM-data")


class Ptr(int):
    """Reference to another record by index; -1 is null."""


class Enum(int):
    """Enumeration value in binary files, where text files have a word."""


def sat_tokens(text: str) -> tuple[int, list[list]]:
    """(ACIS version, records) from SAT text. A record is its type name followed by its fields."""
    lines = text.split("\n", 3)
    version = int(lines[0].split()[0])
    body = lines[3] if version >= 200 and len(lines) > 3 else text.split("\n", 1)[1]  # 2 more header lines from 2.0
    records, record = [], []
    for word in body.split():
        if word == "#":
            if record and isinstance(record[0], float):  # optional "-12" sequence number
                record.pop(0)
            if record and record[0] in _ENDS:
                break
            records.append(record)
            record = []
        elif word[0] == "$" and word[1:].lstrip("-").isdigit():
            record.append(Ptr(word[1:]))
        else:
            try:
                record.append(float(word))
            except ValueError:
                record.append(word)
    return version, records


def sab_tokens(blob: bytes) -> tuple[int, list[list]]:
    """(ACIS version, records) from SAB binary."""
    version = struct.unpack_from("<i", blob, len(SAB_MAGIC))[0]
    p = len(SAB_MAGIC) + 16
    records, record, name = [], [], []
    while p < len(blob):
        tag = blob[p]
        p += 1
        if tag == 0x04:
            record.append(float(struct.unpack_from("<i", blob, p)[0]))
            p += 4
        elif tag == 0x06:
            record.append(struct.unpack_from("<d", blob, p)[0])
            p += 8
        elif tag == 0x0C:
            record.append(Ptr(struct.unpack_from("<i", blob, p)[0]))
            p += 4
        elif tag in (0x0D, 0x0E):  # type name; hyphenated names come as 0x0E parts closed by a 0x0D part
            name.append(blob[p + 1 : p + 1 + blob[p]].decode("ascii", "replace"))
            p += 1 + blob[p]
            if tag == 0x0D:
                record.append("-".join(name))
                name = []
                if record[-1] in _ENDS:
                    break
        elif tag in (0x13, 0x14):  # position, vector
            record += struct.unpack_from("<3d", blob, p)
            p += 24
        elif tag == 0x0A:
            record.append(True)
        elif tag == 0x0B:
            record.append(False)
        elif tag == 0x0F:
            record.append("{")
        elif tag == 0x10:
            record.append("}")
        elif tag == 0x11:
            records.append(record)
            record = []
        elif tag == 0x15:
            record.append(Enum(struct.unpack_from("<i", blob, p)[0]))
            p += 4
        elif tag == 0x16:
            record += struct.unpack_from("<2d", blob, p)
            p += 16
        elif tag == 0x02:
            record.append(float(blob[p]))
            p += 1
        elif tag == 0x03:
            record.append(float(struct.unpack_from("<h", blob, p)[0]))
            p += 2
        elif tag == 0x05:
            record.append(float(struct.unpack_from("<f", blob, p)[0]))
            p += 4
        elif tag in (0x07, 0x08, 0x09, 0x12):  # strings with a 1, 2 or 4 byte length
            size = {0x07: 1, 0x08: 2}.get(tag, 4)
            length = int.from_bytes(blob[p : p + size], "little")
            record.append(blob[p + size : p + size + length].decode("utf-8", "replace"))
            p += size + length
        elif tag == 0x17:
            record.append(float(struct.unpack_from("<q", blob, p)[0]))
            p += 8
        else:
            raise ValueError(f"unknown SAB tag {tag:#x}")
    return version, records


def _flag(token) -> bool:
    """reversed / double / finite, however the file spells it."""
    return token is True or token in ("reversed", "reversed_v", "double", "F")


def _spline(tokens: list, start: int, surface: bool):
    """Finds the NURBS data inside the subtype block opening at tokens[start].

    Blocks hold a procedural definition (blend, offset, intersection...) plus the B-spline that
    approximates it. Only the B-spline is read; nested blocks are support geometry and are skipped.
    """
    depth, i = 0, start
    while i < len(tokens):
        token = tokens[i]
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth == 0:
                return None
        elif depth == 1 and token in ("nubs", "nurbs") and i + 4 < len(tokens):
            ahead = tokens[i + 1 : i + 5]
            closure = lambda t: isinstance(t, Enum) or t in ("open", "closed", "periodic")  # noqa: E731
            is_surface = isinstance(ahead[1], float) and closure(ahead[2])
            is_curve = closure(ahead[1]) and isinstance(ahead[2], float)
            if (surface and is_surface) or (not surface and is_curve and not is_surface):
                return _read_spline(tokens, i, surface)
        i += 1
    return None


def _read_spline(tokens: list, i: int, surface: bool):
    size = 4 if tokens[i] == "nurbs" else 3
    i += 1

    def knots(count):
        nonlocal i
        values = [float(v) for v in tokens[i : i + 2 * count : 2]]
        counts = [int(m) for m in tokens[i + 1 : i + 2 * count : 2]]
        i += 2 * count
        return values, counts

    def points(count):
        nonlocal i
        flat = tokens[i : i + count * size]
        i += count * size
        if len(flat) != count * size or not all(isinstance(v, float) for v in flat):
            raise ValueError("truncated spline data")
        return [tuple(flat[k : k + size]) for k in range(0, len(flat), size)]

    if surface:
        degree_u, degree_v = int(tokens[i]), int(tokens[i + 1])
        count_u, count_v = int(tokens[i + 6]), int(tokens[i + 7])
        i += 8
        (ku, mu), (kv, mv) = knots(count_u), knots(count_v)
        nu, nv = sum(mu) - degree_u + 1, sum(mv) - degree_v + 1
        grid = points(nu * nv)  # v outer, u inner
        return ("bspline", degree_u, degree_v, ku, mu, kv, mv, [grid[u::nu] for u in range(nu)])
    degree, count = int(tokens[i]), int(tokens[i + 2])
    i += 3
    k, m = knots(count)
    return ("bspline", degree, k, m, points(sum(m) - degree + 1))


class _File:
    def __init__(self, version: int, records: list[list]) -> None:
        self.records = records
        # every entity record starts with an attribute pointer; from ACIS 7 an id and a history pointer follow
        self.base = 4 if version >= 700 else 2
        self.edge_params = version >= 500
        # subtype blocks are numbered in file order so later ones can say "ref N"
        self.subtypes = [(r, i) for r, record in enumerate(records) for i, t in enumerate(record)
                         if t == "{" and i + 1 < len(record) and record[i + 1] != "ref"]  # fmt: skip
        self.transform = None

    def record(self, pointer):
        return self.records[pointer] if isinstance(pointer, Ptr) and 0 <= pointer < len(self.records) else None

    def fields(self, pointer, kind: str):
        record = self.record(pointer)
        if not record or not (record[0] == kind or str(record[0]).endswith("-" + kind)):  # "tedge-edge" is an edge
            return None
        return record[self.base :]

    def chain(self, pointer, kind: str):
        """Records linked through their first field (next lump, next shell, next face, next loop)."""
        seen = set()
        while (fields := self.fields(pointer, kind)) is not None and pointer not in seen:
            seen.add(pointer)
            yield fields
            pointer = fields[0]

    def spline(self, record: list, surface: bool):
        """(reversed, spline) for an intcurve or spline-surface record."""
        try:
            at = record.index("{")
        except ValueError:
            return False, None
        reverse = _flag(record[at - 1])
        tokens = record
        for _ in range(8):  # follow "ref N" to the block that holds the data
            if at + 2 < len(tokens) and tokens[at + 1] == "ref":
                number = int(tokens[at + 2])
                if not 0 <= number < len(self.subtypes):
                    return reverse, None
                r, at = self.subtypes[number]
                tokens = self.records[r]
            else:
                return reverse, _spline(tokens, at, surface)
        return reverse, None

    # geometry is returned in world coordinates: the body transform is applied here
    def point(self, p):
        if self.transform is None:
            return tuple(map(float, p))
        rows, shift, scale = self.transform
        return tuple(scale * sum(p[k] * rows[k][i] for k in range(3)) + shift[i] for i in range(3))

    def direction(self, v):
        if self.transform is None:
            return tuple(map(float, v))
        rows = self.transform[0]
        return tuple(sum(v[k] * rows[k][i] for k in range(3)) for i in range(3))

    def length(self, x):
        return x * (self.transform[2] if self.transform else 1.0)

    def spline_world(self, spline):
        if spline is None:
            return None
        move = lambda p: (*self.point(p[:3]), *p[3:])  # noqa: E731
        if len(spline) == 5:
            return (*spline[:4], [move(p) for p in spline[4]])
        return (*spline[:7], [[move(p) for p in row] for row in spline[7]])

    def curve(self, pointer):
        """(reversed, geometry) of an edge's curve; geometry is None when unsupported."""
        record = self.record(pointer)
        if not record:
            return False, None
        f = record[self.base :]
        if record[0] == "straight-curve":
            return False, ("line", self.point(f[0:3]), self.direction(f[3:6]))
        if record[0] == "ellipse-curve":
            major = self.direction(f[6:9])
            return False, ("ellipse", self.point(f[0:3]), self.direction(f[3:6]), tuple(self.length(c) for c in major), f[9])
        if record[0] == "intcurve-curve":
            reverse, spline = self.spline(record, False)
            return reverse, self.spline_world(spline)
        return False, None

    def surface(self, pointer):
        """(reversed, geometry) of a face's surface; geometry is None when unsupported."""
        record = self.record(pointer)
        if not record:
            return False, None
        f = record[self.base :]
        kind = record[0]
        if kind == "plane-surface":
            return False, ("plane", self.point(f[0:3]), self.direction(f[3:6]), self.direction(f[6:9]))
        if kind == "cone-surface":
            i = 10
            for _ in range(2):  # two range bounds, each "infinite" or "finite value"
                i += 2 if _flag(f[i]) else 1
            sine, cosine = f[i], f[i + 1]
            major = tuple(self.length(c) for c in self.direction(f[6:9]))
            return False, ("cone", self.point(f[0:3]), self.direction(f[3:6]), major, f[9], sine, cosine)
        if kind == "sphere-surface":
            return False, ("sphere", self.point(f[0:3]), self.length(f[3]), self.direction(f[4:7]), self.direction(f[7:10]))
        if kind == "torus-surface":
            return False, ("torus", self.point(f[0:3]), self.direction(f[3:6]), self.length(f[6]), self.length(f[7]), self.direction(f[8:11]))
        if kind == "spline-surface":
            reverse, spline = self.spline(record, True)
            return reverse, self.spline_world(spline)
        return False, None

    def vertex(self, pointer):
        fields = self.fields(pointer, "vertex")
        if not fields:
            return None
        point = self.fields(next((t for t in reversed(fields) if isinstance(t, Ptr)), None), "point")
        return self.point(point[0:3]) if point else None

    def loop(self, fields) -> list:
        """A face boundary as [(edge key, start, end, curve, same sense, forward in loop)], in loop order.

        curve is None when unsupported and "point" for an edge without geometry.
        """
        first, out, seen = fields[1], [], set()
        at = first
        while (coedge := self.fields(at, "coedge")) is not None and at not in seen:
            seen.add(at)
            edge = self.fields(coedge[3], "edge")
            if edge:
                if self.edge_params:
                    start, end, curve, sense = edge[0], edge[2], edge[5], edge[6]
                else:
                    start, end, curve, sense = edge[0], edge[1], edge[3], edge[4]
                if self.record(curve) is None:  # no geometry: the edge is a single point, such as a cone's apex
                    out.append((int(coedge[3]), self.vertex(start), self.vertex(end), "point", True, True))
                    at = coedge[0]
                    continue
                curve_reversed, geometry = self.curve(curve)
                out.append(
                    (int(coedge[3]), self.vertex(start), self.vertex(end), geometry, _flag(sense) == curve_reversed, not _flag(coedge[4]))
                )
            at = coedge[0]
        return out

    def bodies(self) -> list[dict]:
        out = []
        for index, record in enumerate(self.records):
            if not record or record[0] != "body":
                continue
            body = record[self.base :]
            self.transform = None
            t = self.fields(body[2], "transform")
            if t:
                if self.base == 4:
                    t = self.record(body[2])[3:]  # a transform record has no history pointer
                self.transform = ([t[0:3], t[3:6], t[6:9]], t[9:12], t[12])
            faces = []
            for lump in self.chain(body[0], "lump"):
                for shell in self.chain(lump[1], "shell"):
                    for face in self.chain(shell[2], "face"):
                        surface_reversed, surface = self.surface(face[4])
                        faces.append(
                            {
                                "surface": surface,
                                "same_sense": _flag(face[5]) == surface_reversed,
                                "double": _flag(face[6]) if len(face) > 6 else False,
                                "loops": [self.loop(loop) for loop in self.chain(face[1], "loop")],
                            }
                        )
            if faces:
                out.append({"faces": faces})
        return out


def bodies(data: bytes) -> list[dict]:
    """The bodies in an ACIS save file (text or binary) as faces with a surface and edge loops."""
    try:
        if data.startswith(SAB_MAGIC):
            return _File(*sab_tokens(data)).bodies()
        return _File(*sat_tokens(data.decode("latin-1"))).bodies()
    except (IndexError, TypeError, struct.error) as e:
        raise ValueError(f"corrupt ACIS data: {e!r}") from None
