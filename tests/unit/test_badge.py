"""`seatbelt run` on the terminal: its badge on every line it prints, and the seatbelt that
buckles as a run starts."""

import io
import logging

import pytest

from seatbelt.gateway import badge
from seatbelt.gateway.badge import BADGE, buckle, said_logs, say, styled, unbuckle

TERMINAL = {"TERM": "xterm-256color", "COLUMNS": "120"}


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


def test_the_seatbelt_buckles_and_unbuckles_on_a_terminal() -> None:
    tty, waits = Terminal(), list[float]()
    assert buckle(tty, TERMINAL, waits.append)
    drawn = tty.getvalue()
    assert drawn.startswith("\x1b[?25l") and drawn.endswith("\n\x1b[?25h")  # cursor back
    frames = [f for f in drawn.split("\r\x1b[2K") if f.startswith(BADGE)]
    assert frames[-1].endswith("▶▐█▌━━━━  click\n\x1b[?25h") and len(frames) == 18
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


def test_a_ctrl_c_while_buckling_gives_the_cursor_back() -> None:
    tty = Terminal()

    def interrupted(_: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        buckle(tty, TERMINAL, interrupted)
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
