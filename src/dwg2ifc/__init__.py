from dwg2ifc import entities, ifc


def convert(data: bytes, name: str = "drawing", heights: dict | None = None, units: str | None = None) -> tuple[str, dict[str, int]]:
    """DWG bytes in; IFC4X3 STEP text out, plus counts of entities that could not be converted, by type.

    `heights`, in metres, are for walls, rooms, columns and openings read out of flat linework, which stores
    none: {"storey": 3.0, "door": 2.1, "window": 1.2, "sill": 0.9}. Any left out are not guessed: the element
    is then written as a flat outline, or the opening is not cut.

    `units` ("mm", "cm", "dm", "m", "km", "in", "ft", "yd", "mi") overrides the drawing's own unit, for a drawing
    that declares none or the wrong one. Which unit was used, and why, is written to the IFC as `DWG_Units`."""
    try:
        drawing = entities.load(data)
    except (IndexError, KeyError) as e:  # truncated or inconsistent file
        raise ValueError(f"corrupt DWG file: {e!r}") from None
    model, skipped = ifc.build(drawing, name, (heights or {}) | ({"units": units} if units else {}))
    return model.to_string(), skipped
