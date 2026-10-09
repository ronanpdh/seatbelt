"""`seatbelt run` on the terminal: its badge on every line it prints, and the seatbelt that
buckles as a run starts."""

import io
import logging
import re

import pytest

from seatbelt.gateway import badge
from seatbelt.gateway.badge import BADGE, belt, buckle, said_logs, say, styled, unbuckle

TERMINAL = {"TERM": "xterm-256color", "COLUMNS": "120"}  # too narrow for issue #46's belt
ROOMY = {"TERM": "xterm-256color", "COLUMNS": "130", "LINES": "40"}


class Terminal(io.StringIO):
    """What a terminal is to `seatbelt run`: a TTY that can show the belt's characters."""

    encoding = "utf-8"  # pyright: ignore[reportIncompatibleVariableOverride]

    def isatty(self) -> bool:
        return True


def test_every_line_starts_with_the_badge() -> None:
    piped = io.StringIO()
    say("recorded run r\n  replay it: seatbelt reconstruct r", piped, TERMINAL)
    assert piped.getvalue() == (
        "[seatbelt] recorded run r\n[seatbelt]   replay it: seatbelt reconstruct r\n"
    )
    tty = Terminal()
    say("recording", tty, TERMINAL)
    assert tty.getvalue() == f"{BADGE} recording\n"


@pytest.mark.parametrize(
    "env", [{"NO_COLOR": "1", "TERM": "xterm"}, {"TERM": "dumb"}], ids=["no-color", "dumb"]
)
def test_a_terminal_that_shows_no_styles_gets_the_plain_badge(env: dict[str, str]) -> None:
    tty = Terminal()
    assert not styled(tty, env)
    say("x", tty, env)
    assert tty.getvalue() == "[seatbelt] x\n"


def screen(written: str) -> list[str]:
    """What a terminal shows after `written`: enough of one for the belt's escapes (carriage
    return, erase line, cursor up, newline), with styles dropped."""
    rows: list[str] = [""]
    at = 0
    for piece in re.findall(r"\x1b\[[0-9;?]*[A-Za-z]|\r|\n|[^\x1b\r\n]+", written):
        if piece == "\n":
            at += 1
            rows += [""] * (at + 1 - len(rows))
        elif piece == "\x1b[2K":
            rows[at] = ""
        elif piece.startswith("\x1b[") and piece.endswith("A"):
            at -= int(piece[2:-1])
        elif not piece.startswith("\x1b") and piece != "\r":
            rows[at] += piece  # each row is erased before it is written
    return [r.rstrip() for r in rows]


def test_issue_46s_belt_buckles_on_a_terminal_with_room_for_it() -> None:
    tty, waits = Terminal(), list[float]()
    assert buckle(tty, ROOMY, waits.append)
    art = belt()
    # the frames and timings of the issue's recording: the tongue slides in, CLICK, a jolt
    assert waits == pytest.approx([0.1] * 5 + [0.12, 0.14, 0.17, 0.2, 0.08, 0.9])
    assert sum(waits) == pytest.approx(2.11)
    drawn = tty.getvalue()
    assert drawn.startswith("\x1b[?25l") and drawn.endswith("\x1b[?25h")  # cursor back
    assert drawn.count("\x1b[34A") == len(art.frames) - 1  # each frame over the last
    assert "\x1b[2mCLICK\x1b[0m" in drawn  # dim
    shown = screen(drawn)
    assert shown[:34] == art.lines(art.frames[-1], styled=False) and shown[34:] == [""]
    assert shown[11][71:] == "CLICK"
    assert all(len(row) <= 121 for row in shown)


def test_issue_46s_belt_unbuckles_when_the_cli_exits() -> None:
    tty, waits = Terminal(), list[float]()
    assert unbuckle(tty, ROOMY, waits.append)
    art = belt()
    assert sum(waits) < 1.0 and "CLICK" not in tty.getvalue()
    assert screen(tty.getvalue())[:34] == art.lines(art.frames[0], styled=False)  # apart


