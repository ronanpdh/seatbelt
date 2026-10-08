"""How `seatbelt run` shows itself on the terminal, so that it is not taken for the CLI it
launches: every line it prints starts with its badge, and a seatbelt buckles as the run
starts and unbuckles when the CLI exits.

Stdlib only: the launcher imports it, and Claude Code's status line runs `BADGE`."""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from typing import TextIO

BADGE = "\x1b[7m seatbelt \x1b[0m"  # reversed, on a terminal
PLAIN = "[seatbelt]"  # anywhere else: a pipe, a log file, NO_COLOR, a dumb terminal
NO_ANIMATION_ENV = "SEATBELT_NO_ANIMATION"

# the seatbelt: a tongue that travels into the buckle, on a strap
_STRAP, _TONGUE, _BUCKLE = "━", "▶", "▐█▌"
_TRAVEL = 16  # cells the tongue travels
_STEP = 0.05  # seconds a frame
_HOLD = 0.5  # seconds the buckled or unbuckled belt stays before the CLI starts or seatbelt goes on
_WIDTH = len(PLAIN) + 1 + 4 + _TRAVEL + 1 + len(_BUCKLE) + 4 + len("  unbuckled")


def styled(stream: TextIO, env: Mapping[str, str] = os.environ) -> bool:
    """Whether `stream` is a terminal that shows styles: a TTY, `NO_COLOR` unset
    (https://no-color.org), not a dumb terminal, and on Windows a terminal known to read
    escape sequences."""
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):  # closed, or not a real stream
        return False
    if not tty or env.get("NO_COLOR") or env.get("TERM") == "dumb":
        return False
    return sys.platform != "win32" or "WT_SESSION" in env or "TERM" in env


def badge(stream: TextIO, env: Mapping[str, str] = os.environ) -> str:
    return BADGE if styled(stream, env) else PLAIN


def say(message: str, stream: TextIO | None = None, env: Mapping[str, str] = os.environ) -> None:
    """Print `message` to `stream` (stderr by default), each line after seatbelt's badge."""
    out = stream if stream is not None else sys.stderr
    mark = badge(out, env)
    for line in message.splitlines() or [""]:
        print(f"{mark} {line}", file=out, flush=True)


class SayHandler(logging.Handler):
    """Log records said with the badge: what seatbelt logs while a run is on the screen."""

    def __init__(self, stream: TextIO | None = None) -> None:
        super().__init__(logging.WARNING)
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            say(f"{record.levelname.lower()}: {record.getMessage()}", self._stream)
        except Exception:
            self.handleError(record)


@contextmanager
def said_logs(*names: str) -> Generator[None]:
    """While the block runs, the loggers `names` print through `say` and nowhere else."""
    handler = SayHandler()
    loggers = [logging.getLogger(n) for n in names]
    before = [(lg.handlers[:], lg.propagate) for lg in loggers]
    for lg in loggers:
        lg.handlers = [handler]
        lg.propagate = False
    try:
        yield
    finally:
        for lg, (handlers, propagate) in zip(loggers, before, strict=True):
            lg.handlers = handlers
            lg.propagate = propagate


def _columns(stream: TextIO, env: Mapping[str, str]) -> int:
    """The terminal's width: `COLUMNS` if set, as `shutil.get_terminal_size` reads it, else
    the terminal's own; 0 when it has none."""
    if env.get("COLUMNS", "").isdigit():
        return int(env["COLUMNS"])
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, OSError, ValueError):
        return 0


def animates(stream: TextIO, env: Mapping[str, str] = os.environ) -> bool:
    """Whether the seatbelt is drawn: on a styled terminal wide enough for it, that can show
    its characters, unless `SEATBELT_NO_ANIMATION` is set."""
    if env.get(NO_ANIMATION_ENV) or not styled(stream, env) or _columns(stream, env) < _WIDTH:
        return False
    try:
        (_STRAP + _TONGUE + _BUCKLE).encode(getattr(stream, "encoding", None) or "ascii")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def frame(gap: int, note: str = "") -> str:
    """The belt with `gap` cells between the tongue and the buckle (0: buckled)."""
    strap = _STRAP * (4 + _TRAVEL - gap)
    return f"{strap}{_TONGUE}{' ' * gap}{_BUCKLE}{_STRAP * 4}{note}"


def _draw(
    stream: TextIO, gaps: Iterator[int] | range, note: str, sleep: Callable[[float], None]
) -> None:
    mark = BADGE
    stream.write("\x1b[?25l")  # cursor hidden while it moves
    try:
        last = 0
        for gap in gaps:
            last = gap
            stream.write(f"\r\x1b[2K{mark} {frame(gap)}")
            stream.flush()
            sleep(_STEP)
        stream.write(f"\r\x1b[2K{mark} {frame(last, note)}")
        stream.flush()
        sleep(_HOLD)
    finally:
        stream.write("\n\x1b[?25h")
        stream.flush()


def buckle(
    stream: TextIO | None = None,
    env: Mapping[str, str] = os.environ,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Buckle up as a run starts, about 1.4 seconds. Returns whether it was drawn."""
    out = stream if stream is not None else sys.stderr
    if not animates(out, env):
        return False
    _draw(out, range(_TRAVEL, -1, -1), "  click", sleep)
    return True


def unbuckle(
    stream: TextIO | None = None,
    env: Mapping[str, str] = os.environ,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Unbuckle when the CLI exits, about 0.8 seconds, faster than buckling up."""
    out = stream if stream is not None else sys.stderr
    if not animates(out, env):
        return False
    _draw(out, range(0, _TRAVEL + 1, 4), "  unbuckled", sleep)
    return True
