"""Development only (needs AutoCAD 2027): measures our conversion against AutoCAD's own figures.

AutoCAD's LIST command prints, for every object in model space, values it computes itself: a line's
length, a circle's area, a polyline's length and area, a region's area, a solid's or mesh's bounding
box. MASSPROP adds the volume, centroid and bounding box of all solids together. This script asks
AutoCAD for those, works the same quantities out from our reading of the drawing and from the IFC we
write, and reports how far apart they are.

    uv run python tools/accuracy.py drawing.dwg [more.dwg ...]
    uv run python tools/accuracy.py --from-log autocad.txt drawing.dwg     (no AutoCAD: a saved log)
    uv run python tools/accuracy.py --save-log autocad.txt drawing.dwg

"decoded" rows compare what we read from the DWG; "ifc" rows compare the finished IFC as a geometry
kernel builds it, which also carries the kernel's own tessellation error on curved shapes.
"""

import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.unit

from dwg2ifc import derive, entities, ifc
from dwg2ifc.entities import bspline_points, fit_spline

NUMBER = r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
XYZ = rf"X\s*=\s*{NUMBER}\s*,?\s*Y\s*=\s*{NUMBER}\s*,?\s*Z\s*=\s*{NUMBER}"
# what LIST calls a value, per quantity we compare
FIELDS = {
    "length": [rf"^\s*Length\s*=\s*{NUMBER}", rf"^\s*length\s+{NUMBER}", rf"^\s*Length:\s*{NUMBER}", rf"^\s*circumference\s+{NUMBER}"],
    "length3d": [rf"3D Length\s*=\s*{NUMBER}"],
    "area": [rf"^\s*area\s+{NUMBER}", rf"^\s*Area:?\s+{NUMBER}"],
    "perimeter": [rf"^\s*Perimeter:\s*{NUMBER}"],
}


SUB_OBJECTS = ("VERTEX", "ATTRIBUTE", "END SEQUENCE")  # listed with their owner, not objects of their own


def _console(drawing: Path, commands: str) -> str:
    with tempfile.TemporaryDirectory() as work:
        script = Path(work) / "run.scr"
        # QAFLAGS 2: no "press Enter to continue"; LUNITS 2 and LUPREC 8: plain decimals, eight of them, whatever the drawing shows
        script.write_text("FILEDIA 0\nQAFLAGS 2\nLUNITS 2\nLUPREC 8\n" + commands + "QUIT Y\n")
        subprocess.run([str(Path(__file__).parent / "acad.sh"), str(script), str(drawing.resolve())], check=False, env={"ACAD_TIMEOUT": "600", "PATH": "/usr/bin:/bin"})
        log = Path(str(script) + ".log").read_bytes().replace(b"\0", b"").replace(b"\b", b"").decode("utf-8", "replace").replace("\r", "")
        # the console's preamble names folders on this machine; none of it is needed
        return "\n".join(line for line in log.splitlines() if "/" not in line or "Layer:" in line)


def autocad_log(drawing: Path) -> str:
    """Runs LIST and MASSPROP on the drawing in AutoCAD's console and returns what it printed.

    When the drawing has solids, they are also measured one at a time. The console cannot pick an
    object, but it can take the last one, so two more runs walk the drawing backwards: the first
    with LIST L and ERASE L, to learn the order the console really takes objects in (it differs from
    LIST ALL's where objects are hidden or frozen); the second with MASSPROP L on the solids.
    """
    log = _console(drawing, "LIST ALL\n\nMASSPROP ALL\n\nN\n")
    listed = [kind for kind in re.findall(r'^\s+([A-Z0-9_ ]+?)\s+Layer: "', log.partition("Command: MASSPROP")[0], re.M) if kind.strip() not in SUB_OBJECTS]
    if any(kind in ("3DSOLID", "REGION") for kind in listed) and len(listed) <= 2000:
        walk = _console(drawing, "LIST L\n\nERASE L\n\n" * (len(listed) + 50))
        order = _walked(walk)
        # only solids and regions are measured: on anything else MASSPROP asks no question, and the answer
        # meant for it would be taken as an unknown command, which ends the script
        # AREA gives a solid's surface area, which does not depend on which way a kernel thinks its faces point
        steps = ("AREA\nO\nL\nMASSPROP L\n\nN\n" * (kind in ("3DSOLID", "REGION")) + "ERASE L\n\n" for _, kind in order)
        log += "\n=== ORDER ===\n" + walk + "\n=== ONE AT A TIME ===\n" + _console(drawing, "".join(steps))
    return log


