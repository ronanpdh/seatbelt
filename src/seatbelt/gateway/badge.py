"""How `seatbelt run` shows itself on the terminal, so that it is not taken for the CLI it
launches: every line it prints starts with its badge, and a seatbelt buckles as the run
starts and unbuckles when the CLI exits, as issue #46 drew them.

Stdlib only: the launcher imports it, and Claude Code's status line runs `BADGE`."""

from __future__ import annotations

import functools
import logging
import os
import sys
import time
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from typing import TextIO

BADGE = "\x1b[1;7m seatbelt \x1b[0m"  # bold and reversed, on a terminal
PLAIN = "[seatbelt]"  # anywhere else: a pipe, a log file, NO_COLOR, a dumb terminal
NO_ANIMATION_ENV = "SEATBELT_NO_ANIMATION"
_DIM, _RESET = "\x1b[2m", "\x1b[0m"
_CLICK = "CLICK"
_UNBUCKLE_STEP = 0.06  # seconds a frame, unbuckling: faster than buckling up
_UNBUCKLED_HOLD = 0.3  # seconds the unbuckled belt stays before seatbelt goes on

# a terminal too small for the drawn seatbelt gets a line: a tongue that travels into the
# buckle, on a strap
_STRAP, _TONGUE, _BUCKLE = "━", "▶", "▐█▌"
_TRAVEL = 16  # cells the tongue travels
_STEP = 0.05  # seconds a frame
_HOLD = 0.5  # seconds the buckled or unbuckled belt stays before the CLI starts or seatbelt goes on
_WIDTH = len(PLAIN) + 1 + 4 + _TRAVEL + 1 + len(_BUCKLE) + 4 + len("  unbuckled")


@dataclass(frozen=True)
class Frame:
    seconds: float
    rows: Mapping[int, str]  # row (from 1) -> its text, where it differs from the buckled belt
    click: bool = False


@dataclass(frozen=True)
class Belt:
    """The seatbelt as issue #46 drew it (`buckle.txt`): the buckled belt, and the frames
    that buckle it."""

    buckled: Sequence[str]
    frames: Sequence[Frame]
    click_at: tuple[int, int]  # (row from 1, column from 0)

    @property
    def width(self) -> int:
        return max(len(row) for row in self.buckled)

    @property
    def height(self) -> int:
        return len(self.buckled)

    def lines(self, frame: Frame, styled: bool = True) -> list[str]:
        """The frame's rows, with CLICK dim when it clicks (plain unless `styled`)."""
        rows = [frame.rows.get(n, row) for n, row in enumerate(self.buckled, 1)]
        if frame.click:
            n, col = self.click_at
            row = rows[n - 1].ljust(col + len(_CLICK))
            word = f"{_DIM}{_CLICK}{_RESET}" if styled else _CLICK
            rows[n - 1] = row[:col] + word + row[col + len(_CLICK) :].rstrip()
        return rows

    def unbuckling(self) -> list[Frame]:
        """The buckled belt, then the frames that slide it shut, backwards and faster."""
        apart = [f for f in self.frames if f.rows and not f.click]
        steps = [Frame(_UNBUCKLE_STEP, {}), *(Frame(_UNBUCKLE_STEP, f.rows) for f in apart[::-1])]
        return [*steps[:-1], Frame(_UNBUCKLED_HOLD, steps[-1].rows)]


def parse_belt(text: str) -> Belt:
    """`buckle.txt`'s belt; its format is described at the top of the file."""
    buckled: list[str] = []
    frames: list[Frame] = []
    click_at = (0, 0)
    rows: dict[int, str] | None = None
    for line in text.splitlines():
        head, bar, rest = line.partition("|")
        if bar and head.isdigit() and rows is None:
            buckled.append(rest)
        elif bar and head.isdigit() and rows is not None:
            rows[int(head)] = rest
        elif line.startswith("click "):
            row, col = line.split()[1:]
            click_at = (int(row), int(col))
        elif line.startswith("frame "):
            words = line.split()
            rows = {}
            frames.append(Frame(int(words[1]) / 1000, rows, "click" in words[2:]))
    return Belt(buckled, frames, click_at)


@functools.cache
def belt() -> Belt:
    return parse_belt(resources.files(__package__).joinpath("buckle.txt").read_text("utf-8"))


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