def test_the_belt_is_issue_46s_drawing() -> None:
    art = belt()
    assert (art.height, art.width, art.click_at) == (34, 121, (12, 71))
    assert [f.click for f in art.frames] == [False] * 8 + [True] * 3
    assert all(1 <= row <= art.height for f in art.frames for row in f.rows)
    buckled = art.lines(art.frames[8], styled=False)  # as it clicks: the tongue in the buckle
    assert buckled[0] == "#" * 20 and buckled[-1] == " " * 106 + "#" * 15
    assert buckled[14] == " " * 40 + "#" * 22 + "---+-+--"
    apart = art.lines(art.frames[0], styled=False)  # the tongue well short of the buckle
    assert apart[11] == " " * 32 + "-+++-----    ---"


@pytest.mark.parametrize(
    ("env", "drawn"),
    [
        (ROOMY, "art"),
        ({**ROOMY, "COLUMNS": "121"}, "line"),  # the belt is 121 columns wide
        ({**ROOMY, "LINES": "34"}, "line"),  # and 34 rows tall
        ({**ROOMY, "COLUMNS": "40"}, None),
    ],
)
def test_the_belt_drawn_fits_the_terminal(env: dict[str, str], drawn: str | None) -> None:
    assert badge._drawn_belt(Terminal(), env) == drawn  # pyright: ignore[reportPrivateUsage]


def test_a_small_terminal_gets_a_line_of_belt() -> None:
    tty, waits = Terminal(), list[float]()
    assert buckle(tty, TERMINAL, waits.append)
    drawn = tty.getvalue()
    assert drawn.startswith("\x1b[?25l") and drawn.endswith("\r\n\x1b[?25h")  # cursor back
    frames = [f for f in drawn.split("\r\x1b[2K") if f.startswith(BADGE)]
    assert frames[-1].endswith("▶▐█▌━━━━  click\r\n\x1b[?25h") and len(frames) == 18
    assert 1.0 < sum(waits) < 2.0
    tty, waits = Terminal(), list[float]()
    assert unbuckle(tty, TERMINAL, waits.append)
    assert "unbuckled" in tty.getvalue() and sum(waits) < 1.0


@pytest.mark.parametrize(
    "env",
    [
        {**TERMINAL, "SEATBELT_NO_ANIMATION": "1"},
        {**TERMINAL, "COLUMNS": "30"},  # no room for it
        {**TERMINAL, "NO_COLOR": "1"},
    ],
    ids=["turned-off", "narrow", "no-color"],
)
def test_the_seatbelt_is_not_drawn_where_it_does_not_belong(env: dict[str, str]) -> None:
    tty = Terminal()
    assert not buckle(tty, env, lambda _: None) and tty.getvalue() == ""


def test_nothing_is_drawn_off_a_terminal_or_where_its_characters_cannot_be_shown() -> None:
    assert not buckle(io.StringIO(), TERMINAL, lambda _: None)

    class Ascii(Terminal):
        encoding = "ascii"

    assert not buckle(Ascii(), TERMINAL, lambda _: None)
    assert buckle(Ascii(), ROOMY, lambda _: None)  # issue #46's belt is ASCII


@pytest.mark.parametrize("env", [ROOMY, TERMINAL], ids=["art", "line"])
def test_a_ctrl_c_while_buckling_gives_the_cursor_back(env: dict[str, str]) -> None:
    tty = Terminal()

    def interrupted(_: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        buckle(tty, env, interrupted)
    assert tty.getvalue().endswith("\x1b[?25h")


def test_seatbelts_logs_are_said_with_the_badge_while_a_run_is_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log = logging.getLogger("seatbelt.gateway.local")
    before = logging.getLogger("seatbelt").handlers[:]
    with said_logs("seatbelt"):
        log.warning("closed %d ledgers", 2)
        log.info("not shown")
    assert capsys.readouterr().err == "[seatbelt] warning: closed 2 ledgers\n"
    assert logging.getLogger("seatbelt").handlers == before


def test_the_belt_fits_the_width_it_asks_for() -> None:
    assert len(f"{badge.PLAIN} {badge.frame(badge._TRAVEL, '  unbuckled')}") <= badge._WIDTH  # pyright: ignore[reportPrivateUsage]
