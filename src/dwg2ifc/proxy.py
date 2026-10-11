"""Proxy entity graphics (spec chapter 29): the picture of itself that an entity can carry.

AutoCAD stores it so programs that do not know an entity type can still draw it. It is how
multileaders, AEC objects and other custom entities are converted here.
"""

import math
import struct
from types import SimpleNamespace

from dwg2ifc import dwg

_IDENTITY = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def _multiply(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)) for i in range(4))


def _wide_string(data: bytes, at: int) -> tuple[str, int]:
    """Zero-terminated UTF-16 string padded to a 4-byte boundary; returns it and the offset after the padding."""
    end = at
    while end + 1 < len(data) and data[end : end + 2] != b"\0\0":
        end += 2
    return data[at:end].decode("utf-16-le", "replace"), at + ((end + 2 - at + 3) & ~3)


def _byte_string(data: bytes, at: int) -> tuple[str, int]:
    end = data.find(b"\0", at)
    end = len(data) if end < 0 else end
    # shortcut: decoded as cp1252, use the drawing's codepage for other scripts
    return data[at:end].decode("cp1252", "replace"), at + ((end + 1 - at + 3) & ~3)


def decode(data: bytes, release: str) -> list[dict]:
    """The drawable parts of a graphics stream, as entity-shaped dicts in world coordinates."""
    parts: list[dict] = []
    stack = [_IDENTITY]
    at = 8  # total size, chunk count
    while at + 8 <= len(data):
        size, kind = struct.unpack_from("<II", data, at)
        if size < 8 or at + size > len(data):
            break
        chunk = data[at + 8 : at + size]
        at += size
        m = stack[-1]

        def point(i):
            x, y, z = struct.unpack_from("<3d", chunk, i)
            return tuple(m[r][0] * x + m[r][1] * y + m[r][2] * z + m[r][3] for r in range(3))

        def vector(i):
            x, y, z = struct.unpack_from("<3d", chunk, i)
            return tuple(m[r][0] * x + m[r][1] * y + m[r][2] * z for r in range(3))

        scale = math.sqrt(sum(m[r][0] ** 2 for r in range(3)))  # shortcut: assumes the transform scales evenly

        try:
            if kind in (2, 4):  # circle, circular arc
                radius = struct.unpack_from("<d", chunk, 24)[0] * scale
                normal = vector(32)
                sweep = 2 * math.pi
                start = None
                if kind == 4:
                    start, sweep = vector(56), struct.unpack_from("<d", chunk, 80)[0]
                if start is None or not any(start):
                    # any direction square to the normal will do for a full circle
                    start = (-normal[1], normal[0], 0.0) if abs(normal[2]) < 0.9 else (1.0, 0.0, 0.0)
                length = math.sqrt(sum(c * c for c in start)) or 1.0
                parts.append({"type": "ELLIPSE", "center": point(0), "major": tuple(c * radius / length for c in start),
                              "extrusion": normal, "ratio": 1.0, "start_angle": 0.0, "end_angle": sweep})  # fmt: skip
            elif kind in (6, 7, 32):  # polyline, polygon, polyline with normal
                count = struct.unpack_from("<I", chunk, 0)[0]
                points = [point(4 + 24 * i) for i in range(count)]
                parts.append({"type": "POLYLINE_3D", "points": points, "closed": kind == 7})
            elif kind == 8:  # mesh: rows x columns of vertices
                rows, columns = struct.unpack_from("<II", chunk, 0)
                points = [point(8 + 24 * i) for i in range(rows * columns)]
                parts.append({"type": "POLYLINE_MESH", "points": points, "m": rows, "n": columns, "closed_m": False, "closed_n": False})
            elif kind == 9:  # shell: vertices, then face loops as a count followed by that many 1-based indices
                count = struct.unpack_from("<I", chunk, 0)[0]
                points = [point(4 + 24 * i) for i in range(count)]
                entries_at = 4 + 24 * count
                entries = struct.unpack_from(f"<{struct.unpack_from('<I', chunk, entries_at)[0]}i", chunk, entries_at + 4)
                faces, i = [], 0
                while i < len(entries):
                    n = abs(entries[i])
                    faces.append([v + 1 for v in entries[i + 1 : i + 1 + n]])
                    i += 1 + n
                parts.append({"type": "POLYLINE_PFACE", "points": points, "faces": faces})
            elif kind in (10, 11, 36, 38):  # text, in four layouts
                read = _wide_string if kind in (36, 38) else _byte_string
                if kind in (10, 36):
                    height = struct.unpack_from("<d", chunk, 72)[0]
                    text, _ = read(chunk, 96)
                else:
                    text, after = read(chunk, 72)
                    height = struct.unpack_from("<d", chunk, after + 8)[0]
                parts.append({"type": "PROXYTEXT", "point": point(0), "extrusion": vector(24), "x_axis": vector(48),
                              "height": height * scale, "text": text})  # fmt: skip
            elif kind == 33:  # the body of an LWPOLYLINE entity, bit-coded as in the objects section
                size = struct.unpack_from("<I", chunk, 0)[0]
                from dwg2ifc.entities import _lwpolyline  # late: entities imports this module

                e = _lwpolyline(SimpleNamespace(release=release), dwg.Bits(chunk[4 : 4 + size]))
                if m is _IDENTITY:
                    parts.append({"type": "LWPOLYLINE"} | e)
                else:
                    # shortcut: under a transform the arcs are drawn as straight segments
                    lifted = [struct.pack("<3d", x, y, e["elevation"]) for x, y in e["points"]]
                    chunk = b"".join(lifted)
                    parts.append({"type": "POLYLINE_3D", "points": [point(24 * i) for i in range(len(lifted))], "closed": e["closed"]})
            elif kind in (29, 30):  # push a transform for what follows
                rows = struct.unpack_from("<16d", chunk, 0)
                stack.append(_multiply(m, tuple(rows[i : i + 4] for i in range(0, 16, 4))))
            elif kind == 31 and len(stack) > 1:
                stack.pop()
            # everything else is colour, layer and clipping state, or circles through three points (not drawn yet)
        except (struct.error, ValueError, IndexError):
            continue  # one bad chunk should not lose the others
    return parts
