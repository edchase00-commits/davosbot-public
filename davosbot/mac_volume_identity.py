"""Read one stable Darwin filesystem identifier without shelling out."""
import ctypes
from functools import lru_cache
import os
import sys
from uuid import UUID


class _Attrlist(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_uint16), ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


@lru_cache(maxsize=1)
def _getattrlist():
    fn = ctypes.CDLL(None).getattrlist
    fn.argtypes = [
        ctypes.c_char_p, ctypes.POINTER(_Attrlist), ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_ulong,
    ]
    fn.restype = ctypes.c_int
    return fn


def volume_uuid(path):
    if sys.platform != "darwin":
        return None
    try:
        # Darwin sys/attr.h: ATTR_VOL_INFO | ATTR_VOL_UUID.
        attrs = _Attrlist(bitmapcount=5, volattr=0x80040000)
        result = ctypes.create_string_buffer(20)
        if _getattrlist()(os.fsencode(path), ctypes.byref(attrs), result, len(result), 0) != 0:
            return None
        if int.from_bytes(result.raw[:4], sys.byteorder) != len(result):
            return None
        value = UUID(bytes=result.raw[4:])
        return value if value.int else None
    except (AttributeError, OSError, ValueError):
        return None
