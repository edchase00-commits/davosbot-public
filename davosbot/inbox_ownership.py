"""Prevent in-process consumer replacement while handler threads still live."""

from contextlib import contextmanager
from pathlib import Path
from threading import RLock

_LOCK = RLock()
_OWNERS = {}


def _key(path):
    return str(Path(path).expanduser().resolve()).casefold()


@contextmanager
def initializing_inbox(path):
    with _LOCK:
        if _key(path) in _OWNERS:
            raise RuntimeError("inbox_workers_still_running")
        yield


def acquire_inbox(path, owner):
    with _LOCK:
        key = _key(path)
        if key in _OWNERS:
            raise RuntimeError("inbox_workers_still_running")
        _OWNERS[key] = owner


def release_inbox(path, owner):
    with _LOCK:
        key = _key(path)
        if _OWNERS.get(key) is owner:
            del _OWNERS[key]
