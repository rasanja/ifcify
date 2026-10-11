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
