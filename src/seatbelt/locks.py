"""OS file locks that say who may write to a ledgers folder.

`gateway serve` holds `<ledgers>/.lock` while it runs, an import holds its folder's `.lock`,
and `seatbelt erase` takes the same lock before it removes anything, so nothing writes to a
folder while ledgers are removed from it. A local run (`seatbelt run`) holds
`.running/<run>.lock` instead, since several can record into one folder at once."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Generator
from pathlib import Path
from typing import IO

LOCK = ".lock"
RUNNING = ".running"  # in the ledgers folder: one lock file per live run


class Busy(ValueError):
    """Another process holds the folder: a gateway, an import or an erase."""


def try_lock(handle: IO[bytes]) -> bool:
    """Take the OS's exclusive lock on an open file without waiting; False if it is held.
    Held until the handle is closed, and released by the OS when the process dies."""
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def hold_folder(folder: Path, holder: str) -> IO[bytes]:
    """Take `<folder>/.lock`, as `gateway serve`, an import and `erase` do, and return the open
    handle: the lock lasts until it is closed, or the process ends. Raises Busy when another
    process holds it."""
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / LOCK).open("wb")
    if not try_lock(handle):
        handle.close()
        raise Busy(
            f"{folder} is in use, so {holder} cannot start: a gateway, an import or an erase "
            "holds its lock"
        )
    return handle


@contextlib.contextmanager
def folder_lock(folder: Path, holder: str) -> Generator[None]:
    with hold_folder(folder, holder):
        yield
