import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from .tables import PERIOD_TABLE, ModNote

# File format information: https://www.exotica.org.uk/wiki/Protracker
BYTE_ORDER: Literal["little", "big"] = "big"

# Paula's clock on a PAL Amiga: a sample plays at PAL_AMIGA_CLOCK / period bytes a second
PAL_AMIGA_CLOCK = 3_546_895


def row_to_bcd(row: int) -> int:
    """BCD-encode a row number for Dxx (pattern break): 32 -> 0x32, 10 -> 0x10."""
    return ((row // 10) << 4) | (row % 10)


# The word ProTracker replays after a one-shot sample ends: its first two bytes
_IDLE_WORD_BYTES = 2


class ModSample:
    _name: str
    length: int
    _finetune: int
    _volume: int
    repeat: int
    repeat_length: int
    data: bytes

    def __init__(self, name):
        self._name = name[0:21]
        self.length = 0
        self._finetune = 0
        self._volume = 0
        self.repeat = 0
        self.repeat_length = 1
        self.data = bytes(0)

    @property
    def name(self) -> str:
        return self._name

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def finetune(self) -> int:
        """-8..+7, as set_finetune takes it (the file stores the low nibble: -1 is $F)."""
        return self._finetune - 16 if self._finetune > 7 else self._finetune

    def set_finetune(self, v: int):
        if v < -8 or v > 7:
            print(f"Warning: Fine Tune {v} is invalid.")
            return
        self._finetune = v & 0xf

    def set_volume(self, v):
        if v < 0 or v > 64:
            print(f"Warning: Volume {v} is invalid.")
            return
        self._volume = int(v)

    def zero_idle_word(self) -> None:
        """A one-shot's first word silenced.  ProTracker replays it once the sample ends, so
        (-126, 126) there buzzes until the next note.  A looped sample replays its loop instead."""
        if self.repeat_length > 1 or len(self.data) < _IDLE_WORD_BYTES:
            return
        self.data = bytes(_IDLE_WORD_BYTES) + self.data[_IDLE_WORD_BYTES:]

    def set_name(self, name: str) -> None: self._name = name[:21]
    def get_name(self): return self._name.ljust(21, ' ')
    def get_name_bytes(self): return bytearray(self.get_name(), 'utf-8') + b'\x00'
    def get_bytes(self):
        output = bytearray()
        output += self.get_name_bytes()
        output += self.length.to_bytes(2, byteorder=BYTE_ORDER, signed=False)
        output += self._finetune.to_bytes(1, byteorder=BYTE_ORDER, signed=True)
        output += self._volume.to_bytes(1, byteorder=BYTE_ORDER, signed=False)
        output += self.repeat.to_bytes(2, byteorder=BYTE_ORDER, signed=False)
        output += self.repeat_length.to_bytes(2, byteorder=BYTE_ORDER, signed=False)
        return output


# Bytes per pattern cell (4) × rows per pattern (64)
_BYTES_PER_CELL = 4
_ROWS_PER_PATTERN = 64


class ModPattern:
    _data: bytearray
    _plen: int

    def __init__(self, chan: int = 4):
        size = chan * _BYTES_PER_CELL * _ROWS_PER_PATTERN
        self._data = bytearray(size)
        self._plen = size

    def get_bytes(self): return self._data

    def set_entry(self, index: int, value: int):
        if index < 0 or index > self._plen - 1:
            print(f"Invalid index {index}")
            return
        self._data[index] = value

    def get_entry(self, index: int):
        if index < 0 or index > self._plen - 1:
            print(f"Invalid index {index}")
            return 0
        return self._data[index]


class ModFile:
    FORMAT_TABLE: ClassVar[dict[int, str]] = {
        4: "M.K.",
        8: "8CHN",
        10: "10CH",
        12: "12CH",
        14: "14CH",
        16: "16CH",
    }

    MAX_POSITIONS: int = 127

    @classmethod
    def valid_channel_counts(cls) -> tuple[int, ...]:
        """The channel counts a format tag exists for: 4, 8, 10, 12, 14, 16."""
        return tuple(sorted(cls.FORMAT_TABLE))

    @classmethod
    def round_up_channels(cls, n: int) -> int:
        """The smallest valid channel count that holds `n` channels (16 at most)."""
        counts = cls.valid_channel_counts()
        return next((c for c in counts if c >= n), counts[-1])

    def __init__(self, channels=10):
        if channels not in self.FORMAT_TABLE:
            raise ValueError(
                f"MOD channel count must be one of {list(self.FORMAT_TABLE)} (got {channels})"
            )
        self.CHANNELS = channels
        self.MOD_FORMAT = self.FORMAT_TABLE[channels].encode("utf-8")
        self.SONG_LENGTH = self.MAX_POSITIONS
        self._name = "untitled"
        self.samples = [ModSample("") for _ in range(31)]
        self.positions = 1
        self.position_list = bytearray(self.MAX_POSITIONS + 1)
        self.patterns = [ModPattern(self.CHANNELS)]
        self._active_pattern = 0
        self._chan = 0
        self._row = 0
        self._inst = 0

    def set_name(self, name: str): self._name = name[0:19]
    def get_name(self): return self._name.ljust(19, ' ')
    def get_name_bytes(self): return bytearray(self.get_name(), 'utf-8') + b'\x00'

    def get_bytes(self):
        output = bytearray()
        output += self.get_name_bytes()
        for sample in self.samples:
            output += sample.get_bytes()
        output += self.positions.to_bytes(1, byteorder=BYTE_ORDER, signed=False)
        output += self.SONG_LENGTH.to_bytes(1, byteorder=BYTE_ORDER, signed=False)
        output += self.position_list
        output += self.MOD_FORMAT
        for pattern in self.patterns:
            output += pattern.get_bytes()
        # Exactly the bytes each header declares (its length is in words): an odd sample used to
        # be written whole, so every later one was read a byte early by players, its loop too
        for sample in self.samples:
            size = 2 * sample.length
            output += sample.data[:size].ljust(size, b"\0")
        return output

    def zero_idle_words(self) -> None:
        """Every one-shot sample's first word silenced (ModSample.zero_idle_word)."""
        for sample in self.samples:
            sample.zero_idle_word()

    def get_index(self):
        return self.cell_index(self._row, self._chan)

    # ------------------------------------------------------------------
    # Cell addressing — the one place that knows a cell is 4 bytes at
    # channel*4 + row*CHANNELS*4, so callers never index pattern data by hand.
    # ------------------------------------------------------------------

    def cell_index(self, row: int, channel: int) -> int:
        """Byte offset of (row, channel) within a pattern's data."""
        return channel * 4 + row * (self.CHANNELS * 4)

    def ensure_pattern(self, index: int) -> None:
        """Grow the pattern list so `index` exists.  No-op when it already does."""
        if index >= len(self.patterns):
            self.add_patterns(index - len(self.patterns) + 1)

    def effect_at(self, pattern: int, row: int, channel: int) -> tuple[int, int]:
        """(effect nibble, parameter byte) of one cell."""
        data = self.patterns[pattern].get_bytes()
        i = self.cell_index(row, channel)
        return data[i + 2] & 0x0F, data[i + 3]

    def note_at(self, pattern: int, row: int, channel: int) -> int:
        """12-bit Amiga period of one cell; 0 = no note trigger."""
        data = self.patterns[pattern].get_bytes()
        i = self.cell_index(row, channel)
        return ((data[i] & 0x0F) << 8) | data[i + 1]

    def effect_slot_free(self, pattern: int, row: int, channel: int) -> bool:
        """True when this cell carries no effect (a command may be written there)."""
        return self.effect_at(pattern, row, channel) == (0, 0)

    def free_effect_channel(self, pattern: int, row: int, order=None) -> int | None:
        """First channel of `order` (default 0..CHANNELS-1) with a free effect slot at this row."""
        for ch in (range(self.CHANNELS) if order is None else order):
            if self.effect_slot_free(pattern, row, ch):
                return ch
        return None

    def set_cursor(self, pattern: int, channel: int, row: int) -> None:
        """Where the next set_note / set_effect writes: (pattern, channel, row)."""
        self.set_active_pattern(pattern)
        self.set_channel(channel)
        self.set_row(row)

    def set_channel(self, chan: int):
        if chan < 0 or chan > self.CHANNELS - 1:
            print(f"Error: Channel {chan} is invalid.")
        else:
            self._chan = chan

    def set_row(self, row: int):
        if row < 0 or row > 63:
            print(f"Error: Row {row} is invalid.")
        else:
            self._row = row

    def set_note(self, n: ModNote, inst: int | None = None):
        if inst is None:
            inst = self._inst

        if inst < 0 or inst > 0x1f:
            print(f"Error: Instrument {inst} is invalid.")
            return
        note_period = PERIOD_TABLE[n.value]
        index = self.get_index()
        value = (note_period << 16) + ((inst & 0xf) << 12) + ((inst >> 4) << 28)

        pattern = self.patterns[self._active_pattern]
        pattern.set_entry(index + 0, (value >> 24) & 0xff)
        pattern.set_entry(index + 1, (value >> 16) & 0xff)

        # Retain effect parameter
        index_2 = pattern.get_entry(index + 2) & 0xf
        pattern.set_entry(index + 2, index_2 + ((value >> 8) & 0xff))

    def set_active_pattern(self, pattern: int):
        if pattern < 0 or pattern > self.MAX_POSITIONS:
            print(f"Error: Pattern {pattern} does not exist.")
            return
        if pattern >= len(self.patterns):
            self.add_patterns(pattern - len(self.patterns) + 1)
        self._active_pattern = pattern

    def add_patterns(self, number_to_add: int):
        if number_to_add <= 0: return

        length = len(self.patterns)
        add = number_to_add + length
        if add > self.MAX_POSITIONS:
            print("Warning: Exceeded 127 patterns. Truncating to 127.")
            number_to_add = self.MAX_POSITIONS - length
        for i in range(number_to_add):
            self.patterns.append(ModPattern(self.CHANNELS))
            self.position_list[length + i] = length + i
        self.positions += number_to_add

    def set_volume(self, vol: int):
        if vol < 0 or vol > 0x40:
            print(f"Warning: Volume out of range (0-64) {vol}")
            return
        index = self.get_index()
        pattern = self.patterns[self._active_pattern]
        pattern.set_entry(index + 2, 0xc + (pattern.get_entry(index + 2) & 0xf0))
        pattern.set_entry(index + 3, vol)

    def set_effect(self, effect: int, param: int):
        """Set a generic effect on the current channel/row.

        Args:
            effect: Effect number (0x0-0xF)
            param: Effect parameter (0x00-0xFF)
        """
        index = self.get_index()
        pattern = self.patterns[self._active_pattern]
        pattern.set_entry(index + 2, (pattern.get_entry(index + 2) & 0xf0) + (effect & 0xf))
        pattern.set_entry(index + 3, param & 0xff)

    def set_position_jump(self, position: int):
        """Set Bxx position jump effect on the current channel/row.

        Args:
            position: Position to jump to (0-127)
        """
        self.set_effect(0xB, position & 0x7f)

    def used_channels(self) -> int:
        """The columns the patterns use: one past the highest with any note, instrument or
        effect in it (0 for an empty MOD)."""
        stride = self.CHANNELS * _BYTES_PER_CELL
        highest = -1
        for pat in self.patterns:
            data = pat.get_bytes()
            for c in range(self.CHANNELS - 1, highest, -1):
                if any(data[r * stride + c * 4: r * stride + c * 4 + 4] != b"\0\0\0\0"
                       for r in range(_ROWS_PER_PATTERN)):
                    highest = c
                    break
        return highest + 1

    def narrow_to(self, channels: int) -> None:
        """Rewrite the patterns with `channels` columns (a valid count), dropping the columns
        past it, which must be empty — the merged build's home columns of channels that play
        elsewhere in every pattern.  The format tag follows."""
        if channels not in self.FORMAT_TABLE:
            raise ValueError(f"MOD channel count must be one of {list(self.FORMAT_TABLE)} (got {channels})")
        if channels >= self.CHANNELS:
            return
        if self.used_channels() > channels:
            raise ValueError(f"cannot narrow to {channels} channels: column {self.used_channels()} is in use")
        old_stride, new_stride = self.CHANNELS * _BYTES_PER_CELL, channels * _BYTES_PER_CELL
        new_patterns = []
        for pat in self.patterns:
            data = pat.get_bytes()
            np_ = ModPattern(channels)
            out = np_.get_bytes()
            for r in range(_ROWS_PER_PATTERN):
                out[r * new_stride: r * new_stride + new_stride] = data[r * old_stride: r * old_stride + new_stride]
            new_patterns.append(np_)
        self.patterns = new_patterns
        self.CHANNELS = channels
        self.MOD_FORMAT = self.FORMAT_TABLE[channels].encode("utf-8")

    def trim_to_pattern(self, last_pattern: int) -> None:
        """Remove patterns after last_pattern (unreachable once the loop-point Bxx is set).

        Called after ModLayout.loop_point to discard any trailing blank patterns that
        apply_pattern_breaks may create when the body doesn't divide evenly into
        64-row chunks.
        """
        keep = last_pattern + 1
        if len(self.patterns) <= keep:
            return
        del self.patterns[keep:]
        self.positions = keep
        for i in range(keep):
            self.position_list[i] = i
        for i in range(keep, self.MAX_POSITIONS + 1):
            self.position_list[i] = 0

    def add_samples(self, working_dir: str, sample_list: list):
        if sample_list is None: return
        for entry in sample_list:
            sample_index: int = entry[0]
            filename: str = entry[1]
            vol: int = entry[2]
            try:
                finetune: int = entry[3]
            except IndexError:
                finetune: int = 0
            full_path = os.path.join(working_dir, filename)

            if sample_index < 1 or sample_index > 0x1f:
                print(f"Error: Instrument {sample_index} is invalid.")
                continue

            if vol < 0 or vol > 0x40:
                print(f"Warning: Volume out of range (0-64). Setting to 64 {vol}")
                vol = 64

            if not os.path.exists(full_path):
                print(f"Warning: Sample file not found: {full_path}")
                continue

            with open(full_path, "rb") as file:
                data = file.read()
                if len(data) > 0xffff:
                    print(f"Error: {filename} is larger than 64K!")
                    continue

                sample = ModSample(filename)
                sample.data = data
                sample.set_volume(vol)
                sample.length = (len(data) + 1) // 2      # whole words: an odd file's last byte kept
                sample.set_finetune(finetune)
                self.samples[sample_index - 1] = sample

    def create_placeholder_samples(self, count=10):
        """Create minimal placeholder samples for instruments 1-count.
        Users can replace these with real samples later.
        """
        for i in range(1, min(count + 1, 32)):
            sample = ModSample(f"Placeholder {i}")
            # Create a tiny 2-byte silent sample
            sample.data = bytes([0, 0])
            sample.length = 1  # 1 word = 2 bytes
            sample.set_volume(64)
            self.samples[i - 1] = sample


def shift_for_breaks(flat_row: int, breaks: list[tuple[int, int]] | None) -> int:
    """Move a pre-break flat row index to where apply_pattern_breaks put it.

    Each break at (pattern P, row R) pushes everything from flat row P*64+R+1 onward
    to the start of pattern P+1, i.e. forward by the 63-R rows it blanked out.
    """
    for P, break_row in sorted(breaks or []):
        if flat_row >= P * 64 + break_row + 1:
            flat_row += 63 - break_row
    return flat_row


def apply_pattern_breaks(mod: ModFile, breaks: list) -> None:
    """Insert a Bxx position-jump and repack the pattern stream at the break row.

    The pattern data is treated as a continuous stream of rows.  For each
    (pattern_slot, row) in breaks (processed ascending):

      1. All rows after the break (pattern_slot rows row+1..63, then all rows
         of every subsequent pattern) are collected into a flat body stream.
      2. That body stream is repacked into patterns starting at pattern_slot+1,
         row 0 — so each new pattern is fully packed with 64 rows of body data.
         One extra pattern is appended at the end if the body doesn't divide
         evenly into 64-row chunks.
      3. Rows row+1..63 of pattern_slot are cleared.
      4. Bxx → pattern_slot+1 is written at (pattern_slot, row) on the first
         free effect channel so the player skips the blank rows and enters the
         repacked stream at row 0.
      5. All Bxx effects that targeted patterns after the break are updated to
         reflect the body data's new position.  If the target data now starts
         at row > 0 within its new pattern, a Dxx (pattern-break, BCD-encoded)
         is written alongside the Bxx on a free adjacent channel so playback
         begins at the correct row.

    Raises ValueError if the required extra pattern would exceed MAX_POSITIONS.
    """
    stride = mod.CHANNELS * 4  # bytes per row

    for orig_slot, row in sorted(breaks):
        P = orig_slot  # pattern indices don't shift (we append, not insert)

        # body_start_flat: the old flat-row index of the first body row
        body_start_flat = P * 64 + row + 1

        # --- collect body as an immutable snapshot --------------------------
        body = bytearray()
        p_data = mod.patterns[P].get_bytes()
        for r in range(row + 1, 64):
            body += p_data[r * stride: r * stride + stride]
        N = len(mod.patterns) - 1
        for pi in range(P + 1, N + 1):
            body += bytes(mod.patterns[pi].get_bytes())

        body_rows      = len(body) // stride           # e.g. 1056 for GHZ
        n_pats_needed  = (body_rows + 63) // 64        # ceil division
        n_pats_avail   = N - P                         # patterns P+1..N
        n_new          = max(0, n_pats_needed - n_pats_avail)

        if mod.positions + n_new > mod.MAX_POSITIONS:
            raise ValueError(
                f"apply_pattern_breaks: {n_new} extra pattern(s) needed but "
                f"would exceed MAX_POSITIONS ({mod.MAX_POSITIONS})"
            )

        # append extra patterns and rebuild identity position list
        for _ in range(n_new):
            mod.patterns.append(ModPattern(mod.CHANNELS))
        mod.positions += n_new
        for i in range(mod.positions):
            mod.position_list[i] = i

        # --- clear rows row+1..63 from pattern P ----------------------------
        for r in range(row + 1, 64):
            for b in range(stride):
                mod.patterns[P].set_entry(r * stride + b, 0)

        # --- rewrite patterns P+1 .. P+n_pats_needed from body --------------
        pat_size = 64 * stride
        for pi_new in range(n_pats_needed):
            pat = mod.patterns[P + 1 + pi_new]
            for i in range(pat_size):
                pat.set_entry(i, 0)
            for r in range(64):
                br = pi_new * 64 + r
                if br < body_rows:
                    for b in range(stride):
                        pat.set_entry(r * stride + b, body[br * stride + b])

        # --- update Bxx effects that targeted patterns after the break -------
        # Old target T (pattern T, row 0) is now at:
        #   body_row   = T*64 - body_start_flat
        #   new_pat    = P+1 + body_row // 64
        #   new_row    = body_row % 64
        # If new_row != 0 a Dxx (BCD row) must accompany the Bxx so the
        # player starts at the right row within the new pattern.
        for pat_i, pat in enumerate(mod.patterns):
            pat_data = pat.get_bytes()
            for row_i in range(64):
                for chan_i in range(mod.CHANNELS):
                    idx = mod.cell_index(row_i, chan_i)
                    if (pat_data[idx + 2] & 0xF) != 0xB:
                        continue
                    target = pat_data[idx + 3]
                    if target <= P:
                        continue
                    old_flat = target * 64
                    if old_flat < body_start_flat:
                        continue
                    br          = old_flat - body_start_flat
                    new_pat_num = P + 1 + br // 64
                    new_row_num = br % 64
                    pat.set_entry(idx + 3, new_pat_num & 0x7F)
                    if new_row_num != 0:
                        dxx_ch = mod.free_effect_channel(
                            pat_i, row_i, [c for c in range(mod.CHANNELS) if c != chan_i])
                        if dxx_ch is not None:
                            didx = mod.cell_index(row_i, dxx_ch)
                            pat.set_entry(didx + 2, (pat_data[didx + 2] & 0xF0) | 0xD)
                            pat.set_entry(didx + 3, row_to_bcd(new_row_num))

        # --- write Bxx at (P, row) → P+1 on the first free channel ----------
        bxx_chan = mod.free_effect_channel(P, row) or 0
        mod.set_active_pattern(P)
        mod.set_channel(bxx_chan)
        mod.set_row(row)
        mod.set_position_jump(P + 1)


# ---------------------------------------------------------------------------
# Reading a MOD back: the one parser the audits, the lint and the comparisons share.
#
#     0     title (20)
#     20    31 sample headers x 30: name (22), length (words), finetune, volume, loop start, loop length
#     950   song length, 951 restart byte, 952 order (128 pattern numbers)
#     1080  format tag ("M.K.", "8CHN", "10CH" ...)
#     1084  patterns (64 rows x channels x 4 bytes, every pattern the order holds), then the sample data
# ---------------------------------------------------------------------------

_SAMPLE_SLOTS = 31
_SAMPLE_HEADERS, _SAMPLE_HEADER_BYTES, _SAMPLE_NAME_BYTES = 20, 30, 22
_SONG_LENGTH_AT, _ORDER_AT, _ORDER_SLOTS = 950, 952, 128
_TAG_AT, _PATTERNS_AT = 1080, 1084
_LOOP_WORDS_NONE = 1           # a loop length of one word: no loop

# Tags with a fixed channel count; any other "nCHN" / "nnCH" names its own
_FIXED_TAGS = {"M.K.": 4, "M!K!": 4, "FLT4": 4, "FLT8": 8, "OCTA": 8}

Cell = tuple[int, int, int, int]    # (period, instrument, effect, parameter)


def format_channels(tag: str) -> int | None:
    """Channels a format tag stands for: "M.K." 4, "6CHN" 6, "14CH" 14; None for an unknown tag."""
    if tag in _FIXED_TAGS:
        return _FIXED_TAGS[tag]
    if tag[1:] == "CHN" and tag[0].isdigit():
        return int(tag[0])
    if tag[2:] == "CH" and tag[:2].isdigit():
        return int(tag[:2])
    return None


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
        seen: set[tuple[int, int]] = set()
        pos, row = 0, 0
        while pos < len(self.order) and (pos, row) not in seen:
            seen.add((pos, row))
            cells = self.patterns[self.order[pos]][row]
            yield self.order[pos], row, cells
            jump = None
            for _p, _i, eff, par in cells:
                if eff == 0xB:
                    jump = (par, 0)
                elif eff == 0xD:
                    jump = (jump[0] if jump is not None else pos + 1, (par >> 4) * 10 + (par & 0xF))
            if jump is not None:
                pos, row = jump
            elif row == _ROWS_PER_PATTERN - 1:
                pos, row = pos + 1, 0
            else:
                row += 1


def read_mod(source: bytes | str | os.PathLike) -> ModImage:
    """Parse a MOD (its bytes or its path).  Raises ValueError on a tag no reader knows."""
    d = source if isinstance(source, bytes) else Path(source).read_bytes()
    tag = d[_TAG_AT:_PATTERNS_AT].decode("latin1")
    channels = format_channels(tag)
    if channels is None:
        raise ValueError(f"unknown MOD format tag {tag!r}")

    headers = []
    for i in range(_SAMPLE_SLOTS):
        o = _SAMPLE_HEADERS + i * _SAMPLE_HEADER_BYTES
        h = d[o:o + _SAMPLE_HEADER_BYTES]
        headers.append((h[:_SAMPLE_NAME_BYTES].split(b"\0")[0].decode("latin1"),
                        int.from_bytes(h[22:24], BYTE_ORDER) * 2, ((h[24] & 0x0F) ^ 8) - 8, h[25],
                        int.from_bytes(h[26:28], BYTE_ORDER) * 2, int.from_bytes(h[28:30], BYTE_ORDER) * 2))

    order = list(d[_ORDER_AT:_ORDER_AT + d[_SONG_LENGTH_AT]])
    stored = max(d[_ORDER_AT:_ORDER_AT + _ORDER_SLOTS]) + 1
    off = _PATTERNS_AT
    patterns = []
    for _ in range(stored):
        rows = []
        for _r in range(_ROWS_PER_PATTERN):
            cells = []
            for _c in range(channels):
                b0, b1, b2, b3 = d[off:off + _BYTES_PER_CELL]
                cells.append((((b0 & 0x0F) << 8) | b1, (b0 & 0xF0) | (b2 >> 4), b2 & 0x0F, b3))
                off += _BYTES_PER_CELL
            rows.append(cells)
        patterns.append(rows)

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
        for r in range(_ROWS_PER_PATTERN):
            for c in range(channels):
                if c == keep:
                    continue
                off = _PATTERNS_AT + ((p * _ROWS_PER_PATTERN + r) * channels + c) * _BYTES_PER_CELL
                eff = b[off + 2] & 0x0F
                if eff in (0xF, 0xB, 0xD):
                    b[off], b[off + 1], b[off + 2] = 0, 0, eff
                else:
                    b[off:off + _BYTES_PER_CELL] = bytes(_BYTES_PER_CELL)
    return bytes(b)
