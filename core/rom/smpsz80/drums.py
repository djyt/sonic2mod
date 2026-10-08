"""Type 0 FM's FM drums: each drum's program run on FM3 frame by frame, as the driver plays it.

The drum track's note n (low nibble) starts drum n (Golden Axe's driver, Z80 $0899):

    records  Z80 $096B   a word per drum -> ptr.w transpose.b volume.b envelope.b voice.b
    voices   Z80 $0987   a word per voice -> 26 bytes (smpsz80/layout.py)
    init     Z80 $0941   the drum track's flags, channel (FM3) and divider (1)

Both tables are found by the code that reads them, not by their addresses:

    ld de,init / ex de,hl / ldi x3 / dec a / ld hl,records / call / ld bc,6 / ldir / call /
    ld a,(voice) / ld hl,voices / call

A program is the driver's track bytecode with Type 0 FM's note modes:

    note [duration]                    a note; no duration: the last one
    $FC 01 ... $FC 00                  slide mode: note, slide (signed fnum step a frame), a byte the
                                       driver skips, duration; the slide wraps the octave at $27E / $4FE
    $E7                                the next note ties (no key-off, no key-on)

Each frame (V-int): TempoWait's hold first (the song's modifier, counted from the hit), then the
track: its timer counts down; at 0 it reads, else it slides.  A note keyed 256 frames without
another runs out (the driver's fill counter, never set, wraps).  A program that never stops (one
that runs on into voice data) is cut at _MAX_FRAMES; the converter renders it only as long as it
is heard.  The drum's volume adds to its voice's carrier TL; the drum track's own is 0 in every
Golden Axe song (its volume byte is not read).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from ...smps import CoordFlag, FmDrum, FmFrame, SmpsSongHeader, TempoSegment, tempo_schedule
from ..flags import FlagKind, FlagSpec
from ..image import RomError, RomImage
from ..voices import read_voices
from ..z80 import z80_ram
from .layout import VOICE_TYPE0
from .memory import Z80RamMemory

_MAX_FRAMES = 512            # ~8.5 s: past any hit's ring
_FILL_FRAMES = 0x100         # a note keyed this long without another runs out

_DRUM_TRACK = 0x80           # the drum track's note $8n plays FM drum n (bits 4-6: a PSG drum, unread)
_REST = 0x80
_FLAG = 0xE0
_SLIDE_MODE = 0xFC           # refused in a song (tracks.py); the drum programs' own
_SLIDE_ON = 1

_RECORD = 6                  # ptr.w transpose.b volume.b envelope.b voice.b
_INIT_DIVIDER = 2            # the init bytes: flags, channel, divider

# The slide's octave wrap (Z80 $0262): fnum at or under the low edge drops a block, over the high
# one rises one; the fnum moves by $280 either way
_WRAP_LOW, _WRAP_HIGH, _WRAP_STEP = 0x27E, 0x4FE, 0x580
_FNUM_MASK = 0x7FF
_WORD_MASK = 0xFFFF

# ld de,init / ex de,hl / ldi x3 / dec a / ld hl,records / call nn / ld bc,6 / ldir / call nn /
# ld a,(nn) / ld hl,voices / call
_READ_DRUM = re.compile(rb"\x11(..)\xEB\xED\xA0\xED\xA0\xED\xA0\x3D\x21(..)\xCD..\x01\x06\x00\xED\xB0\xCD.."
                        rb"\x3A..\x21(..)\xCD", re.DOTALL)


def read_fm_drums(rom: RomImage, header: SmpsSongHeader, fm_frequencies: tuple[int, ...],
                  flags: Mapping[int, FlagSpec]) -> dict[str, FmDrum]:
    """Every FM drum the drum track can name, by its DAC name (drum81 ...), as the song's tempo
    and FM table play it.  `flags`: the variant's, for the operands of the flags a program uses."""
    z80 = z80_ram(rom)
    ram = Z80RamMemory(RomImage(z80))
    init, records, voices = _tables(z80)
    divider = ram.byte(init + _INIT_DIVIDER)
    holds = tempo_schedule(header.tempo_modifier)[0]

    drums: dict[str, FmDrum] = {}
    for n in range(1, (voices - records) // 2 + 1):
        record = ram.word(records + (n - 1) * 2)
        start, transpose, volume, _, voice = (ram.word(record), *ram.bytes_at(record + 2, _RECORD - 2))
        voice_at = ram.word(voices + voice * 2)
        player = _Player(ram, flags, fm_frequencies, holds, divider, _signed(transpose))
        frames, cut = player.play(start)
        drums[f"drum{_DRUM_TRACK + n:02X}"] = FmDrum(read_voices(ram, voice_at, 1, VOICE_TYPE0)[0], volume,
                                                     frames, cut)
    return drums


def _tables(z80: bytes) -> tuple[int, int, int]:
    """(init, records, voices): what the driver's drum code loads."""
    found = list(_READ_DRUM.finditer(z80))
    if len(found) != 1:
        raise RomError(f"Z80 driver: {len(found)} drum readers (ld de,init / ldi x3 / ld hl,records ...), not one")
    init, records, voices = (int.from_bytes(found[0].group(g), "little") for g in (1, 2, 3))
    return init, records, voices


class _Player:
    """One drum program on FM3, a frame at a time."""

    def __init__(self, ram: Z80RamMemory, flags: Mapping[int, FlagSpec], table: tuple[int, ...],
                 holds: TempoSegment, divider: int, transpose: int):
        self._ram, self._flags, self._table, self._holds = ram, flags, table, holds
        self._divider = divider
        self._transpose = transpose
        self._pc = 0
        self._stack: list[int] = []
        self._loops: dict[int, int] = {}
        self._word = 0
        self._keyed = False
        self._duration = 0          # the last note's, for a note with none
        self._slide: int | None = None
        self._fill = 0

    def play(self, start: int) -> tuple[tuple[FmFrame, ...], str]:
        """The frames from the hit to the stop; or to a cap, and why."""
        self._pc = start
        frames: list[FmFrame] = []
        timer = 1                    # set as the hit starts the track: it reads this frame
        for frame in range(_MAX_FRAMES):
            if self._holds.holds(frame):
                timer += 1
            timer -= 1
            attack = False
            if timer == 0:
                try:
                    timer, attack = self._read()
                except _Stop as stop:
                    self._keyed = False
                    frames.append(FmFrame(self._word, False))
                    return tuple(frames), stop.why
            else:
                self._hold()
            frames.append(FmFrame(self._word, self._keyed, attack))
        return tuple(frames), f"no stop in {_MAX_FRAMES} frames"

    def _hold(self) -> None:
        """A frame between reads: the fill counter, then the slide."""
        self._fill = (self._fill - 1) % _FILL_FRAMES
        if self._fill == 0:
            self._keyed = False
            return
        if self._slide is not None:
            self._word = _wrap((self._word + self._slide) & _WORD_MASK)

    def _read(self) -> tuple[int, bool]:
        """The track's next note: (frames until the next read, whether it keys on)."""
        no_attack = False
        while True:
            byte = self._take()
            if byte < _FLAG:
                break
            no_attack = self._flag(byte, no_attack)

        if self._keyed and not no_attack:
            self._keyed = False

        # A note: its word, then a slide (slide mode) and a duration; a lone duration re-keys
        if byte >= _REST:
            index = byte - _REST + (self._transpose if byte != _REST else 0)
            if not 0 <= index < len(self._table):
                raise _Stop(f"note ${byte:02X} at transposition {self._transpose}: off the FM table")
            self._word = self._table[index]
            if self._slide is not None:
                self._slide = _signed(self._take())
                self._take()
                self._duration = self._take() * self._divider
            elif self._ram.byte(self._pc) < _REST:
                self._duration = self._take() * self._divider
        else:
            self._duration = byte * self._divider

        if not no_attack:
            self._fill = 0
        attack = bool(self._word) and not no_attack
        if attack:
            self._keyed = True
        return self._duration, attack

    def _flag(self, byte: int, no_attack: bool) -> bool:
        """One flag; whether the next note ties."""
        if byte == _SLIDE_MODE:
            self._slide = 0 if self._take() == _SLIDE_ON else None
            return no_attack if self._slide is not None else False
        spec = self._flags.get(byte)
        if spec is None or spec.kind is FlagKind.REFUSE:
            raise _Stop(f"${byte:02X} at Z80 ${self._pc - 1:04X}: not a drum program's")
        if spec.kind is FlagKind.NO_ATTACK:
            return True
        if spec.kind is FlagKind.STOP:
            raise _Stop("")
        if spec.kind is FlagKind.JUMP:
            self._pc = self._ram.code_pointer(self._pc)
        elif spec.kind is FlagKind.CALL:
            self._stack.append(self._pc + 2)
            self._pc = self._ram.code_pointer(self._pc)
        elif spec.kind is FlagKind.RETURN:
            self._pc = self._stack.pop()
        elif spec.kind is FlagKind.LOOP:
            self._loop()
        elif spec.kind is FlagKind.EFFECT and spec.flag == CoordFlag.CHANGE_TRANSPOSITION:
            self._transpose += _signed(self._take())
        elif spec.kind is FlagKind.DROP:
            self._pc += spec.operands
        else:
            raise _Stop(f"${byte:02X} at Z80 ${self._pc - 1:04X}: {spec.flag} in a drum program")
        return no_attack

    def _loop(self) -> None:
        """index, count, pointer: the counter loads with the count when it is 0, then counts down."""
        index, count = self._take(), self._take()
        left = self._loops.get(index) or count
        self._loops[index] = left - 1
        if left - 1:
            self._pc = self._ram.code_pointer(self._pc)
        else:
            self._pc += 2

    def _take(self) -> int:
        byte = self._ram.byte(self._pc)
        self._pc += 1
        return byte


class _Stop(Exception):
    """The program ends: at its stop (why ""), or where it cannot be followed."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


def _wrap(word: int) -> int:
    fnum = word & _FNUM_MASK
    if fnum <= _WRAP_LOW:
        return (word - _WRAP_STEP) & _WORD_MASK
    if fnum > _WRAP_HIGH:
        return (word + _WRAP_STEP) & _WORD_MASK
    return word


def _signed(byte: int) -> int:
    return byte - 0x100 if byte > 0x7F else byte

