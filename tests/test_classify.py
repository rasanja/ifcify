from dwg2ifc.classify import classify


def test_names_from_several_conventions():
    cases = {
        "A-WALL": "IfcWall", "A-DOOR": "IfcDoor", "A-GLAZ": "IfcWindow", "A-FLOR": "IfcSlab", "S-COLS": "IfcColumn",
        "Doors": "IfcDoor", "Window - Imperial": "IfcWindow", "PRTA0001": "IfcDoor", "COLU_C16": "IfcColumn",
        "GLUGGAR": "IfcWindow", "MUEB001H": "IfcFurniture", "WC": "IfcSanitaryTerminal", "TREE3": "IfcGeographicElement",
        "Fluorescent Fixture": "IfcLightFixture",
        # no building element in these
        "0": None, "A-ANNO-DIMS": None, "SECTION-A": None, "OUTDOOR-AREA": None, "": None,
    }  # fmt: skip
    assert {name: classify(name) for name in cases} == cases


def test_first_recognised_name_wins():
    assert classify("ProjectRev", "A-DOOR") == "IfcDoor"
    assert classify("Door - Imperial", "A-WALL") == "IfcDoor"
