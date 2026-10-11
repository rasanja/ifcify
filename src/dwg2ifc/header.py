"""Reads drawing units (INSUNITS) from the header variables section (spec chapter 9).

The header is one long run of bit-coded fields with no index, so every field before INSUNITS has
to be stepped over. _FIELDS lists them in file order as "<releases> <field types>".
"""

import struct

from dwg2ifc.dwg import Bits, since

# releases: * all, >=X from X on, <=X up to X.  types: T text, H handle, HS handle kept in the data
# stream in every release (HANDSEED), CMC colour, the rest are bit codes.
_FIELDS = """
*       BD BD BD BD T T T T BL BL
<=R14   BS
<=R2000 H
*       B B
<=R14   B
*       B B B B B B B
<=R14   B
>=R2004 B
*       B B B B
<=R14   B B
*       B B
<=R14   B
*       B B B
<=R14   B
*       B B BS
<=R14   BS
*       BS BS BS BS BS
<=R14   BS
*       BS
<=R14   BS
*       BS
<=R14   BS
>=R2004 BL BL BL
*       BS BS BS BS BS BS BS BS BS BS BS BS BS BS BS BS BS BS BS
*       BD BD BD BD BD BD BD BD BD BD BD BD BD BD BD BD BD BD BD BD BD
<=R2004 T
*       BL BL BL BL
>=R2004 BL BL BL
*       BL BL BL BL CMC HS H H H
>=R2007 H
*       H H
>=R2000 BD
*       3BD 3BD 3BD 2RD 2RD BD 3BD 3BD 3BD H
>=R2000 H BS H 3BD 3BD 3BD 3BD 3BD 3BD
*       3BD 3BD 3BD 2RD 2RD BD 3BD 3BD 3BD H
>=R2000 H BS H 3BD 3BD 3BD 3BD 3BD 3BD T T
<=R14   B B B B B B B B B B B RC RC B B RC RC RC B RC RC RC RC BS BS BS BS BS BS H
*       BD BD BD BD BD BD BD BD BD
>=R2007 BD BD BS CMC
>=R2000 B B B B B B BS BS BS
>=R2007 BS
*       BD BD BD BD BD BD BD BD
<=R14   T T T T T
>=R2000 BD B BS B B B B
*       CMC CMC CMC
>=R2000 BS BS BS BS BS BS BS BS BS BS BS B B BS BS BS BS B BS
>=R2007 B
>=R2010 B BD T BD T
>=R2000 H H H H H
>=R2007 H H H
>=R2000 BS BS
*       H H H H H H H H H
<=R2000 H
*       H H H
>=R2000 BS BS T T H H H
>=R2004 H H
>=R2007 H
>=R2013 H
>=R2000 BL BS
"""


def insunits(section: bytes, release: str, maintenance: int) -> int:
    """DWG INSUNITS code (1 inch, 4 mm, 6 m, ...), or 0 when unknown or the release predates it."""
    if not since(release, "R2000"):
        return 0
    try:
        return _insunits(section, release, maintenance)
    except (ValueError, IndexError, struct.error):
        return 0


def _insunits(section: bytes, release: str, maintenance: int) -> int:
    start = 16 + 4 + (4 if release == "R2018" or (since(release, "R2010") and maintenance > 3) else 0)
    r = Bits(section, start * 8)
    split = since(release, "R2007")  # text and handles live in their own streams, not in the data
    if split:
        r.rl()  # size in bits
    if since(release, "R2013"):
        r.bll()  # REQUIREDVERSIONS
    value = 0
    for line in _FIELDS.strip().splitlines():
        releases, *types = line.split()
        if releases.startswith(">=") and not since(release, releases[2:]):
            continue
        if releases.startswith("<=") and since(release, releases[2:]) and release != releases[2:]:
            continue
        for kind in types:
            if kind == "T":
                if not split:
                    r.t()
            elif kind == "H":
                if not split:
                    r.h()
            elif kind == "HS":
                r.h()
            elif kind == "CMC":
                r.bs()
                if since(release, "R2004"):
                    r.bl()
                    names = r.rc()
                    if not split:
                        for bit in (1, 2):
                            if names & bit:
                                r.t()
            elif kind == "3BD":
                r.bd3()
            elif kind == "2RD":
                r.rd2()
            else:
                value = getattr(r, kind.lower())()
    return value
