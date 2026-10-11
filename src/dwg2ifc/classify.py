"""Guesses what building element a block or layer stands for, from its name.

DWG has no notion of a wall or a door: drawings say it through layer and block names, by office or
national convention (US National CAD Standard "A-WALL", plain "Doors", Spanish "PRTA0001",
Icelandic "GLUGGAR"). The rules below read those names. They are a best guess, kept in one table
so that a convention we have not met can be added in one line.
"""

import re

# (IFC class, name fragments). A fragment matches a word of the name that starts with it; a trailing
# "$" makes it match the whole word only. First rule to match wins, so specific ones come first.
_RULES = (
    ("IfcDoor", "door doors$ dr$ puerta prta porte tuer tur$ hurd dor$"),
    ("IfcWindow", "window glaz ventana vent$ fenster fenetre glugg vindu"),
    ("IfcCurtainWall", "curtainwall cwall cw$ fachada"),
    ("IfcStair", "stair strs escalera treppe escalier stigi trappa"),
    ("IfcRailing", "handrail railing rail$ baranda gelander handrid"),
    ("IfcColumn", "column cols$ col$ colu columna pilar stutze stuetze poteau sula"),
    ("IfcBeam", "beam beams$ viga trager traeger poutre biti bjalki"),
    ("IfcRoof", "roof techo dach toit thak"),
    ("IfcCovering", "ceiling clng plafon cielo"),
    ("IfcSlab", "slab flor$ floor floors$ piso losa dalle golf platte"),
    ("IfcWall", "wall walls$ muro mur$ pared wand mauer vegg"),
    ("IfcSanitaryTerminal", "plumb sanit wc$ toilet lavat sink bath basin urinal inodoro wb$"),
    ("IfcLightFixture", "light lite$ lamp lumin ljos fixture"),
    ("IfcOutlet", "outlet socket recept tengill"),
    ("IfcFurniture", "furn mueb mobel moebel meuble chair table desk sofa bed$ dining husgogn"),
    ("IfcGeographicElement", "tree shrub plant veget arbol baum"),
)
_COMPILED = [
    (ifc_class, [(f[:-1], True) if f.endswith("$") else (f, False) for f in fragments.split()])
    for ifc_class, fragments in _RULES
]
_PLAIN = str.maketrans("áàâäãéèêëíìîïóòôöõúùûüýðþæøåñç", "aaaaaeeeeiiiiooooouuuuydtaoanc")


# drawings of a building seen from the side: their "wall" lines are not walls in plan
_NOT_PLAN = re.compile(r"section|elev|detail|schnitt|ansicht|corte|alzado|coupe|snid|utlit")


def is_plan(name: str) -> bool:
    return not _NOT_PLAN.search((name or "").lower())


def classify(*names: str) -> str | None:
    """The IFC element class the first recognisable name stands for, or None."""
    for name in names:
        words = re.findall(r"[a-z]+", (name or "").lower().translate(_PLAIN))
        for ifc_class, fragments in _COMPILED:
            for fragment, whole in fragments:
                if any(word == fragment if whole else word.startswith(fragment) for word in words):
                    return ifc_class
    return None
