"""Maps a decoded DWG drawing to an IFC4X3 model.

Model space becomes one IfcAnnotation per entity, blocks become IfcRepresentationMaps placed by
IfcMappedItem, and DWG layers become IfcPresentationLayerAssignments.
"""

import math
import re
from collections import defaultdict

import ifcopenshell
import ifcopenshell.api.aggregate
import ifcopenshell.api.context
import ifcopenshell.api.project
import ifcopenshell.api.root
import ifcopenshell.api.spatial
import ifcopenshell.api.unit
import ifcopenshell.guid

from dwg2ifc.entities import Drawing, bspline_points, fit_spline

# DWG INSUNITS code -> (SI prefix, None) or (None, conversion unit name)
_UNITS = {1: (None, "inch"), 2: (None, "foot"), 3: (None, "mile"), 4: ("MILLI", None), 5: ("CENTI", None),
          6: (None, None), 7: ("KILO", None), 10: (None, "yard"), 14: ("DECI", None)}  # fmt: skip

# DWG entity type -> _Builder method, where the name is not simply the lowercased type
_METHODS = {"POLYLINE_2D": "lwpolyline", "ATTRIB": "text", "TRACE": "solid", "3DFACE": "face3d",
            "3DSOLID": "acis", "REGION": "acis", "BODY": "acis"}  # fmt: skip
_METHODS |= dict.fromkeys(("SURFACE", "PLANESURFACE", "EXTRUDEDSURFACE", "REVOLVEDSURFACE", "SWEPTSURFACE",
                           "LOFTEDSURFACE", "NURBSURFACE"), "acis")  # fmt: skip
# representation types that are model geometry rather than drafting
_BODY_KINDS = {"AdvancedBrep", "SurfaceModel", "Tessellation", "CSG", "SweptSolid"}

_Z = (0.0, 0.0, 1.0)
# a full circle as a quadratic NURBS: nine control points (x, y, weight) on the unit circle's bounding square
_CIRCLE = [
    (math.cos(k * math.pi / 4) * (1 if k % 2 == 0 else math.sqrt(2)), math.sin(k * math.pi / 4) * (1 if k % 2 == 0 else math.sqrt(2)),
     1.0 if k % 2 == 0 else math.sqrt(0.5))
    for k in range(9)
]  # fmt: skip
_CIRCLE_KNOTS = ([3, 2, 2, 2, 3], [0.0, 0.25, 0.5, 0.75, 1.0])  # multiplicities, knots


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(v):
    length = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    return (v[0] / length, v[1] / length, v[2] / length) if length else _Z


def _ocs(extrusion):
    """DWG arbitrary axis algorithm: the X, Y, Z axes of an entity's object coordinate system."""
    z = _unit(extrusion)
    if z == _Z:
        return (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), z
    x = _unit(_cross((0.0, 1.0, 0.0) if abs(z[0]) < 1 / 64 and abs(z[1]) < 1 / 64 else _Z, z))
    return x, _cross(z, x), z


def _frame(z, x):
    """Orthonormal X, Y, Z axes from a normal and a reference direction that may be missing or not square to it."""
    z = _unit(z)
    x = tuple(x[i] - sum(a * b for a, b in zip(x, z)) * z[i] for i in range(3))
    x = _unit(x) if sum(c * c for c in x) > 1e-18 else _ocs(z)[0]
    return x, _cross(z, x), z


def _to_wcs(axes, p):
    x, y, z = axes
    return tuple(p[0] * x[i] + p[1] * y[i] + p[2] * z[i] for i in range(3))


def _same(p, q) -> bool:
    """Points too close to make an edge. Zero-length edges crash geometry kernels (seen with IfcOpenShell)."""
    return all(math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9) for a, b in zip(p, q))


def _distinct(points):
    """Drops points that repeat their predecessor."""
    out = []
    for p in points:
        if not out or not _same(out[-1], p):
            out.append(tuple(map(float, p)))
    return out


def _plain_text(mtext: str) -> str:
    """MTEXT content without its inline formatting codes."""
    s = re.sub(r"\\P", "\n", mtext)
    s = re.sub(r"\\[ACcFfHQTWp][^;]*;|\\[LlOoKk]", "", s)
    s = re.sub(r"\\S([^;]*);", lambda m: m.group(1).replace("^", "/").replace("#", "/"), s)
    s = re.sub(r"(?<!\\)[{}]", "", s)
    return s.replace("\\~", " ").replace("\\\\", "\\").replace("\\{", "{").replace("\\}", "}")


