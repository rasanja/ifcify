# dwg2ifc

A local web service that converts AutoCAD DWG drawings to IFC4X3.

It reads the DWG binary format directly. It does not need AutoCAD, accoreconsole or any other Autodesk
software, makes no network calls, and runs headless.

## What it converts

DWG releases R14, R2000, R2004, R2007, R2010, R2013 and R2018.

| DWG | IFC |
|-----|-----|
| Lines, arcs, circles, ellipses, polylines, splines, leaders | curves in an `IfcAnnotation` |
| Text, multiline text, attributes | `IfcTextLiteralWithExtent`, with its colour, font and height as an `IfcTextStyle` |
| Hatches, 2D solids | `IfcAnnotationFillArea`, a patterned hatch with its lines as `IfcFillAreaStyleHatching` |
| Blocks and their inserts, dimensions, tables | `IfcRepresentationMap` placed by `IfcMappedItem` |
| Tolerances, multilines | text and curves |
| Rays, construction lines | `IfcLine`, which is unbounded like they are |
| Lines, arcs, circles, polylines drawn with a thickness | faces standing on the curve (`IfcShellBasedSurfaceModel`), in an `IfcBuildingElementProxy` |
| 3D solids, regions, surfaces | `IfcAdvancedBrep` or a surface model, in an `IfcBuildingElementProxy` |
| Meshes, polyface meshes, 3D faces, subdivision meshes | `IfcPolygonalFaceSet` |
| Helices | `IfcBSplineCurveWithKnots`, with the radius, turns and turn height as properties |
| Multileaders | leader lines and one text (or the block they hold) |
| Images, wipeouts | the outline of the frame; an image's file path as a property (the picture is not in the DWG) |
| Lights | `IfcLightFixture` at the light's position, with its name, kind and intensity |
| PDF, DWF and DGN underlays | the clip outline, or the insertion point when the page is not clipped; file and page as properties |
| Shapes | a point, with the shape file and number as properties (the symbol is not in the DWG) |
| Other custom entities | drawn from the picture of themselves they carry, when they have one |
| AutoCAD Architecture walls, doors, windows, openings | `IfcWall`, `IfcDoor`, `IfcWindow`, `IfcOpeningElement`, with their stored sizes and styles as types |
| Groups | `IfcGroup` |
| Materials on solids | `IfcMaterial` |
| Geographic location in a projected coordinate system | `IfcMapConversion` and `IfcProjectedCRS` |
| Layers | `IfcPresentationLayerAssignment` |
| Colours, line weights, linetypes at their scale | `IfcStyledItem` with curve, fill or surface styles |
| Drawing units | IFC length unit |

## Building elements

A DWG holds lines on layers, not walls and rooms. The converter adds building information in three ways:

- **Properties.** Every converted entity carries its layer, entity type, colour and linetype. Block inserts
  carry their block name and their attributes (door numbers, room data). Data attached by other programs
  is carried over too, both kinds a DWG has: extended data (`DWG_XData_<program>`) and records in the
  entity's extension dictionary (`DWG_Record_<name>`). Derived rooms carry the records of their outline.
- **Classes and types from names.** A layer or block whose name says what it is (`A-DOOR`, `Windows`,
  `PRTA0001`, `Fluorescent Fixture`) makes its solids and block inserts IFC doors, windows, light fixtures,
  furniture and so on. A named block recognised this way becomes a type and its inserts occurrences of it;
  blocks whose names are not recognised stay plain drafting, without a type.
  An insert of a dynamic block that was stretched or flipped is drawn by AutoCAD from a private copy
  (`*U12`); the drawing stores which block it was made from, and that name is the one used for the
  element's name, class and type. What its parameters are set to is in `DWG_BlockParameters`, under
  the labels the block's author gave them (`Door Size` 30, `Hinge` Right, `Opening Angle` Open 30º).
  Read today: linear and constraint distances, flip states and visibility states. A distance is
  stored as its two end points, so the figure is the distance between them. Rotation, polar, XY,
  point and lookup parameters are not read yet and are simply absent from the set.
- **Elements derived from plans.** Room outlines become `IfcSpace` with a floor area, and a name when there
  is text inside the outline. Pairs of
  parallel lines on wall layers become `IfcWall` with a thickness and a wall type; a gap with a door or window
  drawn in it becomes an opening in the wall, filled by an `IfcDoor` or `IfcWindow`. Closed shapes on column
  layers become `IfcColumn`. The original linework is kept alongside. These are 3D only when a height is
  known; see "Heights" below.

