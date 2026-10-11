"""Building information read out of 2D drafting: rooms from their outlines, walls from their two faces.

A plan drawing does not store rooms or walls, only lines on layers. What is derived here is an
interpretation, written to the IFC next to the original linework and never instead of it.
"""

import math
import re

_Z = (0.0, 0.0, 1.0)
# layer names that hold room outlines: rooms, spaces, areas, zones, in a few languages
_SPACE_LAYER = re.compile(r"room|space|area|zone|raum|flache|habitac|local|rymi|herbergi", re.IGNORECASE)


def _outline(points, bulges) -> list:
    """A polyline with bulge arcs as plain points, arcs broken into short chords."""
    out = []
    count = len(points)
    for i, (x1, y1) in enumerate(points):
        out.append((x1, y1))
        bulge = bulges[i] if i < len(bulges) else 0.0
        if not bulge:
            continue
        x2, y2 = points[(i + 1) % count]
        angle = 4 * math.atan(bulge)
        chord = math.hypot(x2 - x1, y2 - y1)
        if not chord:
            continue
        radius = chord / (2 * math.sin(angle / 2))
        # centre: off the chord's midpoint, to the left of travel for a counter-clockwise arc
        offset = radius * math.cos(angle / 2)
        cx = (x1 + x2) / 2 - offset * (y2 - y1) / chord
        cy = (y1 + y2) / 2 + offset * (x2 - x1) / chord
        start = math.atan2(y1 - cy, x1 - cx)
        steps = max(2, math.ceil(abs(angle) / (math.pi / 12)))
        out += [(cx + abs(radius) * math.cos(start + angle * s / steps), cy + abs(radius) * math.sin(start + angle * s / steps)) for s in range(1, steps)]
    return out


def area(outline) -> float:
    """Signed area of a closed outline: positive when counter-clockwise."""
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(outline, outline[1:] + outline[:1])) / 2


