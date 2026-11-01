"""Reading a MOD back: the one parser the audits, the lint and the comparisons share.

    0     title (20)
    20    31 sample headers x 30: name (22), length (words), finetune, volume, loop start, loop length
    950   song length, 951 restart byte, 952 order (128 pattern numbers)
    1080  format tag ("M.K.", "8CHN", "10CH" ...)
    1084  patterns (64 rows x channels x 4 bytes, every pattern the order holds), then the sample data
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .cell import CELL_BYTES, ROWS_PER_PATTERN, Cell

# File format information: https://www.exotica.org.uk/wiki/Protracker
BYTE_ORDER: Literal["little", "big"] = "big"

_SAMPLE_SLOTS = 31
# The title and sample name fields: NUL padded, no terminator when full
TITLE_BYTES, SAMPLE_NAME_BYTES = 20, 22
_SAMPLE_HEADERS, _SAMPLE_HEADER_BYTES = TITLE_BYTES, 30
_SONG_LENGTH_AT, _ORDER_AT, _ORDER_SLOTS = 950, 952, 128
_TAG_AT, _PATTERNS_AT = 1080, 1084
_LOOP_WORDS_NONE = 1           # a loop length of one word: no loop

# Tags with a fixed channel count; any other "nCHN" / "nnCH" names its own
_FIXED_TAGS = {"M.K.": 4, "M!K!": 4, "FLT4": 4, "FLT8": 8, "OCTA": 8}

# Commands that steer the song, kept on every channel by isolate_channel: Fxx speed / tempo,
# Bxx position jump, Dxx pattern break
_FLOW_EFFECTS = (0xF, 0xB, 0xD)


def format_channels(tag: str) -> int | None:
    """Channels a format tag stands for: "M.K." 4, "6CHN" 6, "14CH" 14; None for an unknown tag."""
    if tag in _FIXED_TAGS:
        return _FIXED_TAGS[tag]
    if tag[1:] == "CHN" and tag[0].isdigit():
        return int(tag[0])
    if tag[2:] == "CH" and tag[:2].isdigit():
        return int(tag[:2])
    return None


def sample_name(field: bytes | bytearray) -> str:
    """A sample's name as its 22-byte header field holds it: up to the first NUL."""
    return bytes(field[:SAMPLE_NAME_BYTES]).split(b"\0")[0].decode("latin1")


@dataclass(frozen=True)
class SampleInfo:
    """One sample slot as the file holds it; lengths in bytes."""
    name: str
    length: int
    finetune: int          # -8..+7
    volume: int
    loop_start: int
    loop_len: int
    data: bytes

    @property
    def looped(self) -> bool:
        return self.loop_len > 2 * _LOOP_WORDS_NONE


@dataclass(frozen=True)
class ModImage:
    """A MOD file read back: header, samples, order, every stored pattern's cells."""
    tag: str
    channels: int
    samples: list[SampleInfo]
    order: list[int]                        # the song's positions (song length long)
    patterns: list[list[list[Cell]]]        # patterns[p][row][channel]

    def play_rows(self) -> Iterator[tuple[int, int, list[Cell]]]:
        """(pattern, row, cells) of each row one pass plays: Bxx and Dxx followed (read left to
        right, as ProTracker does: a Bxx after a Dxx resets the row to 0), stopping where the
        song loops back to a row already played."""
        rows, _loop = self.play_order()
        for pos, row in rows:
            yield self.order[pos], row, self.patterns[self.order[pos]][row]

    def play_order(self) -> tuple[list[tuple[int, int]], int | None]:
        """(the (position, row) of each row one pass plays, as play_rows; the index in that list the
        song loops back to, or None for a song that runs off its last position: one that stops)."""
        rows: list[tuple[int, int]] = []
        seen: dict[tuple[int, int], int] = {}
        pos, row = 0, 0
        while pos < len(self.order) and (pos, row) not in seen:
            seen[(pos, row)] = len(rows)
            rows.append((pos, row))
            cells = self.patterns[self.order[pos]][row]
            jump = None
            for _p, _i, eff, par in cells:
                if eff == 0xB:
                    jump = (par, 0)
                elif eff == 0xD:
                    jump = (jump[0] if jump is not None else pos + 1, (par >> 4) * 10 + (par & 0xF))
            if jump is not None:
                pos, row = jump
            elif row == ROWS_PER_PATTERN - 1:
                pos, row = pos + 1, 0
            else:
                row += 1
        return rows, seen.get((pos, row))


def read_mod(source: bytes | str | os.PathLike) -> ModImage:
    """Parse a MOD (its bytes or its path).  Raises ValueError on a tag no reader knows."""
    d = source if isinstance(source, bytes) else Path(source).read_bytes()
    tag = d[_TAG_AT:_PATTERNS_AT].decode("latin1")
    channels = format_channels(tag)
    if channels is None:
        raise ValueError(f"unknown MOD format tag {tag!r}")

    # The sample headers: lengths in words, finetune a signed nibble
    headers = []
    for i in range(_SAMPLE_SLOTS):
        o = _SAMPLE_HEADERS + i * _SAMPLE_HEADER_BYTES
        h = d[o:o + _SAMPLE_HEADER_BYTES]
        headers.append((sample_name(h),
                        int.from_bytes(h[22:24], BYTE_ORDER) * 2, ((h[24] & 0x0F) ^ 8) - 8, h[25],
                        int.from_bytes(h[26:28], BYTE_ORDER) * 2, int.from_bytes(h[28:30], BYTE_ORDER) * 2))

    # The stored patterns: up to the highest any of the 128 order slots names
    order = list(d[_ORDER_AT:_ORDER_AT + d[_SONG_LENGTH_AT]])
    stored = max(d[_ORDER_AT:_ORDER_AT + _ORDER_SLOTS]) + 1
    off = _PATTERNS_AT
    patterns = []
    for _ in range(stored):
        rows = []
        for _r in range(ROWS_PER_PATTERN):
            rows.append([Cell.decode(d, off + c * CELL_BYTES) for c in range(channels)])
            off += channels * CELL_BYTES
        patterns.append(rows)

    # The sample data, in slot order after the patterns
    samples = []
    for name, length, finetune, volume, loop_start, loop_len in headers:
        samples.append(SampleInfo(name, length, finetune, volume, loop_start, loop_len, d[off:off + length]))
        off += length
    return ModImage(tag, channels, samples, order, patterns)


def isolate_channel(data: bytes, keep: int | None) -> bytes:
    """A copy of the MOD with every channel but `keep` stripped of its notes (None: unchanged).
    The flow commands (Fxx speed / tempo, Bxx jump, Dxx break) stay on every channel, so the
    timing and the song's structure are the same."""
    if keep is None:
        return data
    channels = format_channels(data[_TAG_AT:_PATTERNS_AT].decode("latin1")) or 4
    order = data[_ORDER_AT:_ORDER_AT + data[_SONG_LENGTH_AT]]
    b = bytearray(data)
    for p in range((max(order) + 1) if order else 0):
        for r in range(ROWS_PER_PATTERN):
            for c in range(channels):
                if c == keep:
                    continue
                off = _PATTERNS_AT + ((p * ROWS_PER_PATTERN + r) * channels + c) * CELL_BYTES
                cell = Cell.decode(b, off)
                flow = Cell(effect=cell.effect, param=cell.param) if cell.effect in _FLOW_EFFECTS else Cell()
                b[off:off + CELL_BYTES] = flow.encode()
    return bytes(b)