**Real building objects.** A drawing made with AutoCAD Architecture can hold walls, doors, windows and
openings as objects instead of linework. Those are converted directly: a wall with its stored height,
width, length and position; a door or window with its stored width, height and sill; each opening cut
into the wall it belongs to and filled by its door or window; each style (`Brick-X`, `Hinged - Double`)
as an IFC type. Nothing about them is assumed. Where a drawing has such walls, walls are not also
guessed from its linework. Their 2D plan symbols are kept alongside as drafting.

Each type carries what its style stores:

- `DWG_ArchitectureStyle`: the style's own sizes as lengths. For a door: frame width and depth, stop
  width and depth, door and glass thickness. For a window: frame, sash and glass. For a wall: default
  width, base height, roof-line and floor-line offsets, cleanup radius.
- The drawing's schedule property sets (AutoCAD's "property set definitions", such as `DoorStyles` or
  `ManufacturerStyles`), under their own names, with each value as the number, text or yes/no it is
  stored as. A set attached to a single wall, door or window goes on that element. Properties AutoCAD
  fills in by itself when it draws a schedule (a wall's length, a door's size) are not stored in the
  set and so are not in it; the element's own stored sizes are in `DWG_Architecture`.
- For a wall, its layers (`IfcMaterialLayerSet`): each of the style's components with its name and
  material. A component's width is stored as a rule ("the wall's width", "half the wall's width plus
  10"); the rule is applied to each wall's stored width the way AutoCAD applies it, and the layers
  start at the side of the wall's stored box.

Derived elements are an interpretation and are marked as such. Recognition depends on layer and block
naming, so a drawing with meaningless names produces drafting and generic objects only. Import into Revit
has not been tested.

### Heights: nothing is assumed

A 2D plan shows where a wall is and how thick it is; nothing in it says how tall. The converter does not
make a height up. For each wall, room and column read out of flat linework, a height comes from one of
three places, in this order:

| Source | When | What is written |
|--------|------|-----------------|
| The drawing | Its lines were drawn with a thickness, which is how a flat drawing stores a height | A 3D body of that height |
| The request | You pass `storey_height` (and `door_height`, `window_height`, `window_sill` for openings), in metres | A 3D body of that height |
| Neither | | A flat outline in plan, with its axis. No body, and openings are not cut |

Each such element says which it was, in `DWG_Derived.Height` or `DWG_Room.Height`.

```sh
curl --data-binary @plan.dwg -o plan.ifc \
  "http://127.0.0.1:8000/convert?storey_height=3&door_height=2.1&window_height=1.2&window_sill=0.9"
```

| Size | Where it comes from |
|------|---------------------|
| Wall length, position and thickness | measured from the drawing |
| Door and window width | measured from the gap in the wall |
| Room outline and floor area | measured from the drawing, or the area AutoCAD stored for it |
| Column cross-section | measured from the drawing |
| Wall, room and column height | the drawing's line thickness, else the request, else none |
| Door and window height, window sill | the request, else the opening is not cut |
| A door or window found only as a gap | drawn as a block filling its opening, as deep as the wall |
| Storey elevation | 0; a drawing has one model space and no storeys |

This applies only to elements *derived* from linework. Elements that the drawing holds as objects need
none of it, because their sizes are stored: 3D solids, and AutoCAD Architecture walls, doors and windows
(`DWG_Architecture`). One thing is simplified there: an Architecture door or window is drawn as a plain
block filling its opening, as deep as the object's stored box but no deeper than the wall, not as a
frame and a leaf.

Floor areas never depend on a height. Volumes and wall-face areas do, so they are as good as the height
they were given.

### What to trust in a flat 2D drawing

Nothing is assumed about plan geometry, but not everything in the output is exact, and some of it is a
reading of the drawing rather than a fact in it.

**Exact.** Positions and sizes of lines, arcs, circles, ellipses, polylines, points, splines with control
points, solid fills, block placements (position, scale, rotation, mirroring), and text content and
position. These are checked value for value against AutoCAD's own export of a test drawing, in each
supported release. Layers, colours, line weights and linetype patterns are read directly from the file;
colours given as a palette number are looked up in AutoCAD's own colour table.

**Stored by AutoCAD in the drawing, and carried over as stored.** Where a drawing holds a value, it is
used rather than recomputed:

