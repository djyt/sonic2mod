"""A run of code to find in a ROM or a Z80 program, written as a listing: one instruction a line, its
bytes, then its mnemonic.  Compiled to a bytes regex: each capture a group, in order.

    3A <w>      ld a,(hi)          <w> a word captured (2 bytes), <b> a byte captured
    CD ? ?      call ...           ? any byte, ?n any n bytes
    32 00 60    ld ($6000),a       anything after the bytes: the mnemonic, a comment

The bytes end at the first word that is none of these (no mnemonic is two hex digits).
"""

from __future__ import annotations

import re

_TOKENS = {"<b>": b"(.)", "<w>": b"(..)", "?": b"."}
_ANY_RUN = re.compile(r"\?(\d+)")
_HEX_BYTE = re.compile(r"[0-9A-Fa-f]{2}")


def code_pattern(listing: str) -> re.Pattern[bytes]:
    """The regex the listing's bytes spell, line by line."""
    out = b""
    for line in listing.splitlines():
        for token in line.split():
            if token in _TOKENS:
                out += _TOKENS[token]
            elif run := _ANY_RUN.fullmatch(token):
                out += b".{%d}" % int(run.group(1))
            elif _HEX_BYTE.fullmatch(token):
                out += re.escape(bytes.fromhex(token))
            else:
                break                                 # the mnemonic
    if not out:
        raise ValueError(f"a code pattern of no bytes: {listing!r}")
    return re.compile(out, re.DOTALL)
