from io import StringIO

from rich.console import Console
from rich.text import Text

from seatbelt.terminal import printable


def test_escape_sequences_become_visible_text():
    s = "ok\x1b[2K\x1b[1A\x1b]52;c;ZXZpbA==\x1b\\\x9b31m\x07done"
    assert printable(s) == "ok\\x1b[2K\\x1b[1A\\x1b]52;c;ZXZpbA==\\x1b\\\\x9b31m\\x07done"


def test_tab_newline_and_ordinary_unicode_are_kept():
    s = "line one\n\tindented: café, 東京, emoji 🚀"
    assert printable(s) == s


def test_bidi_overrides_are_shown():
    assert printable("run‮gnp.exe") == "run\\u202egnp.exe"


def test_nothing_raw_reaches_the_terminal():
    out = StringIO()
    Console(file=out, force_terminal=True, color_system=None).print(
        Text(printable("a\x1b[8mhidden\x1b[0m"))
    )
    assert "\x1b" not in out.getvalue()
    assert "hidden" in out.getvalue()