- Field values. A drawing can keep a computed property next to an object, most often the area of a room
  outline. Both forms AutoCAD stores are read: the text it displays (`405 SF`, written as `StoredArea`)
  and the unrounded number behind it, which becomes the room's floor area. Rooms without a stored area
  are measured from their outline, and `AreaSource` says which it was. On the sample plan the stored and
  measured areas agree to 0.01 percent.
- The measured size of multiline text.
- The width of a wide polyline, when it is the same along its length.
- The thickness (extrusion height) of a 2D entity. A line, arc, circle or polyline drawn with one is
  written as the surface it describes: a flat panel on a line, part of a cylinder on an arc, with exact
  surfaces and edges; a filled shape with a thickness is a solid block. It is also the height of a wall,
  room or column read from lines that have one. A circle is written as its side only, without the caps
  AutoCAD shades on it.
- A dimension's measurement.
- The sizes a solid was made with. AutoCAD keeps the length, width and height of a box or wedge, the
  radius of a sphere, the height and radii of a cylinder or cone, and the two radii of a torus, in the
  solid's history. They are written to `DWG_Stored` when that primitive is the solid's only modelling
  step; after a fillet, a cut or a union they no longer describe the solid and are left out. Drawings
  saved as R2004 or older have no history.
- A helix's radius, number of turns and turn height.
- Text style: the font each text is drawn with, and whether it is slanted.
- Where text is hung from: single-line text by its alignment point and justification (left, centre, right;
  baseline, middle, top), multiline text by its attachment point. A text's width factor and slant, which
  IFC text cannot express, are kept as properties.
- Linetype scale: dash and gap lengths are the pattern's, times the drawing's scale, times the entity's.
- Hatch patterns: each family of parallel lines (direction, spacing, dashes) as an
  `IfcFillAreaStyleHatching`. Few viewers draw these; a patterned hatch then shows as its outline.
- Layout names, group names and descriptions, and material names.
- The value and tag of a multi-line block attribute (R2018 files; saved to an older release, AutoCAD
  itself splits it into one plain attribute per line, tagged `NAME_001`, and that is what the file holds).

**Exact only if the unit is right.** Coordinates are written in the drawing's own unit. Real sizes, and
floor areas in square metres, depend on which unit that is, and if it is wrong every real-world size is
wrong by the same factor. The unit is taken from the first of these that the drawing has:

1. the `units` parameter of the request, if you give one;
2. the unit an AutoCAD Architecture drawing was set up in. This can differ from the next one: one test
   drawing declares inches but was set up, and drawn, in millimetres;
3. the unit the drawing declares (INSUNITS);
4. the model-space block's own unit;
5. failing all of those, the drawing's measurement setting, which says only "imperial" or "metric". That
   is a guess between inches and millimetres.

Which one was used, and why, is in the `X-DWG-Units` response header and in the `DWG_Units` property
set on the IFC project. Check it before trusting any size.

**Approximate.**

- Splines drawn through fit points are recomputed; end tangents and closed splines are not honoured.
- "By block" colour and linetype take the layer's. Palette colour 7, which AutoCAD shows white on a dark
  screen and black on paper, is written black for linework and white on solids.
- Text: single-line text stores no size, so the box around it is estimated from the character count.
  Multiline text loses its inline formatting. An SHX font is named by its file (`romans.shx`).
- Subdivision meshes are written as the cage they are smoothed from; a mesh with smoothing levels comes
  out faceted.
- A solid with different materials on different faces lists them all; which face has which is not kept.
- Dimensions saved without their drawn form are redrawn as plain lines and a number, without arrowheads
  and without the drawing's unit format (a length shows as `144`, not `12'-0"`).

**Present in the drawing but not carried over.**

- The width of a polyline that tapers or changes width along its length; it comes out with no width.
- Hatch holes rely on the order of the hatch's loops; gradient fills become plain.
- Shapes or text inside a linetype.
- Arrowheads of a multileader saved without its picture, and the frame of a wipeout as a mask: it is an
  outline only and hides nothing.
- A geographic location given as latitude and longitude (it needs a map projection); only locations in a
  projected coordinate system are written. The written form has not been checked against a geolocated
  drawing, as none was available.

**A reading, not a fact.** The derived elements are found by rules, and a rule can pick the wrong lines:

- A wall is any two parallel lines on a wall layer that are 7 to 65 cm apart. The thickness is the true
  distance between those two lines, but nothing guarantees they are the two faces of one wall.
- A wall's length is the stretch along which its two lines run side by side. Corners are not joined, so
  it is not the wall's length as an architect would give it, and lengths do not add up to a perimeter.
