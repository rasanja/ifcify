from dwg2ifc import entities, ifc


def convert(data: bytes, name: str = "drawing") -> tuple[str, dict[str, int]]:
    """DWG bytes in; IFC4X3 STEP text out, plus counts of entities that could not be converted, by type."""
    try:
        drawing = entities.load(data)
    except (IndexError, KeyError) as e:  # truncated or inconsistent file
        raise ValueError(f"corrupt DWG file: {e!r}") from None
    model, skipped = ifc.build(drawing, name)
    return model.to_string(), skipped