def inside(point, outline) -> bool:
    x, y = point[0], point[1]
    hit = False
    for (x1, y1), (x2, y2) in zip(outline, outline[1:] + outline[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def spaces(drawing, owner, smallest: float) -> list[dict]:
    """Rooms: closed outlines on room, space or area layers, named by the text found inside them.

    Hidden layers count too: offices often keep the room outlines on a layer that is switched off.
    `smallest` is the least area worth calling a room, in drawing units squared.
    """
    found, seen = [], set()
    labels = []
    for e in drawing.entities:
        if e["owner"] != owner:
            continue
        if e["type"] in ("TEXT", "MTEXT", "ATTRIB") and e.get("text", "").strip() and not e["invisible"]:
            labels.append(e)
            continue
        if e.get("extrusion", _Z) != _Z or not _SPACE_LAYER.search(drawing.layers.get(e["layer"], {}).get("name", "")):
            continue
        if e["type"] in ("LWPOLYLINE", "POLYLINE_2D") and e["closed"]:
            loops = [(e["points"], e["bulges"])]
        elif e["type"] == "HATCH":
            loops = [(p["points"], p["bulges"]) for p in e["paths"][:1]]  # the outer loop
        else:
            continue
        for points, bulges in loops:
            outline = _outline([p[:2] for p in points], bulges)
            size = abs(area(outline)) if len(outline) > 2 else 0.0
            if size < smallest:
                continue
            cx = sum(p[0] for p in outline) / len(outline)
            cy = sum(p[1] for p in outline) / len(outline)
            grain = math.sqrt(size) / 100  # the same room drawn again on another layer lands in the same cell
            key = (round(cx / grain), round(cy / grain), round(size / (grain * grain) / 100))
            if key in seen:
                continue
            seen.add(key)
            found.append({"points": [p[:2] for p in points], "bulges": list(bulges), "outline": outline, "area": size,
                          "elevation": e.get("elevation", 0.0), "handle": e["handle"], "layer": e["layer"],
                          "xdata": e.get("xdata", ())})  # fmt: skip

    for room in found:
        xs, ys = [p[0] for p in room["outline"]], [p[1] for p in room["outline"]]
        box = (min(xs), min(ys), max(xs), max(ys))
        texts = []
        for label in labels:
            x, y = label["point"][0], label["point"][1]
            if box[0] <= x <= box[2] and box[1] <= y <= box[3] and inside((x, y), room["outline"]):
                texts.append(re.sub(r"\\[A-Za-z][^;]*;|[{}]", "", label["text"]).replace("\\P", "\n").strip())
        texts = [t for t in dict.fromkeys(texts) if t]
        # a room number is short and has a digit; the longest remaining line is the best description we have
        numbers = sorted((t for t in texts if len(t) <= 10 and any(c.isdigit() for c in t) and "\n" not in t), key=lambda t: (" " in t, len(t)))
        room["name"] = numbers[0] if numbers else (texts[0].split("\n")[0][:40] if texts else "")
        room["labels"] = texts
        room["centre"] = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    rooms, named = [], set()
    for room in sorted(found, key=lambda r: r["area"]):
        if sum(1 for other in rooms if inside(other["centre"], room["outline"])) >= 3:
            continue  # encloses several rooms: a gross-area or zone outline, not a room
        key = (room["name"], round(math.log(room["area"]) * 20))  # same name, area within about 5 percent
        if room["name"] and key in named:
            continue  # the same room drawn again, as a hatch on a colour-coding layer
        named.add(key)
        rooms.append(room)
    return rooms


def _segments(drawing, owner, wanted) -> list:
    """Straight pieces of the visible linework on the wanted layers: (x1, y1, x2, y2, layer handle)."""
    out = []
    for e in drawing.entities:
        layer = drawing.layers.get(e["layer"], {})
        if e["owner"] != owner or e["invisible"] or layer.get("off") or layer.get("frozen") or not wanted(layer.get("name", "")):
            continue
        if e.get("extrusion", _Z) != _Z:
            continue
        if e["type"] == "LINE":
            out.append((*e["start"][:2], *e["end"][:2], e["layer"]))
        elif e["type"] in ("LWPOLYLINE", "POLYLINE_2D"):
            points = [p[:2] for p in e["points"]]
            pairs = zip(points, points[1:] + points[:1]) if e["closed"] else zip(points, points[1:])
            for i, (p, q) in enumerate(pairs):
                if not (e["bulges"][i] if i < len(e["bulges"]) else 0.0):
                    out.append((*p, *q, e["layer"]))
    return out


def _uncovered(start, end, covered) -> list:
    """The parts of [start, end] not inside any of the covered intervals."""
    out = [(start, end)]
    for a, b in covered:
        out = [piece for s, e in out for piece in ((s, min(e, a)), (max(s, b), e)) if piece[1] > piece[0]]
    return out


def walls(drawing, owner, wanted, thinnest: float, thickest: float, shortest: float = 0.0) -> list[dict]:
    """Walls, read from the two parallel lines a plan draws for each one.

    Two lines on a wall layer that run parallel, between `thinnest` and `thickest` apart, are taken as
    the faces of a wall for as far as they run side by side, if that is further than `shortest` and than
    the wall is thick. Each face is used once per side, nearest
    partner first, so a line between two others makes two walls rather than three.
    Returns centre lines with a thickness: {"start", "end", "thickness", "layer"}, plus where each lies in
    its group's own axes ("frame": origin and direction; "a", "b" along it; "across") for `join`.
    """
    buckets = {}
    for x1, y1, x2, y2, layer in _segments(drawing, owner, wanted):
        length = math.hypot(x2 - x1, y2 - y1)
        if length < thinnest:
            continue
        angle = math.atan2(y2 - y1, x2 - x1) % math.pi
        key = round(math.degrees(angle) * 2) % 360  # half-degree bins; 0 and 180 degrees are the same direction
        buckets.setdefault(key, []).append((x1, y1, x2, y2, layer))

    out = []
    for lines in buckets.values():
        # measure along and across the first line's direction, from its start, so far-away drawings keep precision
        ox, oy, x2, y2, _ = lines[0]
        length = math.hypot(x2 - ox, y2 - oy)
        ux, uy = (x2 - ox) / length, (y2 - oy) / length
        faces = []
        for x1, y1, x2, y2, layer in lines:
            a, b = sorted(((x1 - ox) * ux + (y1 - oy) * uy, (x2 - ox) * ux + (y2 - oy) * uy))
            across = (-(x1 - ox) * uy + (y1 - oy) * ux - (x2 - ox) * uy + (y2 - oy) * ux) / 2
            faces.append({"across": across, "a": a, "b": b, "layer": layer, "above": [], "below": []})
        faces.sort(key=lambda f: f["across"])
        candidates = []
        for i, low in enumerate(faces):
            for high in faces[i + 1 :]:
                gap = high["across"] - low["across"]
                if gap > thickest:
                    break
                if gap >= thinnest and min(low["b"], high["b"]) - max(low["a"], high["a"]) > gap:
                    candidates.append((gap, low, high))
        candidates.sort(key=lambda c: c[0])
        for gap, low, high in candidates:
            start, end = max(low["a"], high["a"]), min(low["b"], high["b"])
            for a, b in _uncovered(start, end, low["above"] + high["below"]):
                if b - a <= max(gap, shortest):
                    continue  # shorter than it is thick: a jamb or a corner, not a wall
                low["above"].append((a, b))
                high["below"].append((a, b))
                middle = low["across"] + gap / 2
                out.append({
                    "start": (ox + a * ux - middle * uy, oy + a * uy + middle * ux),
                    "end": (ox + b * ux - middle * uy, oy + b * uy + middle * ux),
                    "thickness": gap,
                    "layer": low["layer"],
                    "frame": (ox, oy, ux, uy), "a": a, "b": b, "across": middle,
                })  # fmt: skip
    return out


def join(pieces: list[dict], evidence: list, tolerance: float, widest: float) -> list[dict]:
    """Walls joined across the gaps a plan leaves for doors and windows.

    Pieces on one line with one thickness are one wall where the gap between them is negligible, or
    holds a sign of a door or window. `evidence` is [(x, y, what)]: a point of something drawn on a
    door or window layer, or the insertion point of a door or window block. Each returned wall has
    "openings": [(distance from its start, width, what)].
    """
    lines = {}
    for piece in pieces:
        key = (piece["frame"], round(piece["across"] / tolerance), round(piece["thickness"] / tolerance))
        lines.setdefault(key, []).append(piece)
    out = []
    for (frame, _, _), group in lines.items():
        ox, oy, ux, uy = frame
        group.sort(key=lambda p: p["a"])
        near = []  # evidence in this line's axes, close enough across to be in the wall
        reach = group[0]["thickness"] / 2 + 2 * tolerance
        for x, y, what in evidence:
            if abs(-(x - ox) * uy + (y - oy) * ux - group[0]["across"]) <= reach:
                near.append(((x - ox) * ux + (y - oy) * uy, what))
        wall = dict(group[0], openings=[])
        for piece in group[1:]:
            gap = piece["a"] - wall["b"]
            found = next((what for along, what in near if wall["b"] - tolerance <= along <= piece["a"] + tolerance), None)
            if gap <= tolerance or (gap <= widest and found is not None):
                if gap > tolerance:
                    wall["openings"].append((wall["b"] - wall["a"], gap, found))
                wall["b"] = piece["b"]
                continue
            out.append(wall)
            wall = dict(piece, openings=[])
        out.append(wall)
    for wall in out:
        ox, oy, ux, uy = wall["frame"]
        wall["start"] = (ox + wall["a"] * ux - wall["across"] * uy, oy + wall["a"] * uy + wall["across"] * ux)
        wall["end"] = (ox + wall["b"] * ux - wall["across"] * uy, oy + wall["b"] * uy + wall["across"] * ux)
    return out


def footprints(drawing, owner, wanted, smallest: float, largest: float) -> list[dict]:
    """Closed shapes on the wanted visible layers, as outlines: the plan section of a column, say.

    Circles are returned as {"center", "radius"}, everything else as {"outline"}. A shape drawn twice
    (an outline and its hatch) is returned once.
    """
    out, seen = [], set()
    for e in drawing.entities:
        layer = drawing.layers.get(e["layer"], {})
        if e["owner"] != owner or e["invisible"] or layer.get("off") or layer.get("frozen") or not wanted(layer.get("name", "")):
            continue
        if e.get("extrusion", _Z) != _Z:
            continue
        shape = {"layer": e["layer"], "handle": e["handle"], "elevation": e.get("elevation", 0.0)}
        if e["type"] == "CIRCLE":
            shape |= {"center": e["center"][:2], "radius": e["radius"], "elevation": e["center"][2]}
            size, (cx, cy) = math.pi * e["radius"] ** 2, e["center"][:2]
        elif e["type"] in ("LWPOLYLINE", "POLYLINE_2D") and e["closed"] or e["type"] == "HATCH" and e["paths"]:
            points, bulges = (e["points"], e["bulges"]) if "points" in e else (e["paths"][0]["points"], e["paths"][0]["bulges"])
            outline = _outline([p[:2] for p in points], bulges)
            if len(outline) < 3:
                continue
            size = abs(area(outline))
            cx, cy = sum(p[0] for p in outline) / len(outline), sum(p[1] for p in outline) / len(outline)
            shape["outline"] = outline
        else:
            continue
        if not smallest <= size <= largest:
            continue
        grain = math.sqrt(size) / 20
        key = (round(cx / grain), round(cy / grain))
        if key not in seen:
            seen.add(key)
            out.append(shape)
    return out
