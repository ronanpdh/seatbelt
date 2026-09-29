"""Make untrusted text safe to print to a terminal.

Ledgers and packs hold strings that users, models, tools and pack authors chose. Rich's
`Text` and `escape` stop Rich markup, but pass ESC, CSI and OSC sequences straight to the
terminal, where they can move the cursor, erase or hide lines, or write the clipboard. So
every string that comes from a ledger or a pack goes through `printable` before it is shown.
"""

import re

# C0 controls except tab and newline, DEL, the C1 controls (0x9b is a one-byte CSI, 0x9d
# a one-byte OSC), and the bidirectional overrides that can reorder what a reader sees
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def _visible(m: re.Match[str]) -> str:
    c = ord(m.group())
    return f"\\x{c:02x}" if c < 0x100 else f"\\u{c:04x}"


def printable(s: str) -> str:
    """`s` with every control character shown as a visible escape, e.g. ESC as `\\x1b`."""
    return _UNSAFE.sub(_visible, s)