- A door or window width is the gap in the wall lines, frame included.
- A room's name is whatever short text with a digit lies inside its outline.
- What something is (door, window, column) comes from layer and block names.

Model space goes into one storey: `IfcProject > IfcSite > IfcBuilding > IfcBuildingStorey`. A drawing
with nothing in model space, such as a title block, is exported as its paper-space layouts instead,
one storey per layout, named after the layout.

A drawing placed far from the origin, as survey drawings are, is written relative to a local origin so
viewers can display it. Its real position is kept in the file as `IfcMapConversion`.

Entities on layers that are switched off or frozen, and entities marked invisible, are left out, as
AutoCAD hides them. Room outlines are the exception: they are read from hidden layers too, because
offices often keep them there.

Not converted: custom entities that carry no picture of themselves, and paper-space layouts of drawings
that also have a model. Embedded OLE objects are in the first group too. An underlay's page is not in
the DWG, and neither is its size, so an unclipped underlay is only a point with the file's path. 

AutoCAD Architecture objects other than straight walls, doors, windows and openings (curved walls,
slabs, roofs, stairs, spaces, curtain walls) are drawn from their picture only; their sizes are not
read. The same holds for a wall with body modifiers or a custom roof or floor line, and for any object
carrying a classification: those are reported as `<TYPE> sizes (layout not read; drawn from its
picture)`. Of a style, the display settings are not read (how it is drawn in plan, hatching), nor a
component's top and bottom elevations, nor the materials of a door's or window's parts; a property set
whose definition has formula, location or classification properties is reported as unreadable, not
half-read. All of this
was checked against one drawing, saved by AutoCAD Architecture 2007; other versions of these objects
take the same route only if their layout matches, and fall back to the picture if not.

Every entity left out, for any of these
reasons, is counted by reason in the `X-DWG-Skipped` response header. The properties listed above as not
carried over are dropped without a count.

## Run with Docker

```sh
docker compose up
```

That builds the image on first use (this step needs the network; running does not) and serves on
http://127.0.0.1:8000, reachable from your machine only. API docs are at http://127.0.0.1:8000/docs.

Put drawings in `./drawings` to refer to them with `?path=`; the folder is mounted read-only. Sending
the drawing in the request body needs no setup. Stop with `Ctrl+C`, or run `docker compose up -d` and
`docker compose down`.

## Install without Docker

Needs Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
```

## Run

```sh
uv run dwg2ifc
```

The service listens on http://127.0.0.1:8000. Interactive API docs are at http://127.0.0.1:8000/docs
and work offline.

| Environment variable | Default | Meaning |
|----------------------|---------|---------|
| `DWG2IFC_HOST` | `127.0.0.1` | Address to listen on. The Docker image sets `0.0.0.0` |
| `DWG2IFC_PORT` | `8000` | Port to listen on |
| `DWG2IFC_ROOT` | current directory | Directory that `?path=` references are read from |
| `DWG2IFC_MAX_MB` | `100` | Largest drawing accepted, in megabytes |
| `DWG2IFC_WORKERS` | one per processor | How many drawings are converted at the same time |

Requests are handled in parallel. Each conversion runs in its own worker process, so several drawings
are converted side by side on separate processors; requests beyond `DWG2IFC_WORKERS` wait their turn.
On a 10-core machine, eight copies of a 560 KB plan sent at once took 2.9 s in total, against 21.6 s
sent one after another. Each worker holds a drawing and its model in memory, so lower the number on a
machine with little memory.

## Use

There is one endpoint, `POST /convert`. Give it the drawing in either of two ways.

Send the DWG bytes as the request body:

```sh
curl --data-binary @drawing.dwg -o drawing.ifc http://127.0.0.1:8000/convert
```

Or name a file under `DWG2IFC_ROOT`:

```sh
DWG2IFC_ROOT=/path/to/drawings uv run dwg2ifc
curl -X POST -o drawing.ifc "http://127.0.0.1:8000/convert?path=drawing.dwg"
```

The response body is the IFC file (STEP text, `application/x-step`). Two headers describe the result:

| Header | Example | Meaning |
|--------|---------|---------|
| `X-DWG-Version` | `R2013` | DWG release of the input |
| `X-DWG-Units` | `inches (declared by the drawing as INSUNITS)` | The length unit used, and where it came from |
| `X-DWG-Skipped` | `LIGHT=8` | Entities left out, counted by type or reason. Empty when none were |

Add `-D -` to `curl` to see them.

| Status | Meaning |
|--------|---------|
| 200 | Converted |
| 403 | `path` points outside `DWG2IFC_ROOT` |
| 404 | `path` does not exist |
| 413 | The drawing is larger than `DWG2IFC_MAX_MB` |
| 422 | Not a DWG file, an unsupported release, or a corrupt file |
| 500 | The worker process died on this drawing; other requests carry on |

From Python, without the web service:

```python
from pathlib import Path
from dwg2ifc import convert