def _walked(walk: str) -> list[tuple[int, str]]:
    """(handle, type) of each object the LIST L / ERASE L walk met, in order, stopping if it stops advancing."""
    order = []
    for answer in walk.split("Command: LIST")[1:]:
        # the object's type is the first thing printed once the selection is closed (a light names no layer)
        kind = re.search(r'Select objects:[ .]*\n\s+([A-Z0-9_]+(?: [A-Z]+)*?)\s*(?:Layer: "[^\n]*)?\n', answer)
        handle = re.search(r"^\s*Handle = ([0-9a-fA-F]+)", answer, re.M)
        if not kind or not handle or (order and order[-1][0] == int(handle.group(1), 16)):
            break
        order.append((int(handle.group(1), 16), kind.group(1).strip()))
    return order


def parse(log: str) -> tuple[dict, dict]:
    """({handle: {type, length, area, perimeter, box}}, totals from MASSPROP)."""
    log, _, single = log.partition("=== ONE AT A TIME ===")
    log, _, walk = log.partition("=== ORDER ===")
    listed, _, mass = log.partition("Command: MASSPROP")
    objects, current = {}, None
    lines = listed.splitlines()
    for i, line in enumerate(lines):
        header = re.match(r'^\s+([A-Z0-9_ ]+?)\s+Layer: "', line)
        if header:
            current = {"type": header.group(1).strip()}
            continue
        if current is None:
            continue
        handle = re.match(r"^\s*Handle = ([0-9a-fA-F]+)", line)
        if handle:
            objects[int(handle.group(1), 16)] = current
            continue
        for name, patterns in FIELDS.items():
            for pattern in patterns:
                found = re.search(pattern, line)
                if found and name not in current:
                    current[name] = float(found.group(1))
        low = re.search(rf"Lower [Bb]ound {XYZ}", line)
        if low and i + 1 < len(lines):
            high = re.search(rf"Upper [Bb]ound {XYZ}", lines[i + 1])
            if high:
                current["box"] = (tuple(map(float, low.groups())), tuple(map(float, high.groups())))
    # the one-at-a-time run: its n-th MASSPROP is the n-th solid or region the walk met
    measured = [handle for handle, kind in _walked(walk) if kind in ("3DSOLID", "REGION")]
    for handle, answer in zip(measured, single.split("Command: AREA")[1:]):
        volume, surface = re.search(rf"Volume:\s*{NUMBER}", answer), re.search(rf"Area = {NUMBER}", answer)
        if handle in objects and objects[handle]["type"] == "3DSOLID":
            if volume:
                objects[handle]["volume"] = float(volume.group(1))
            if surface:
                objects[handle]["surface"] = float(surface.group(1))
    totals = {}
    solids = mass.partition("SOLIDS")[2]
    volume = re.search(rf"Volume:\s*{NUMBER}", solids)
    if volume:
        totals["volume"] = float(volume.group(1))
        spans = re.findall(rf"[XYZ]:\s*{NUMBER}\s*--\s*{NUMBER}", solids)[:3]
        totals["box"] = (tuple(float(a) for a, _ in spans), tuple(float(b) for _, b in spans))
        centre = solids.partition("Centroid:")[2]
        totals["centroid"] = tuple(float(v) for v in re.findall(rf"[XYZ]:\s*{NUMBER}", centre)[:3])
    return objects, totals


def _polyline_length(points, bulges, closed) -> float:
    total, bulges = 0.0, list(bulges) + [0.0] * len(points)
    for i in range(len(points) if closed else len(points) - 1):
        (x1, y1), (x2, y2) = points[i][:2], points[(i + 1) % len(points)][:2]
        chord = math.hypot(x2 - x1, y2 - y1)
        angle = 4 * math.atan(abs(bulges[i]))
        total += chord * (angle / 2) / math.sin(angle / 2) if bulges[i] and chord else chord
    return total


def _path_length(points) -> float:
    return sum(math.dist(p, q) for p, q in zip(points, points[1:]))


