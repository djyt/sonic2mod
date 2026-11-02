"""Writing a MOD: its samples, patterns and order, built up cell by cell (core.mod.cell)."""

import copy
import os
from collections.abc import Iterator
from typing import ClassVar

from .cell import CELL_BYTES, ROWS_PER_PATTERN, Cell
from .notes import PERIOD_TABLE, ModNote
from .reader import BYTE_ORDER, SAMPLE_NAME_BYTES, TITLE_BYTES, ModImage, SampleInfo, sample_name

# Paula's clock on a PAL Amiga: a sample plays at PAL_AMIGA_CLOCK / period bytes a second
PAL_AMIGA_CLOCK = 3_546_895


# The word ProTracker replays after a one-shot sample ends: its first two bytes
_IDLE_WORD_BYTES = 2


def _name_field(name: str, size: int) -> bytearray:
    """A title or sample name field: ASCII, NUL padded to `size` (a full one has no terminator)."""
    return bytearray(name.encode('ascii', 'replace')[:size].ljust(size, b'\x00'))


class ModSample:
    _name: str
    length: int
    _finetune: int
    _volume: int
    repeat: int
    repeat_length: int
    data: bytes
    file: str | None    # the sample_list file its bytes came from (read_sample); None: made here

    def __init__(self, name):
        self._name = name[:SAMPLE_NAME_BYTES]
        self.length = 0
        self._finetune = 0
        self._volume = 0
        self.repeat = 0
        self.repeat_length = 1
        self.data = bytes(0)
        self.file = None

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
            raise ValueError(f"finetune {v}: not in -8..7")
        self._finetune = v & 0xf

    def set_volume(self, v):
        if v < 0 or v > 64:
            raise ValueError(f"volume {v}: not in 0..64")
        self._volume = int(v)

    @property
    def looped(self) -> bool:
        return self.repeat_length > 1

    def zero_idle_word(self) -> None:
        """A one-shot's first word silenced.  ProTracker replays it once the sample ends, so
        (-126, 126) there buzzes until the next note.  A looped sample replays its loop instead."""
        if self.looped or len(self.data) < _IDLE_WORD_BYTES:
            return
        self.data = bytes(_IDLE_WORD_BYTES) + self.data[_IDLE_WORD_BYTES:]

    def stored_bytes(self) -> bytes:
        """Exactly the bytes the header declares (its length is in words), NUL padded."""
        size = 2 * self.length
        return bytes(self.data[:size].ljust(size, b"\0"))

    def set_name(self, name: str) -> None: self._name = name[:SAMPLE_NAME_BYTES]
    def get_name_bytes(self):
        return _name_field(self._name, SAMPLE_NAME_BYTES)
    def get_bytes(self):
        output = bytearray()
        output += self.get_name_bytes()
        output += self.length.to_bytes(2, byteorder=BYTE_ORDER, signed=False)
        output += self._finetune.to_bytes(1, byteorder=BYTE_ORDER, signed=True)
        output += self._volume.to_bytes(1, byteorder=BYTE_ORDER, signed=False)
        output += self.repeat.to_bytes(2, byteorder=BYTE_ORDER, signed=False)
        output += self.repeat_length.to_bytes(2, byteorder=BYTE_ORDER, signed=False)
        return output


MAX_INSTRUMENT = 0x1F           # a cell's instrument field (5 bits); 0 = none
_MAX_SAMPLE_BYTES = 0xFFFF      # what sample_list loads
_EMPTY = Cell()