ifc_text, skipped = convert(Path("drawing.dwg").read_bytes())
# heights for elements read out of flat linework are yours to give, in metres; none are assumed
ifc_text, skipped = convert(Path("plan.dwg").read_bytes(), heights={"storey": 3.0, "door": 2.1})
Path("drawing.ifc").write_text(ifc_text)
```

## Limits

- There is a size limit but no time limit on a conversion; run it on a trusted machine. A drawing that
  takes very long occupies one worker until it finishes.
- Password-protected drawings are rejected with a 422; they are out of scope.
- DWG R13 and older are out of scope. An R13 file is handed to the R14 reader, which has never been
  tried on one.
- Objects that only drive AutoCAD's own display and editing are not read, as nothing in an IFC depends on
  them: viewports, views, visual styles, plot settings, sort order, the styles of tables, multileaders and
  multilines, and the links that keep dimensions and constraints attached to geometry.
- Solids in drawings saved as R2000 or R14 lose their rounded (filleted) faces; the rest of the solid converts.
- A drawing with no AutoCAD Architecture setup is taken at its declared unit. If that is wrong, pass
  `units` in the request.
- IFC4X3 has no cone surface, so cone faces are written as surfaces of revolution. Viewer support for that varies.
- Some curved faces of solids do not come through IfcOpenShell (and so Bonsai) as AutoCAD has them. Measured
  against AutoCAD on the two 3D test drawings, 70 of 82 solids match its surface area within 0.2 percent.
  The rest have a face of one of these kinds:
  - the rounded end of a pin (a torus that crosses its own axis). IFC has no torus of that kind, so it is
    written as an exact rational spline surface. IfcOpenShell ignores the weights of such a surface and
    draws it bulged: 1 to 2 percent too much area on those solids;
  - a flat sliver half a thousandth of a unit wide between an arc and a spline. The IFC has it right;
    IfcOpenShell's check for outlines that cross themselves misfires on it and returns the arc's whole
    disc. With that check switched off (its setting `no-wire-intersection-check`) the solid matches AutoCAD;
  - a long narrow strip that the kernel builds but its mesher leaves empty at fine settings.
  Three shapes are written differently from how AutoCAD stores them, without moving any point, because
  IfcOpenShell cannot build the stored form: a patch of a sphere is a revolved arc (it does not read
  `IfcSphericalSurface`); a tube swept along a path (a cable, a handrail) is two half-tubes joined
  along its length where AutoCAD has one face closed on itself; and the rounded rim of a disc is two
  half rims, because given only its two circles the kernel builds the rest of the torus instead.
- Text is positioned, aligned, sized and given its font. Its width factor is a property only, so a viewer
  draws it at normal width.
- Arc-length and jogged-radius dimensions are placed by the block AutoCAD drew for them. Saved without
  that block they are not redrawn, unlike the other dimension kinds.

## Development

```sh
uv run pytest -q
```

Tests compare decoded values with AutoCAD's own DXF export of the same drawing in every supported
release, and validate the IFC output against the schema.

`tools/accuracy.py drawing.dwg` measures a conversion against AutoCAD's own figures: it has AutoCAD
print every object's length, area or bounding box and each solid's volume and surface area, and
compares them with what was read from the drawing and with the IFC as a geometry kernel builds it.
On the test drawing, lengths and areas agree to the eight decimals AutoCAD prints, and solids to
within 0.2 percent in volume (the kernel's tessellation of curved faces). `tests/test_accuracy.py`
holds that line from a saved copy of AutoCAD's answers.

`tools/make_fixtures.sh` rebuilds the test drawings `tests/fixtures/entities_*` from the script
`tools/entities.scr`. It is the only part that needs AutoCAD (2027, on macOS) and is not needed to run
the service or the tests. `tests/fixtures/extras_*` holds the few entities AutoCAD's console cannot
create (they need a mouse pick); it was drawn by hand and is not rebuilt by the script.