def _size(stream: TextIO, env: Mapping[str, str]) -> tuple[int, int]:
    """The terminal's (columns, lines): `COLUMNS` and `LINES` if set, as
    `shutil.get_terminal_size` reads them, else the terminal's own; 0 when it has none."""
    try:
        own = os.get_terminal_size(stream.fileno())
        columns, lines = own.columns, own.lines
    except (AttributeError, OSError, ValueError):
        columns = lines = 0
    if env.get("COLUMNS", "").isdigit():
        columns = int(env["COLUMNS"])
    if env.get("LINES", "").isdigit():
        lines = int(env["LINES"])
    return columns, lines


def animates(stream: TextIO, env: Mapping[str, str] = os.environ) -> bool:
    """Whether a seatbelt is drawn: on a styled terminal, unless `SEATBELT_NO_ANIMATION` is
    set. Which one depends on the room (`_drawn_belt`)."""
    return not env.get(NO_ANIMATION_ENV) and styled(stream, env)


def _drawn_belt(stream: TextIO, env: Mapping[str, str]) -> str | None:
    """ "art" on a terminal with room for issue #46's belt, "line" on one with room for a line
    of it that can show its characters, None on one too small for either."""
    columns, lines = _size(stream, env)
    art = belt()
    if columns > art.width and lines > art.height:
        return "art"
    if columns < _WIDTH:
        return None
    try:
        (_STRAP + _TONGUE + _BUCKLE).encode(getattr(stream, "encoding", None) or "ascii")
    except (LookupError, UnicodeEncodeError):
        return None
    return "line"


def frame(gap: int, note: str = "") -> str:
    """The line of belt with `gap` cells between the tongue and the buckle (0: buckled)."""
    strap = _STRAP * (4 + _TRAVEL - gap)
    return f"{strap}{_TONGUE}{' ' * gap}{_BUCKLE}{_STRAP * 4}{note}"


@contextmanager
def _cursor_hidden(stream: TextIO) -> Generator[None]:
    stream.write("\x1b[?25l")  # hidden while the belt moves
    try:
        yield
    finally:
        stream.write("\x1b[?25h")
        stream.flush()


def _play(stream: TextIO, frames: Iterable[Frame], sleep: Callable[[float], None]) -> None:
    """Draw each frame of issue #46's belt over the last, in place. The belt stays on the
    screen after, and the cursor below it."""
    art = belt()
    with _cursor_hidden(stream):
        for i, f in enumerate(frames):
            if i:
                stream.write(f"\x1b[{art.height}A")  # back to the belt's first row
            # \r\n, not \n alone: a terminal not turning \n into \r\n keeps the column
            stream.write("".join(f"\r\x1b[2K{row}\r\n" for row in art.lines(f)))
            stream.flush()
            sleep(f.seconds)


def _draw(
    stream: TextIO, gaps: Iterator[int] | range, note: str, sleep: Callable[[float], None]
) -> None:
    """Draw the line of belt, a frame per gap, over itself."""
    with _cursor_hidden(stream):
        last = 0
        try:
            for gap in gaps:
                last = gap
                stream.write(f"\r\x1b[2K{BADGE} {frame(gap)}")
                stream.flush()
                sleep(_STEP)
            stream.write(f"\r\x1b[2K{BADGE} {frame(last, note)}")
            stream.flush()
            sleep(_HOLD)
        finally:
            stream.write("\r\n")


def buckle(
    stream: TextIO | None = None,
    env: Mapping[str, str] = os.environ,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Buckle up as a run starts, about 2 seconds. Returns whether a belt was drawn."""
    out = stream if stream is not None else sys.stderr
    drawn = _drawn_belt(out, env) if animates(out, env) else None
    if drawn == "art":
        _play(out, belt().frames, sleep)
    elif drawn == "line":
        _draw(out, range(_TRAVEL, -1, -1), "  click", sleep)
    return drawn is not None


def unbuckle(
    stream: TextIO | None = None,
    env: Mapping[str, str] = os.environ,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Unbuckle when the CLI exits, under a second. Returns whether a belt was drawn."""
    out = stream if stream is not None else sys.stderr
    drawn = _drawn_belt(out, env) if animates(out, env) else None
    if drawn == "art":
        _play(out, belt().unbuckling(), sleep)
    elif drawn == "line":
        _draw(out, range(0, _TRAVEL + 1, 4), "  unbuckled", sleep)
    return drawn is not None