def _kind(item) -> str:
    """The IfcShapeRepresentation type an item belongs in."""
    for ifc_class, kind in (
        ("IfcMappedItem", "MappedRepresentation"),
        ("IfcTextLiteral", "Text"),
        ("IfcCartesianPoint", "Point"),
        ("IfcAnnotationFillArea", "FillArea"),
        ("IfcTessellatedItem", "Tessellation"),
        ("IfcAdvancedBrep", "AdvancedBrep"),
        ("IfcShellBasedSurfaceModel", "SurfaceModel"),
        ("IfcCsgSolid", "CSG"),
        ("IfcSweptAreaSolid", "SweptSolid"),
    ):
        if item.is_a(ifc_class):
            return kind
    return "Curve3D"


class _Builder:
    def __init__(self, drawing: Drawing, name: str) -> None:
        self.d = drawing
        self.f = f = ifcopenshell.api.project.create_file(version="IFC4X3")
        self.by_owner = defaultdict(list)
        for e in drawing.entities:
            if not e["invisible"]:
                self.by_owner[e["owner"]].append(e)
        self.layer_items = defaultdict(list)
        self.maps: dict[int, object] = {}  # block handle -> IfcRepresentationMap, or None if empty
        self.skipped = drawing.skipped.copy()

        project = ifcopenshell.api.root.create_entity(f, ifc_class="IfcProject", name=name)
        model_space = drawing.block_named("*Model_Space")
        units = drawing.units or (drawing.blocks[model_space]["units"] if model_space else 0)
        prefix, conversion = _UNITS.get(units) or (("MILLI", None) if drawing.metric else (None, "inch"))
        length = (
            ifcopenshell.api.unit.add_conversion_based_unit(f, name=conversion)
            if conversion
            else ifcopenshell.api.unit.add_si_unit(f, unit_type="LENGTHUNIT", prefix=prefix)
        )
        # angles stay in radians, as DWG stores them
        ifcopenshell.api.unit.assign_unit(f, units=[length, ifcopenshell.api.unit.add_si_unit(f, unit_type="PLANEANGLEUNIT")])
        model = ifcopenshell.api.context.add_context(f, context_type="Model")
        self.context = ifcopenshell.api.context.add_context(
            f, context_type="Model", context_identifier="Annotation", target_view="MODEL_VIEW", parent=model
        )
        self.body_context = ifcopenshell.api.context.add_context(
            f, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=model
        )
        self.origin = f.createIfcAxis2Placement3D(f.createIfcCartesianPoint((0.0, 0.0, 0.0)))
        self.identity = f.createIfcCartesianTransformationOperator3D(None, None, self.origin.Location)

        site = ifcopenshell.api.root.create_entity(f, ifc_class="IfcSite", name="Site")
        building = ifcopenshell.api.root.create_entity(f, ifc_class="IfcBuilding", name="Building")
        storey = ifcopenshell.api.root.create_entity(f, ifc_class="IfcBuildingStorey", name="Model Space")
        ifcopenshell.api.aggregate.assign_object(f, products=[site], relating_object=project)
        ifcopenshell.api.aggregate.assign_object(f, products=[building], relating_object=site)
        ifcopenshell.api.aggregate.assign_object(f, products=[storey], relating_object=building)

        placement = f.createIfcLocalPlacement(None, self.origin)
        products = []
        for e in self.by_owner.get(model_space, []):
            items = self.items(e, ())
            if not items:
                continue
            representation = self.representation(items)
            shape = f.createIfcProductDefinitionShape(None, None, [representation])
            # solids and meshes are things in the model; everything else is drafting
            create = f.createIfcBuildingElementProxy if representation.RepresentationIdentifier == "Body" else f.createIfcAnnotation
            products.append(create(ifcopenshell.guid.new(), None, f"{e['type']}:{e['handle']:X}", None, e["type"], placement, shape))
        if products:
            ifcopenshell.api.spatial.assign_container(f, products=products, relating_structure=storey)
        for layer, items in self.layer_items.items():
            f.createIfcPresentationLayerAssignment(layer, None, items, None)

    def representation(self, items):
        groups = defaultdict(list)
        for item in items:
            groups[_kind(item)].append(item)
        if len(groups) > 1:
            # IFC has no representation type for mixed content, so each kind of geometry goes into
            # its own nested map and the representation holds mapped items only
            items = groups.pop("MappedRepresentation", [])
            for same_kind in groups.values():
                nested = self.f.createIfcRepresentationMap(self.origin, self.representation(same_kind))
                items.append(self.f.createIfcMappedItem(nested, self.identity))
            groups = {"MappedRepresentation": items}
        ((kind, items),) = groups.items()
        body = kind in _BODY_KINDS or any(
            i.is_a("IfcMappedItem") and i.MappingSource.MappedRepresentation.RepresentationIdentifier == "Body" for i in items
        )
        if body:
            return self.f.createIfcShapeRepresentation(self.body_context, "Body", kind, items)
        return self.f.createIfcShapeRepresentation(self.context, "Annotation", kind, items)

    def point(self, p):
        return self.f.createIfcCartesianPoint(tuple(map(float, p)))

    def direction(self, v):
        return self.f.createIfcDirection(tuple(map(float, v)))

    def placement(self, axes, origin):
        return self.f.createIfcAxis2Placement3D(self.point(origin), self.direction(axes[2]), self.direction(axes[0]))

    def block(self, handle, parents):
        """The IfcRepresentationMap for a block, built on first use. None if it has nothing to draw."""
        if handle in self.maps:
            return self.maps[handle]
        if handle in parents:  # a block that contains itself
            return None
        items = [i for e in self.by_owner.get(handle, []) for i in self.items(e, (*parents, handle))]
        result = self.f.createIfcRepresentationMap(self.origin, self.representation(items)) if items else None
        self.maps[handle] = result
        return result

    def items(self, e, parents) -> list:
        """One DWG entity as IFC representation items, in its block's coordinates."""
        kind = e["type"]
        name = "insert" if kind.startswith("DIMENSION") else _METHODS.get(kind, kind.lower())
        try:
            found = getattr(self, "_" + name)(e, parents)
        except (ValueError, ZeroDivisionError, OverflowError):  # geometry too broken to express
            found = None
        found = [] if found is None else found if isinstance(found, list) else [found]
        if not found:
            self.skipped[kind + " (empty)"] += 1
        self.layer_items[self.d.layers.get(e["layer"], "0")] += found
        return found

    def polyline(self, points):
        points = _distinct(points)
        return self.f.createIfcPolyline([self.point(p) for p in points]) if len(points) > 1 else None

    def polycurve(self, points, bulges, closed, elevation, extrusion):
        """A 2D polyline with bulge arcs, as LWPOLYLINE, POLYLINE_2D and hatch loops store it."""
        if len(points) < 2:
            return None
        axes = _ocs(extrusion)
        bulges = list(bulges) + [0.0] * (len(points) - len(bulges))
        coords, segments = [points[0]], []
        for i in range(len(points) if closed else len(points) - 1):
            (x1, y1), (x2, y2) = coords[-1], points[(i + 1) % len(points)]
            if _same((x1, y1), (x2, y2)):
                continue
            start = len(coords)
            if bulges[i]:
                # arc through the point where the bulge is deepest: half the chord times the bulge, off the midpoint
                half = bulges[i] / 2
                coords.append(((x1 + x2) / 2 + half * (y2 - y1), (y1 + y2) / 2 - half * (x2 - x1)))
                segments.append(self.f.createIfcArcIndex((start, start + 1, start + 2)))
            else:
                segments.append(self.f.createIfcLineIndex((start, start + 1)))
            coords.append((x2, y2))
        if not segments:
            return None
        listed = self.f.createIfcCartesianPointList3D([_to_wcs(axes, (float(x), float(y), elevation)) for x, y in coords])
        return self.f.createIfcIndexedPolyCurve(listed, segments, False)

    def faces(self, points, faces):
        """A face set from points and 1-based index loops; loops that collapse to fewer than 3 corners are dropped."""
        loops = []
        for face in faces:
            loop = [i for n, i in enumerate(face) if 1 <= i <= len(points) and i not in face[:n]]
            if len(loop) >= 3:
                loops.append(self.f.createIfcIndexedPolygonalFace(loop))
        if not loops:
            return None
        listed = self.f.createIfcCartesianPointList3D([tuple(map(float, p)) for p in points])
        return self.f.createIfcPolygonalFaceSet(listed, None, loops)

    def _line(self, e, _):
        return self.polyline([e["start"], e["end"]])

    def _leader(self, e, _):
        return self.polyline(e["points"])

    def _point(self, e, _):
        return self.point(e["point"])

    def _circle(self, e, _):
        axes = _ocs(e["extrusion"])
        if e["radius"] <= 0:
            return None
        return self.f.createIfcCircle(self.placement(axes, _to_wcs(axes, e["center"])), e["radius"])

    def trimmed(self, curve, start, end):
        value = self.f.createIfcParameterValue
        return self.f.createIfcTrimmedCurve(curve, [value(start)], [value(end)], True, "PARAMETER")

    def _arc(self, e, _):
        circle = self._circle(e, _)
        return circle and self.trimmed(circle, e["start_angle"], e["end_angle"])

    def _ellipse(self, e, _):
        major = math.sqrt(sum(c * c for c in e["major"]))
        if not major or e["ratio"] <= 0:
            return None
        placement = self.placement((_unit(e["major"]), None, _unit(e["extrusion"])), e["center"])
        ellipse = self.f.createIfcEllipse(placement, major, major * e["ratio"])
        if abs(e["end_angle"] - e["start_angle"] - 2 * math.pi) < 1e-9:
            return ellipse
        return self.trimmed(ellipse, e["start_angle"], e["end_angle"])

    def _lwpolyline(self, e, _):
        points = [p[:2] for p in e["points"]]  # POLYLINE_2D vertices carry a z that is always 0
        return self.polycurve(points, e["bulges"], e["closed"], e["elevation"], e["extrusion"])

    def _polyline_3d(self, e, _):
        return self.polyline(e["points"] + e["points"][:1] if e["closed"] else e["points"])

    def _polyline_pface(self, e, _):
        return self.faces(e["points"], [[abs(i) for i in face] for face in e["faces"]])

    def _polyline_mesh(self, e, _):
        m, n = e["m"], e["n"]
        if m * n > len(e["points"]):
            return None
        at = lambda i, j: (i % m) * n + (j % n) + 1  # noqa: E731
        quads = [
            [at(i, j), at(i + 1, j), at(i + 1, j + 1), at(i, j + 1)]
            for i in range(m if e["closed_m"] else m - 1)
            for j in range(n if e["closed_n"] else n - 1)
        ]
        return self.faces(e["points"], quads)

    def _face3d(self, e, _):
        return self.faces(_distinct(e["corners"]), [[1, 2, 3, 4]])

    def axes(self, origin, z, x):
        """A placement from a normal and a reference direction that may be missing or not square to it."""
        return self.placement(_frame(z, x), origin)

    def nurbs_knots(self, knots, counts, degree):
        """ACIS repeats end knots `degree` times; IFC wants one more. Returns None if the result is not valid."""
        distinct, total = [], []
        for k, m in zip(knots, counts):
            if distinct and math.isclose(k, distinct[-1], rel_tol=1e-12, abs_tol=1e-12):
                total[-1] += m
            else:
                distinct.append(float(k))
                total.append(int(m))
        if len(distinct) < 2 or distinct != sorted(distinct):
            return None
        total[0] += 1
        total[-1] += 1
        if max(total[0], total[-1]) > degree + 1 or any(m > degree for m in total[1:-1]):
            return None
        return total, distinct

    def acis_curve(self, curve):
        if curve[0] == "line":
            if not any(curve[2]):
                return None
            return self.f.createIfcLine(self.point(curve[1]), self.f.createIfcVector(self.direction(_unit(curve[2])), 1.0))
        if curve[0] == "ellipse":
            _, center, normal, major, ratio = curve
            radius = math.sqrt(sum(c * c for c in major))
            if not radius or ratio <= 0:
                return None
            placement = self.axes(center, normal, major)
            if math.isclose(ratio, 1.0, rel_tol=1e-12):
                return self.f.createIfcCircle(placement, radius)
            return self.f.createIfcEllipse(placement, radius, radius * ratio)
        _, degree, knots, counts, points = curve
        fixed = self.nurbs_knots(knots, counts, degree)
        if fixed is None or len(points) <= degree:
            return None
        args = [degree, [self.point(p[:3]) for p in points], "UNSPECIFIED", False, False, *fixed, "UNSPECIFIED"]
        if len(points[0]) == 4:
            return self.f.createIfcRationalBSplineCurveWithKnots(*args, [float(p[3]) for p in points])
        return self.f.createIfcBSplineCurveWithKnots(*args)

    def revolution(self, center, axis, reference, profile):
        """IfcSurfaceOfRevolution of a 2D (radius, height) curve about an axis."""
        x, _, z = _frame(axis, reference)
        f = self.f
        position = self.placement((x, None, _cross(x, z)), center)  # profile plane: X is the radius, Y the axis
        # the axis is given in the surface's own space, not inside the profile's placement (as IfcOpenShell reads it)
        around = f.createIfcAxis1Placement(self.point(center), self.direction(z))
        return f.createIfcSurfaceOfRevolution(f.createIfcArbitraryOpenProfileDef("CURVE", None, profile), position, around)

    def revolved(self, center, axis, reference, profile, degree, counts, knots, ratio=1.0):
        """Exact NURBS surface made by turning a profile of (radius, height, weight) points about an axis.

        `ratio` squashes the circle into an ellipse, which a NURBS surface follows exactly.
        """
        x, y, z = _frame(axis, reference)
        grid = [
            [tuple(center[i] + r * (cx * x[i] + cy * ratio * y[i]) + h * z[i] for i in range(3)) for r, h, _ in profile]
            for cx, cy, _ in _CIRCLE
        ]
        weights = [[w * cw for _, _, w in profile] for _, _, cw in _CIRCLE]
        return self.f.createIfcRationalBSplineSurfaceWithKnots(
            2, degree, [[self.point(p) for p in row] for row in grid], "UNSPECIFIED", False, False, False,
            _CIRCLE_KNOTS[0], counts, _CIRCLE_KNOTS[1], knots, "UNSPECIFIED", weights,
        )  # fmt: skip

    def acis_surface(self, surface, face):
        """(IFC surface, flipped): ACIS marks inward-facing cylinders, spheres and tori by a sign; IFC's always face out."""
        kind = surface[0]
        if kind == "plane":
            return self.f.createIfcPlane(self.axes(surface[1], surface[2], surface[3])), False
        if kind == "cone":
            _, center, axis, major, ratio, sine, cosine = surface
            radius = math.sqrt(sum(c * c for c in major))
            if not radius or not cosine or ratio <= 0:
                return None, False
            if abs(sine) < 1e-12 and math.isclose(ratio, 1.0, rel_tol=1e-12):
                return self.f.createIfcCylindricalSurface(self.axes(center, axis, major), radius), cosine < 0
            # IFC has no cone and no elliptical cylinder: revolve a straight profile that spans the face.
            # The radius changes by sine/cosine per unit of height (checked against AutoCAD's cones).
            z = _unit(axis)
            heights = [sum((p[i] - center[i]) * z[i] for i in range(3)) for loop in face["loops"] for _, a, b, *_ in loop for p in (a, b) if p]
            low, high = (min(heights), max(heights)) if heights else (-radius, radius)
            margin = 0.05 * (high - low) or radius
            low, high = low - margin, high + margin
            slope = sine / cosine
            if slope:  # stop at the apex: beyond it lies the mirror cone
                apex = -radius / slope
                low, high = (max(low, apex), high) if slope > 0 else (low, min(high, apex))
            if high <= low:
                return None, False
            ends = [(radius + slope * h, h, 1.0) for h in (low, high)]
            if math.isclose(ratio, 1.0, rel_tol=1e-12):
                line = self.f.createIfcPolyline([self.f.createIfcCartesianPoint((float(r), float(h))) for r, h, _ in ends])
                return self.revolution(center, axis, major, line), cosine < 0
            return self.revolved(center, axis, major, ends, 1, [2, 2], [0.0, 1.0], ratio), cosine < 0
        if kind == "sphere":
            _, center, radius, u, pole = surface
            return (self.f.createIfcSphericalSurface(self.axes(center, pole, u), abs(radius)), radius < 0) if radius else (None, False)
        if kind == "torus":
            _, center, axis, major, minor, u = surface
            if not minor:
                return None, False
            if 0 < abs(minor) < major:
                return self.f.createIfcToroidalSurface(self.axes(center, axis, u), major, abs(minor)), minor < 0
            # IfcToroidalSurface cannot describe a torus that crosses its own axis, so revolve the tube circle
            origin = self.f.createIfcAxis2Placement2D(self.f.createIfcCartesianPoint((float(major), 0.0)))
            tube = self.trimmed(self.f.createIfcCircle(origin, abs(minor)), 0.0, 2 * math.pi)
            return self.revolution(center, axis, u, tube), minor < 0
        _, degree_u, degree_v, ku, mu, kv, mv, grid = surface
        fixed_u, fixed_v = self.nurbs_knots(ku, mu, degree_u), self.nurbs_knots(kv, mv, degree_v)
        if fixed_u is None or fixed_v is None or not grid or not grid[0]:
            return None, False
        args = [degree_u, degree_v, [[self.point(p[:3]) for p in row] for row in grid], "UNSPECIFIED", False, False, False,
                fixed_u[0], fixed_v[0], fixed_u[1], fixed_v[1], "UNSPECIFIED"]  # fmt: skip
        if len(grid[0][0]) == 4:
            return self.f.createIfcRationalBSplineSurfaceWithKnots(*args, [[float(p[3]) for p in row] for row in grid]), False
        return self.f.createIfcBSplineSurfaceWithKnots(*args), False

    def whole_cone(self, body):
        """A plain pointed cone as a revolved triangle, or None.

        As a B-rep its side is bounded by one circle and the apex point, which IfcOpenShell fails to build,
        and it does not build IfcRightCircularCone either.
        """
        kinds = sorted(face["surface"][0] if face["surface"] else "" for face in body["faces"])
        if kinds != ["cone", "plane"] or any(face["double"] for face in body["faces"]):
            return None
        side = next(face for face in body["faces"] if face["surface"][0] == "cone")
        rims = [edge[3] for loop in side["loops"] for edge in loop if edge[3] not in (None, "point")]
        tips = [edge[1] for loop in side["loops"] for edge in loop if edge[3] == "point"]
        if len(rims) != 1 or len(tips) != 1 or rims[0][0] != "ellipse" or not math.isclose(rims[0][4], 1.0, rel_tol=1e-12):
            return None
        center, radius = rims[0][1], math.sqrt(sum(c * c for c in rims[0][3]))
        up = tuple(tips[0][i] - center[i] for i in range(3))
        height = math.sqrt(sum(c * c for c in up))
        if not height or not radius:
            return None
        f = self.f
        x, _, z = _frame(up, rims[0][3])
        corners = [f.createIfcCartesianPoint(p) for p in ((0.0, 0.0), (radius, 0.0), (0.0, height), (0.0, 0.0))]
        profile = f.createIfcArbitraryClosedProfileDef("AREA", None, f.createIfcPolyline(corners))
        return self.turned(profile, center, x, z)

    def turned(self, profile, center, x, z):
        """IfcRevolvedAreaSolid: a profile in the plane of the radius (X) and the axis (Y), turned fully about the axis."""
        f = self.f
        around = f.createIfcAxis1Placement(f.createIfcCartesianPoint((0.0, 0.0, 0.0)), self.direction((0.0, 1.0, 0.0)))
        return f.createIfcRevolvedAreaSolid(profile, self.placement((x, None, _cross(x, z)), center), around, 2 * math.pi)

    def acis_body(self, body, kind):
        """One ACIS body as an IfcAdvancedBrep, or as an open surface model if it is a sheet or has faces we cannot map."""
        f = self.f
        vertices, edges, faces = {}, {}, []
        cone = self.whole_cone(body)
        if cone is not None:
            return cone
        only = body["faces"][0]
        if len(body["faces"]) == 1 and not only["loops"] and not only["double"] and only["surface"]:
            # a whole sphere or ring torus has no edges to bound a B-rep face, and IFC has primitives for both
            s = only["surface"]
            if s[0] == "sphere" and s[2]:
                return f.createIfcCsgSolid(f.createIfcSphere(self.axes(s[1], s[4], s[3]), abs(s[2])))
            if s[0] == "torus" and 0 < abs(s[4]) < s[3]:
                x, _, z = _frame(s[2], s[5])
                profile = f.createIfcCircleProfileDef("AREA", None, f.createIfcAxis2Placement2D(f.createIfcCartesianPoint((float(s[3]), 0.0))), abs(s[4]))
                return self.turned(profile, s[1], x, z)

        def vertex(p):
            key = tuple(round(c, 9) for c in p)
            if key not in vertices:
                vertices[key] = f.createIfcVertexPoint(self.point(p))
            return vertices[key]

        closed = True
        for face in body["faces"]:
            surface, flipped = self.acis_surface(face["surface"], face) if face["surface"] else (None, False)
            loops, ok = [], surface is not None
            for loop in face["loops"] if ok else ():
                oriented = []
                for key, start, end, curve, same_sense, forward in loop:
                    if curve == "point":
                        continue  # the neighbouring edges already meet at this vertex
                    if key not in edges:
                        geometry = self.acis_curve(curve) if curve and start and end else None
                        edges[key] = geometry and f.createIfcEdgeCurve(vertex(start), vertex(end), geometry, same_sense)
                    if edges[key] is None:
                        ok = False
                        break
                    oriented.append(f.createIfcOrientedEdge(None, None, edges[key], forward))
                if oriented:
                    points = [p for _, start, end, *_ in loop for p in (start, end)]
                    extent = sum((max(c) - min(c)) ** 2 for c in zip(*points))
                    loops.append((extent, f.createIfcEdgeLoop(oriented)))
                elif ok and loop and loop[0][1]:  # a loop that is one point: the apex of a cone
                    loops.append((0, f.createIfcVertexLoop(vertex(loop[0][1]))))
            if not ok:
                closed = False
                self.skipped[f"{kind} face (unsupported geometry)"] += 1
                continue
            if not loops:  # a whole sphere or torus has no edges; IFC still wants a bound, so use a point on it
                s = face["surface"]
                on_surface = [s[1][i] + s[2] * _unit(s[4])[i] for i in range(3)] if s[0] == "sphere" else s[1]
                loops.append((0, f.createIfcVertexLoop(vertex(on_surface))))
            outer = max(range(len(loops)), key=lambda i: loops[i][0])
            bounds = [(f.createIfcFaceOuterBound if i == outer else f.createIfcFaceBound)(loop, True) for i, (_, loop) in enumerate(loops)]
            faces.append(f.createIfcAdvancedFace(bounds, surface, face["same_sense"] != flipped))
            closed = closed and not face["double"]
        if not faces:
            return None
        if closed:
            return f.createIfcAdvancedBrep(f.createIfcClosedShell(faces))
        return f.createIfcShellBasedSurfaceModel([f.createIfcOpenShell(faces)])

    def _acis(self, e, _):
        if e["bodies"] is None:
            return None
        return [item for body in e["bodies"] if (item := self.acis_body(body, e["type"])) is not None]

    def fill(self, outer, inner=()):
        return self.f.createIfcAnnotationFillArea(outer, list(inner) or None)

    def _solid(self, e, _):
        a, b, c, d = e["corners"]  # stored in a Z pattern: the last two are swapped relative to the outline
        outline = self.polycurve([a, b, d, c], [], True, e["elevation"], e["extrusion"])
        return outline and self.fill(outline)

    def _hatch(self, e, _):
        # shortcut: a loop flagged external starts a new filled area and the loops after it are its holes;
        # nest by containment instead if drawings turn up whose flags do not follow that order
        areas = []
        for path in e["paths"]:
            curve = self.polycurve(path["points"], path["bulges"], True, e["elevation"], e["extrusion"])
            if curve is None:
                continue
            if path["flags"] & 1 or not areas:
                areas.append([curve])
            else:
                areas[-1].append(curve)
        return [self.fill(outer, holes) for outer, *holes in areas]

    def _spline(self, e, _):
        degree, control, knots, weights = e["degree"], e["control"], e["knots"], e["weights"]
        if not control:  # only fit points are stored; AutoCAD derives the curve from them
            # shortcut: end tangents and closed fit splines are treated as open with natural ends
            knots, control = fit_spline(e["fit"], e["knot_parameter"])
            degree, weights = 3, []
            if not control:
                return self.polyline(e["fit"])
        distinct, counts = [], []
        for k in knots:
            if distinct and math.isclose(k, distinct[-1], rel_tol=1e-12, abs_tol=1e-12):
                counts[-1] += 1
            else:
                distinct.append(float(k))
                counts.append(1)
        valid = (
            degree >= 1
            and len(control) > degree
            and len(knots) == len(control) + degree + 1
            and len(distinct) >= 2
            and distinct == sorted(distinct)
            and max(counts[0], counts[-1]) <= degree + 1
            and all(c <= degree for c in counts[1:-1])
            and (not weights or (len(weights) == len(control) and min(weights) > 0))
        )
        if not valid:  # IFC would reject it, so keep the shape as a polyline
            return self.polyline(bspline_points(degree, knots, control, weights))
        args = [degree, [self.point(p) for p in control], "UNSPECIFIED", e["closed"], False, counts, distinct, "UNSPECIFIED"]
        if weights:
            return self.f.createIfcRationalBSplineCurveWithKnots(*args, [float(w) for w in weights])
        return self.f.createIfcBSplineCurveWithKnots(*args)

    def text(self, e, text, point, x_axis, alignment):
        lines = text.split("\n")
        if not text.strip() or e["height"] <= 0:
            return None
        # shortcut: extent is estimated at 0.6 x height per character, measure real glyphs if layout matters
        extent = self.f.createIfcPlanarExtent(0.6 * e["height"] * max(map(len, lines)), 1.5 * e["height"] * len(lines))
        placement = self.placement((x_axis, None, _unit(e["extrusion"])), point)
        return self.f.createIfcTextLiteralWithExtent(text, placement, "RIGHT", extent, alignment)

    def _text(self, e, _):
        axes = _ocs(e["extrusion"])
        x_axis = _to_wcs(axes, (math.cos(e["rotation"]), math.sin(e["rotation"]), 0.0))
        return self.text(e, e["text"], _to_wcs(axes, e["point"]), x_axis, "bottom-left")

    def _mtext(self, e, _):  # insertion point and x axis are already in world coordinates
        return self.text(e, _plain_text(e["text"]), e["point"], _unit(e["x_axis"]), "top-left")

    def _insert(self, e, parents, offset=(0.0, 0.0)):
        block = self.d.blocks.get(e["block"])
        source = self.block(e["block"], parents) if block else None
        sx, sy, sz = e["scale"]
        if source is None or not (sx and sy and sz):
            return None
        x, y, z = _ocs(e["extrusion"])
        cos, sin = math.cos(e["rotation"]), math.sin(e["rotation"])
        # the insert's own axes: the object coordinate system turned by the rotation
        rx = tuple(cos * x[i] + sin * y[i] for i in range(3))
        ry = tuple(cos * y[i] - sin * x[i] for i in range(3))
        # a negative scale mirrors by flipping its axis, since IFC scales must be positive
        ax = tuple(math.copysign(1, sx) * c for c in rx)
        ay = tuple(math.copysign(1, sy) * c for c in ry)
        az = tuple(math.copysign(1, sz) * c for c in z)
        sx, sy, sz = abs(sx), abs(sy), abs(sz)
        bx, by, bz = block["base"]
        origin = _to_wcs((x, y, z), e["point"])
        origin = tuple(
            origin[i] + offset[0] * rx[i] + offset[1] * ry[i] - sx * bx * ax[i] - sy * by * ay[i] - sz * bz * az[i]
            for i in range(3)
        )
        target = self.f.createIfcCartesianTransformationOperator3DnonUniform(
            self.direction(ax), self.direction(ay), self.point(origin), sx, self.direction(az), sy, sz
        )
        return self.f.createIfcMappedItem(source, target)

    def _minsert(self, e, parents):
        cells = [
            self._insert(e, parents, (column * e["column_spacing"], row * e["row_spacing"]))
            for row in range(max(1, min(e["rows"], 1000)))
            for column in range(max(1, min(e["columns"], 1000)))
        ]
        return [c for c in cells if c is not None]


def build(drawing: Drawing, name: str) -> tuple[ifcopenshell.file, dict[str, int]]:
    """Returns the IFC model and a count of what could not be converted, by entity type."""
    builder = _Builder(drawing, name)
    return builder.f, dict(builder.skipped)