def decoded(drawing) -> dict:
    """The same quantities LIST prints, from our reading of the drawing: {handle: {length, area, ...}}."""
    out = {}
    for e in drawing.entities:
        kind, mine = e["type"], {}
        if kind == "LINE":
            (x1, y1, z1), (x2, y2, z2) = e["start"], e["end"]
            mine = {"length": math.hypot(x2 - x1, y2 - y1), "length3d": math.dist(e["start"], e["end"])}
        elif kind == "CIRCLE":
            mine = {"length": 2 * math.pi * e["radius"], "area": math.pi * e["radius"] ** 2}
        elif kind == "ARC":
            mine = {"length": e["radius"] * ((e["end_angle"] - e["start_angle"]) % (2 * math.pi) or 2 * math.pi)}
        elif kind in ("LWPOLYLINE", "POLYLINE_2D"):
            points = [p[:2] for p in e["points"]]
            mine = {"length": _polyline_length(points, e["bulges"], e["closed"])}
            if e["closed"]:  # for an open one AutoCAD closes it its own way, which says nothing about our reading
                mine["area"] = derive.exact_area(points, e["bulges"])
        elif kind == "ELLIPSE":
            a = math.sqrt(sum(c * c for c in e["major"]))
            b, steps = a * e["ratio"], 20000
            span = (e["end_angle"] - e["start_angle"]) % (2 * math.pi) or 2 * math.pi
            angles = [e["start_angle"] + span * i / steps for i in range(steps + 1)]
            mine = {"length": _path_length([(a * math.cos(t), b * math.sin(t)) for t in angles])}
        elif kind in ("SPLINE", "HELIX"):
            knots, control, weights = e["knots"], e["control"], e["weights"]
            if not control and e.get("fit"):
                knots, control = fit_spline(e["fit"], e.get("knot_parameter", 0))
                weights = []
            if control:
                mine = {"length": _path_length(bspline_points(e["degree"], knots, control, weights, 400))}
        elif kind == "HATCH":
            # the filled area: outer loops less the islands inside them. A loop around a text (flag 8) keeps the
            # pattern off the text but does not count, as AutoCAD reckons it.
            loops = [(derive.exact_area(p["points"], p["bulges"]), p["flags"] & 1) for p in e["paths"] if not p["flags"] & 8]
            if loops:
                outer = sum(a for a, external in loops if external) or loops[0][0]
                mine = {"area": outer - sum(a for a, external in loops[1:] if not external) if any(x for _, x in loops) else outer}
        if mine:
            out[e["handle"]] = mine
    return out


def built(model) -> tuple[dict, dict]:
    """From the IFC, through IfcOpenShell's kernel: per product {box, area}, and the solids' total volume,
    box and centroid."""
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    # finer than the default, for the volume figures. Not finer still: at an angular deflection of 0.02 the
    # mesher throws points far outside some curved faces, which reads as a wrong shape when it is not one.
    settings.set("mesher-linear-deflection", 0.0002)
    settings.set("mesher-angular-deflection", 0.1)
    scale = ifcopenshell.util.unit.calculate_unit_scale(model)  # the kernel answers in metres
    products, volume, moment, low, high = {}, 0.0, [0.0, 0.0, 0.0], [math.inf] * 3, [-math.inf] * 3
    for product in model.by_type("IfcProduct"):
        handle = re.search(r":([0-9A-F]+)$", product.Name or "")
        shown = product.Representation and product.Representation.Representations[0]
        if not handle or not shown or shown.RepresentationIdentifier != "Body":
            continue
        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
        except Exception:
            products[int(handle.group(1), 16)] = {"failed": True}
            continue
        verts = [c / scale for c in shape.geometry.verts]
        points = [tuple(verts[i : i + 3]) for i in range(0, len(verts), 3)]
        faces = shape.geometry.faces
        if not points:
            continue
        box = (tuple(min(p[i] for p in points) for i in range(3)), tuple(max(p[i] for p in points) for i in range(3)))
        area = part = 0.0
        centre = [0.0, 0.0, 0.0]
        for i in range(0, len(faces), 3):
            a, b, c = (points[faces[i + k]] for k in range(3))
            u, v = [b[k] - a[k] for k in range(3)], [c[k] - a[k] for k in range(3)]
            cross = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
            area += math.sqrt(sum(x * x for x in cross)) / 2
            six = sum(a[k] * cross[k] for k in range(3))  # six times the tetrahedron to the origin
            part += six / 6
            for k in range(3):
                centre[k] += six / 24 * (a[k] + b[k] + c[k])
        products[int(handle.group(1), 16)] = {"box": box, "area": area, "volume": part, "type": product.ObjectType}
        if product.ObjectType == "3DSOLID":
            volume += part
            for k in range(3):
                moment[k] += centre[k]
                low[k], high[k] = min(low[k], box[0][k]), max(high[k], box[1][k])
    totals = {"volume": volume, "box": (tuple(low), tuple(high)), "centroid": tuple(m / volume for m in moment)} if volume else {}
    return products, totals


