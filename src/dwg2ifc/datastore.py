"""AcDb:AcDsPrototype_1b, the data store where R2013+ keeps ACIS data for solids (spec chapter 24)."""

import struct

_SEGMENT_HEADER = 48
_BLOB_REFERENCE = 0xBB106BB1


def records(section: bytes) -> dict[int, bytes]:
    """Object handle -> its binary record. Empty if the section is missing or unreadable."""
    try:
        return _records(section)
    except (struct.error, IndexError, ValueError):
        return {}


def _records(section: bytes) -> dict[int, bytes]:
    if len(section) < 56:
        return {}
    index_offset, _, segment_count, _, data_index = struct.unpack_from("<5i", section, 24)
    # segment index: where each numbered segment starts
    segments = [struct.unpack_from("<QI", section, index_offset + _SEGMENT_HEADER + 12 * i)[0] for i in range(segment_count)]

    def data_start(segment: int) -> int:
        """Record data begins at the segment's object data alignment offset, stored in units of 16 bytes."""
        return segment + (struct.unpack_from("<i", section, segment + 36)[0] << 4)

    def blob(at: int) -> bytes:
        total, pages = struct.unpack_from("<QI", section, at)
        out = bytearray()
        for i in range(pages):
            page = segments[struct.unpack_from("<I", section, at + 32 + 8 * i)[0]]
            size = struct.unpack_from("<Q", section, page + _SEGMENT_HEADER + 24)[0]
            out += section[page + _SEGMENT_HEADER + 32 : page + _SEGMENT_HEADER + 32 + size]
        return bytes(out[:total])

    out = {}
    index = segments[data_index] + _SEGMENT_HEADER
    for i in range(struct.unpack_from("<i", section, index)[0]):
        segment_number, local, _schema = struct.unpack_from("<3I", section, index + 8 + 12 * i)
        if not segment_number:
            continue  # stub entry
        segment = segments[segment_number]
        # the index offset counts from the end of the segment header, not from the segment start as the spec says
        _size, _, handle, data_offset = struct.unpack_from("<IIQI", section, segment + _SEGMENT_HEADER + local)
        at = data_start(segment) + data_offset
        size = struct.unpack_from("<I", section, at)[0]
        out[handle] = blob(at + 4) if size == _BLOB_REFERENCE else section[at + 4 : at + 4 + size]
    return out