class ModPattern:
    """64 rows of `channels` cells, as the file stores them (core.mod.cell)."""
    _data: bytearray
    _stride: int        # bytes a row

    def __init__(self, channels: int = 4):
        self._stride = channels * CELL_BYTES
        self._data = bytearray(self._stride * ROWS_PER_PATTERN)

    def get_bytes(self) -> bytes:
        return bytes(self._data)

    def cell(self, row: int, channel: int) -> Cell:
        return Cell.decode(self._data, row * self._stride + channel * CELL_BYTES)

    def put(self, row: int, channel: int, cell: Cell) -> None:
        at = row * self._stride + channel * CELL_BYTES
        self._data[at:at + CELL_BYTES] = cell.encode()

    def cells(self) -> Iterator[tuple[int, int, Cell]]:
        """(row, channel, cell) of every cell, row by row."""
        channels = self._stride // CELL_BYTES
        for row in range(ROWS_PER_PATTERN):
            for channel in range(channels):
                yield row, channel, self.cell(row, channel)

    def column_used(self, channel: int) -> bool:
        """Any row of the column holds a note, an instrument or an effect."""
        return any(self.cell(row, channel) != _EMPTY for row in range(ROWS_PER_PATTERN))

    def row_bytes(self, row: int) -> bytes:
        return bytes(self._data[row * self._stride:(row + 1) * self._stride])

    def put_row(self, row: int, data: bytes) -> None:
        """A row's cells; `data` as wide as this pattern's rows or narrower (the rest left empty)."""
        if len(data) > self._stride:
            raise ValueError(f"row of {len(data)} bytes: wider than {self._stride}")
        self._data[row * self._stride:(row + 1) * self._stride] = data.ljust(self._stride, b"\0")

    def clear_row(self, row: int) -> None:
        self.put_row(row, b"")


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

    def set_name(self, name: str): self._name = name[:TITLE_BYTES]
    def get_name_bytes(self): return _name_field(self._name, TITLE_BYTES)

    def scratch(self) -> "ModFile":
        """A copy to write cells into and throw away: its patterns and order its own, the samples
        this one's (read, not written)."""
        out = copy.copy(self)
        out.patterns = copy.deepcopy(self.patterns)
        out.position_list = bytearray(self.position_list)
        out.samples = list(self.samples)
        return out

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
            output += sample.stored_bytes()
        return output

    def image(self) -> ModImage:
        """The MOD as read_mod reads get_bytes() back, without writing it."""
        stored = max(self.position_list) + 1
        samples = [SampleInfo(sample_name(s.get_name_bytes()), 2 * s.length, s.finetune, s.volume,
                              2 * s.repeat, 2 * s.repeat_length, s.stored_bytes())
                   for s in self.samples]
        patterns = [[[p.cell(r, c) for c in range(self.CHANNELS)] for r in range(ROWS_PER_PATTERN)]
                    for p in self.patterns[:stored]]
        return ModImage(self.MOD_FORMAT.decode("latin1"), self.CHANNELS, samples,
                        list(self.position_list[:self.positions]), patterns)

    def zero_idle_words(self) -> None:
        """Every one-shot sample's first word silenced (ModSample.zero_idle_word)."""
        for sample in self.samples:
            sample.zero_idle_word()

    # --- cells, addressed (pattern, row, channel) --------------------------------------------------
    def cell_at(self, pattern: int, row: int, channel: int) -> Cell:
        return self.patterns[pattern].cell(row, channel)

    def note_at(self, pattern: int, row: int, channel: int) -> int:
        """12-bit Amiga period of one cell; 0 = no note trigger."""
        return self.cell_at(pattern, row, channel).period

    def effect_at(self, pattern: int, row: int, channel: int) -> tuple[int, int]:
        """(effect nibble, parameter byte) of one cell."""
        cell = self.cell_at(pattern, row, channel)
        return cell.effect, cell.param

    def effect_slot_free(self, pattern: int, row: int, channel: int) -> bool:
        """True when this cell carries no effect (a command may be written there)."""
        return self.effect_at(pattern, row, channel) == (0, 0)

    def free_effect_channel(self, pattern: int, row: int, order=None) -> int | None:
        """First channel of `order` (default 0..CHANNELS-1) with a free effect slot at this row."""
        for ch in (range(self.CHANNELS) if order is None else order):
            if self.effect_slot_free(pattern, row, ch):
                return ch
        return None

    def put_note(self, pattern: int, row: int, channel: int, note: ModNote, instrument: int) -> None:
        """A note-on: period and instrument; the cell's effect stays."""
        if instrument < 0 or instrument > MAX_INSTRUMENT:
            raise IndexError(f"instrument {instrument}: not in 0..{MAX_INSTRUMENT}")
        cell = self._writable(pattern, row, channel)
        self.patterns[pattern].put(row, channel, cell._replace(period=PERIOD_TABLE[note.value], instrument=instrument))

    def put_effect(self, pattern: int, row: int, channel: int, effect: int, param: int) -> None:
        """A command (effect 0x0-0xF, parameter 0x00-0xFF); the cell's note stays."""
        cell = self._writable(pattern, row, channel)
        self.patterns[pattern].put(row, channel, cell._replace(effect=effect & 0xF, param=param & 0xFF))

    def _writable(self, pattern: int, row: int, channel: int) -> Cell:
        """The cell a write goes to, its pattern made if need be; a place past the MOD is an IndexError."""
        if pattern < 0 or pattern > self.MAX_POSITIONS:
            raise IndexError(f"pattern {pattern}: not in 0..{self.MAX_POSITIONS}")
        if channel < 0 or channel > self.CHANNELS - 1:
            raise IndexError(f"channel {channel}: not in 0..{self.CHANNELS - 1}")
        if row < 0 or row > ROWS_PER_PATTERN - 1:
            raise IndexError(f"row {row}: not in 0..{ROWS_PER_PATTERN - 1}")
        self.ensure_pattern(pattern)
        return self.cell_at(pattern, row, channel)

    # --- patterns ----------------------------------------------------------------------------------
    def ensure_pattern(self, index: int) -> None:
        """Grow the pattern list so `index` exists.  No-op when it already does."""
        if index >= len(self.patterns):
            self.add_patterns(index - len(self.patterns) + 1)

    def add_patterns(self, number_to_add: int):
        if number_to_add <= 0: return

        length = len(self.patterns)
        add = number_to_add + length
        if add > self.MAX_POSITIONS:
            raise IndexError(f"{add} patterns: past the MOD's {self.MAX_POSITIONS}")
        for i in range(number_to_add):
            self.patterns.append(ModPattern(self.CHANNELS))
            self.position_list[length + i] = length + i
        self.positions += number_to_add

    def used_channels(self) -> int:
        """The columns the patterns use: one past the highest with any note, instrument or
        effect in it (0 for an empty MOD)."""
        highest = -1
        for pat in self.patterns:
            for c in range(self.CHANNELS - 1, highest, -1):
                if pat.column_used(c):
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
        width = channels * CELL_BYTES
        new_patterns = []
        for pat in self.patterns:
            narrow = ModPattern(channels)
            for r in range(ROWS_PER_PATTERN):
                narrow.put_row(r, pat.row_bytes(r)[:width])
            new_patterns.append(narrow)
        self.patterns = new_patterns
        self.CHANNELS = channels
        self.MOD_FORMAT = self.FORMAT_TABLE[channels].encode("utf-8")

    def compact_samples(self) -> dict[int, int]:
        """Close the gaps between the sample slots in use: every slot that holds a sample or that a
        note names keeps its order and moves down, and every cell's instrument number follows.
        Loops, volumes and finetunes travel with their sample, and a 9xx offset is relative to its
        own.  Returns {old slot: new slot} (1-based) for every slot kept, or {} when none moved."""
        named = {cell.instrument for pat in self.patterns for _r, _c, cell in pat.cells() if cell.instrument}
        keep = [s for s in range(1, len(self.samples) + 1) if self.samples[s - 1].length or s in named]
        mapping = {old: new for new, old in enumerate(keep, 1)}
        moved = {old: new for old, new in mapping.items() if old != new}
        if not moved:
            return {}
        self.samples = [self.samples[old - 1] for old in keep] + [
            ModSample("") for _ in range(len(self.samples) - len(keep))]
        for pat in self.patterns:
            for row, channel, cell in pat.cells():
                new = moved.get(cell.instrument)
                if new is not None:
                    pat.put(row, channel, cell._replace(instrument=new))
        return mapping

    def trim_tails(self, heard: dict[int, int | None]) -> dict[int, tuple[int, int]]:
        """Each one-shot sample cut after the bytes its notes are heard to (`heard`, {slot: bytes;
        None where all of it may be}: core.mod.reach.heard_bytes), in whole words.  A looped sample
        and one read from a file (`ModSample.file`: the same drum in several MODs stays the same
        bytes) are left whole.  Returns {slot: (bytes before, bytes after)} of every sample cut."""
        cut: dict[int, tuple[int, int]] = {}
        for slot, n in heard.items():
            s = self.samples[slot - 1]
            if n is None or s.looped or s.file is not None:
                continue
            keep = max(_IDLE_WORD_BYTES, n + (n & 1))
            if keep < 2 * s.length:
                cut[slot] = (2 * s.length, keep)
                s.data, s.length = s.data[:keep], keep // 2
        return cut

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


def read_sample(working_dir: str, sample_index: int, filename: str, vol: int, finetune: int) -> ModSample | None:
    """A sample_list row's file (`filename` in `working_dir`) as instrument `sample_index`, with its
    volume and finetune; None where the file is not found.  A slot or size the MOD cannot hold is a
    config error."""
    if sample_index < 1 or sample_index > MAX_INSTRUMENT:
        raise ValueError(f"sample_list: instrument {sample_index} is not in 1..{MAX_INSTRUMENT}")
    full_path = os.path.join(working_dir, filename)
    if not os.path.exists(full_path):
        return None
    with open(full_path, "rb") as file:
        data = file.read()
    if len(data) > _MAX_SAMPLE_BYTES:
        raise ValueError(f"sample_list: {filename} is {len(data)} bytes, past {_MAX_SAMPLE_BYTES}")
    sample = ModSample(filename)
    sample.data = data
    sample.file = filename
    sample.set_volume(vol)
    sample.length = (len(data) + 1) // 2      # whole words: an odd file's last byte kept
    sample.set_finetune(finetune)
    return sample