def compare(drawing_path: Path, log: str) -> list[tuple]:
    """Rows of (source, type, handle, quantity, AutoCAD's value, ours, relative difference)."""
    theirs, their_totals = parse(log)
    data = drawing_path.read_bytes()
    drawing = entities.load(data)
    model, _ = ifc.build(drawing, drawing_path.stem)
    read, (products, totals) = decoded(drawing), built(model)
    rows = []

    def add(source, kind, handle, quantity, want, got):
        size = max(abs(want), abs(got), 1e-9)
        rows.append((source, kind, handle, quantity, want, got, abs(want - got) / size))

    for handle, known in theirs.items():
        for quantity in ("length", "length3d", "area"):
            if quantity in known and quantity in read.get(handle, {}):
                add("decoded", known["type"], handle, quantity, known[quantity], read[handle][quantity])
        ours = products.get(handle)
        if ours and not ours.get("failed"):
            if "box" in known:
                span = max(math.dist(*known["box"]), 1e-9)
                off = max(abs(a - b) for want, got in zip(known["box"], ours["box"]) for a, b in zip(want, got))
                rows.append(("ifc", known["type"], handle, "bounding box", span, span - off, off / span))
            if "area" in known and known["type"] in ("REGION", "PLANESURFACE"):
                add("ifc", known["type"], handle, "area", known["area"], ours["area"])
            if "surface" in known:
                add("ifc", known["type"], handle, "surface area", known["surface"], ours["area"])
            if "volume" in known:
                add("ifc", known["type"], handle, "volume", known["volume"], ours["volume"])
        elif ours:
            rows.append(("ifc", known["type"], handle, "not built by the kernel", 1.0, 0.0, 1.0))
    if their_totals and totals:
        add("ifc", "all 3DSOLIDs", 0, "volume", their_totals["volume"], totals["volume"])
        span = max(math.dist(*their_totals["box"]), 1e-9)
        for name, want, got in (("bounding box", their_totals["box"], totals["box"]), ("centroid", (their_totals["centroid"],), (totals["centroid"],))):
            off = max(abs(a - b) for w, g in zip(want, got) for a, b in zip(w, g))
            rows.append(("ifc", "all 3DSOLIDs", 0, name, span, span - off, off / span))
    return rows


def report(name: str, rows: list[tuple]) -> None:
    print(f"\n{name}: {len(rows)} values compared with AutoCAD")
    groups = {}
    for row in rows:
        groups.setdefault((row[0], row[1], row[3]), []).append(row)
    print(f"  {'source':8} {'object':14} {'quantity':24} {'count':>6} {'worst difference':>18}")
    for (source, kind, quantity), group in sorted(groups.items()):
        worst = max(group, key=lambda r: r[6])
        note = f"  (handle {worst[2]:X}: AutoCAD {worst[4]:.8g}, ours {worst[5]:.8g})" if worst[6] > 1e-6 and worst[2] else ""
        print(f"  {source:8} {kind:14} {quantity:24} {len(group):6d} {worst[6]:18.2e}{note}")


if __name__ == "__main__":
    args = sys.argv[1:]
    saved = args.pop(args.index("--from-log") + 1) if "--from-log" in args else None
    keep = args.pop(args.index("--save-log") + 1) if "--save-log" in args else None
    for path in (Path(a) for a in args if not a.startswith("--")):
        text = Path(saved).read_text() if saved else autocad_log(path)
        if keep:
            Path(keep).write_text(text)
        report(path.name, compare(path, text))
