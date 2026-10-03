"""YM2612 + SN76489 register state, replayed over a VgmLog write by write.

`ChipState.replay(log)` applies each write and yields a `Change` where one is musically
visible: a key on/off, a frequency latched, a PSG period or attenuation written, the noise
register, a DAC byte, a PCM seek.  Everything else (operator registers, algorithm, pan) only
updates the state, which the accessors read at any point of the replay.

What the chips do with the writes, not what the driver meant by them:

    YM2612   A4..A6 is latched and takes effect with the A0..A2 write after it (one FNUM/block
             pair, one latch for the whole chip); 0x28 keys a channel's operator slots; 0x2A is the DAC.
    SN76489  a latch byte selects channel + register and writes its low bits; a data byte
             writes the latched register (tone: the period's high 6 bits).  A period latch
             followed by its data byte at the same sample is one write: the half-written
             period in between is never heard, so it yields no change.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import Enum
from typing import NamedTuple

from ..smps import CARRIER_OFFSETS_BY_ALG, MD_FM_CLOCK, MD_PSG_CLOCK
from .reader import VgmLog, VgmOp, VgmWrite

FM_CHANNELS = 6
PSG_TONE_CHANNELS = 3
NOISE_CHANNEL = 3                   # the SN76489's fourth channel
DAC_CHANNEL = 5                     # FM6, while 0x2B enables the DAC
PSG_SILENT = 0xF                    # 4-bit attenuation: off

# YM2612 registers
_REG_LFO = 0x22
_REG_KEY = 0x28
_REG_DAC = 0x2A
_REG_DAC_ENABLE = 0x2B
_REG_TL = 0x40
_REG_FNUM_LO = 0xA0
_REG_FNUM_HI = 0xA4
_REG_ALGORITHM = 0xB0              # feedback << 3 | algorithm
_REG_PAN = 0xB4                    # L R AMS PMS
_CHANNELS_PER_PORT = 3
_KEY_SLOTS_SHIFT = 4
_KEY_PORT1_BIT = 0x04
_KEY_CHANNEL_MASK = 0x03
_KEY_UNUSED = 0x03                 # channel field 3 (and 7) addresses nothing
_DAC_ENABLE_BIT = 0x80
_TL_MASK = 0x7F
_ALGORITHM_MASK = 0x07
_FNUM_HI_MASK = 0x07
_BLOCK_SHIFT = 3

# SN76489 bytes
_PSG_LATCH = 0x80
_PSG_VOLUME_BIT = 0x10
_PSG_LOW_NIBBLE = 0x0F
_PSG_HIGH_BITS = 0x3F
_PSG_HIGH_SHIFT = 4
_NOISE_MASK = 0x07
_NOISE_RATE_MASK = 0x03
_NOISE_TONE2_RATE = 3              # the LFSR is clocked by tone channel 2
_NOISE_BASE_DIVIDER = 512          # rates 0-2: clock / (512 << rate)

# Pitch: f = clock x fnum / (144 x 2^(21 - block));  f = clock / (32 x period)
_FM_CLOCK_DIVIDER = 144
_FM_FNUM_BITS = 21
_PSG_CLOCK_DIVIDER = 32


class ChangeKind(Enum):
    FM_KEY = "fm_key"               # value: the slots keyed on (0 = key-off)
    FM_FREQUENCY = "fm_frequency"   # value: FNUM | block << 11, as latched
    PSG_TONE = "psg_tone"           # value: the period
    PSG_VOLUME = "psg_volume"       # value: the attenuation BEFORE the write (read the new one from the state)
    PSG_NOISE = "psg_noise"         # value: the noise register
    DAC_WRITE = "dac_write"         # value: the byte
    PCM_SEEK = "pcm_seek"           # value: the PCM bank offset


class Change(NamedTuple):
    sample: int
    kind: ChangeKind
    channel: int                    # FM 0-5 / PSG 0-3 (3 = noise) / DAC_CHANNEL
    value: int


def fm_frequency_hz(fnum: int, block: int, clock: int) -> float:
    """YM2612: f = fnum x (clock / 144) x 2^block / 2^21  (A4 = fnum 1083, block 4 at the MD clock)."""
    return clock * fnum / (_FM_CLOCK_DIVIDER * (1 << (_FM_FNUM_BITS - block)))


def psg_frequency_hz(period: int, clock: int) -> float:
    """SN76489: f = clock / (32 x period); 0 for period 0."""
    return clock / (_PSG_CLOCK_DIVIDER * period) if period > 0 else 0.0


class ChipState:
    """The registers a log has written so far."""

    def __init__(self, fm_clock: int = MD_FM_CLOCK, psg_clock: int = MD_PSG_CLOCK) -> None:
        self.fm_clock = fm_clock
        self.psg_clock = psg_clock

        self._fm_regs = [bytearray(256), bytearray(256)]          # per port
        self._fm_hi_latch = 0                                     # A4..A6 waits here for A0..A2
        self._fm_freq = [0] * FM_CHANNELS                         # FNUM | block << 11, as latched
        self._fm_slots = [0] * FM_CHANNELS

        self._psg_att = [PSG_SILENT] * (PSG_TONE_CHANNELS + 1)
        self._psg_period = [0] * PSG_TONE_CHANNELS
        self._noise = 0
        self._latch_channel = 0
        self._latch_volume = False

    @classmethod
    def for_log(cls, log: VgmLog, fm_clock: int = MD_FM_CLOCK, psg_clock: int = MD_PSG_CLOCK) -> ChipState:
        """The log's own clocks, the given ones where its header has none."""
        return cls(log.header.fm_clock or fm_clock, log.header.psg_clock or psg_clock)

    # ---- the replay ----

    def replay(self, log: VgmLog) -> Iterator[Change]:
        """Apply every write of `log`, yielding the changes they make."""
        writes = log.writes
        for i, w in enumerate(writes):
            following = writes[i + 1] if i + 1 < len(writes) else None
            change = self.apply(w, following)
            if change is not None:
                yield change

    def apply(self, w: VgmWrite, following: VgmWrite | None = None) -> Change | None:
        """One write; `following` is the next write of the log (a PSG period latch waits for its data byte)."""
        if w.op is VgmOp.FM:
            return self._fm_write(w)
        if w.op is VgmOp.PSG:
            return self._psg_write(w, following)
        return Change(w.sample, ChangeKind.PCM_SEEK, DAC_CHANNEL, w.value)

    # ---- YM2612 ----

    def _fm_write(self, w: VgmWrite) -> Change | None:
        self._fm_regs[w.port][w.reg] = w.value
        if w.port == 0 and w.reg == _REG_KEY:
            return self._fm_key(w)
        if w.port == 0 and w.reg == _REG_DAC:
            return Change(w.sample, ChangeKind.DAC_WRITE, DAC_CHANNEL, w.value)

        # Frequency: the high byte is latched, the low byte writes the pair
        if _REG_FNUM_HI <= w.reg < _REG_FNUM_HI + _CHANNELS_PER_PORT:
            self._fm_hi_latch = w.value
            return None
        if not _REG_FNUM_LO <= w.reg < _REG_FNUM_LO + _CHANNELS_PER_PORT:
            return None
        ch = w.port * _CHANNELS_PER_PORT + w.reg - _REG_FNUM_LO
        hi = self._fm_hi_latch
        fnum = (hi & _FNUM_HI_MASK) << 8 | w.value
        block = (hi >> _BLOCK_SHIFT) & _FNUM_HI_MASK
        self._fm_freq[ch] = fnum | block << 11
        return Change(w.sample, ChangeKind.FM_FREQUENCY, ch, self._fm_freq[ch])

    def _fm_key(self, w: VgmWrite) -> Change | None:
        field = w.value & _KEY_CHANNEL_MASK
        if field == _KEY_UNUSED:
            return None
        ch = field + (_CHANNELS_PER_PORT if w.value & _KEY_PORT1_BIT else 0)
        self._fm_slots[ch] = w.value >> _KEY_SLOTS_SHIFT
        return Change(w.sample, ChangeKind.FM_KEY, ch, self._fm_slots[ch])

    def _fm_channel_reg(self, ch: int, base: int, slot_offset: int = 0) -> int:
        port, local = divmod(ch, _CHANNELS_PER_PORT)
        return self._fm_regs[port][base + slot_offset + local]

    def fm_fnum_block(self, ch: int) -> tuple[int, int]:
        f = self._fm_freq[ch]
        return f & 0x7FF, f >> 11

    def fm_hz(self, ch: int) -> float:
        return fm_frequency_hz(*self.fm_fnum_block(ch), self.fm_clock)

    def fm_slots(self, ch: int) -> int:
        """The operator slots keyed on (0 = key-off)."""
        return self._fm_slots[ch]

    def fm_algorithm(self, ch: int) -> int:
        return self._fm_channel_reg(ch, _REG_ALGORITHM) & _ALGORITHM_MASK

    def fm_feedback_algorithm(self, ch: int) -> int:
        """Register B0: feedback << 3 | algorithm."""
        return self._fm_channel_reg(ch, _REG_ALGORITHM)

    def fm_pan(self, ch: int) -> int:
        """Register B4: L R AMS PMS."""
        return self._fm_channel_reg(ch, _REG_PAN)

    def fm_tl(self, ch: int, slot_offset: int) -> int:
        """Total level of the operator at register offset `slot_offset` (0x00 / 0x04 / 0x08 / 0x0C)."""
        return self._fm_channel_reg(ch, _REG_TL, slot_offset) & _TL_MASK

    def fm_carrier_tls(self, ch: int) -> tuple[int, ...]:
        """The carriers' total levels, in register-offset order."""
        return tuple(self.fm_tl(ch, s) for s in sorted(CARRIER_OFFSETS_BY_ALG[self.fm_algorithm(ch)]))

    def fm_operator_regs(self, ch: int, first: int, last: int) -> bytes:
        """The channel's operator registers `first`..`last` (0x30..0x9F: four slots per register)."""
        return bytes(self._fm_channel_reg(ch, base, slot)
                     for base in range(first, last + 1, 0x10) for slot in (0x00, 0x04, 0x08, 0x0C))

    def fm_global(self, reg: int) -> int:
        """A port-0 global register (0x22 LFO, 0x27 mode, 0x2B DAC enable)."""
        return self._fm_regs[0][reg]

    @property
    def dac_enabled(self) -> bool:
        return bool(self._fm_regs[0][_REG_DAC_ENABLE] & _DAC_ENABLE_BIT)

    @property
    def lfo(self) -> int:
        return self._fm_regs[0][_REG_LFO]

    # ---- SN76489 ----

    def _psg_write(self, w: VgmWrite, following: VgmWrite | None) -> Change | None:
        b = w.value
        if b & _PSG_LATCH:
            self._latch_channel = (b >> 5) & 3
            self._latch_volume = bool(b & _PSG_VOLUME_BIT)
        ch = self._latch_channel

        if self._latch_volume:
            old = self._psg_att[ch]
            self._psg_att[ch] = b & _PSG_LOW_NIBBLE
            return Change(w.sample, ChangeKind.PSG_VOLUME, ch, old)

        if ch == NOISE_CHANNEL:
            self._noise = b & _NOISE_MASK
            return Change(w.sample, ChangeKind.PSG_NOISE, ch, self._noise)

        # Tone period: a latch writes the low nibble, a data byte the high six bits
        period = self._psg_period[ch]
        if b & _PSG_LATCH:
            period = (period & ~_PSG_LOW_NIBBLE) | (b & _PSG_LOW_NIBBLE)
        else:
            period = (b & _PSG_HIGH_BITS) << _PSG_HIGH_SHIFT | (period & _PSG_LOW_NIBBLE)
        self._psg_period[ch] = period
        if b & _PSG_LATCH and _data_byte_follows(w, following):
            return None
        return Change(w.sample, ChangeKind.PSG_TONE, ch, period)

    def psg_period(self, ch: int) -> int:
        return self._psg_period[ch]

    def psg_hz(self, ch: int) -> float:
        return psg_frequency_hz(self._psg_period[ch], self.psg_clock)

    def psg_attenuation(self, ch: int) -> int:
        return self._psg_att[ch]

    def psg_audible(self, ch: int) -> bool:
        return self._psg_att[ch] < PSG_SILENT

    @property
    def noise(self) -> int:
        """The noise register: bit 2 white, bits 0-1 rate."""
        return self._noise

    @property
    def noise_white(self) -> bool:
        return bool(self._noise & 0x04)

    @property
    def noise_rate(self) -> int:
        return self._noise & _NOISE_RATE_MASK

    def noise_shift_hz(self) -> float:
        """The LFSR's shift rate: clock / (512 << rate), or tone 2's frequency at rate 3 (its period 0
        counted as 1, as the Sega VDP PSG does)."""
        if self.noise_rate != _NOISE_TONE2_RATE:
            return self.psg_clock / float(_NOISE_BASE_DIVIDER << self.noise_rate)
        return self.psg_clock / (float(_PSG_CLOCK_DIVIDER) * (self._psg_period[2] or 1))


def _data_byte_follows(w: VgmWrite, following: VgmWrite | None) -> bool:
    """`following` is the data byte finishing the period `w` latched, at the same instant."""
    return (following is not None and following.op is VgmOp.PSG and following.sample == w.sample
            and not following.value & _PSG_LATCH)
