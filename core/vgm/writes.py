"""A VGM log's register writes one by one, each decoded: the chip channel it addresses and what it
sets.  What the frame log (frames.py) folds into states, in the order the driver wrote it: a
volume write that rewrites every TL, a key-off before a key-on in one frame.

    FM3    TL op1 $16            an operator register: its field, operator, value
    FM3    key on                $28: the channel's slots (on, off, or the slot mask)
    FM3    fnum hi / block $1A   $A4-$A6, $A0-$A2: the frequency's halves
    YM     LFO $0B               $22, the timers, $27, the DAC's enable: the chip's own
    PSG2   att 7 / tone 830      a latch and its data byte, a tone's both halves read as one
    NOISE  form E4
    DAC    seek 1234             a PCM bank seek; the DAC's data bytes are left out
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ..chips import OperatorReg, split_operator_register
from .reader import VgmLog, VgmOp, VgmWrite

_PART_CHANNELS = 3
_KEY, _DAC_DATA, _DAC_ENABLE, _LFO, _CH3_MODE = 0x28, 0x2A, 0x2B, 0x22, 0x27
_OPERATOR_FIRST = 0x30          # the first channel register (DT/MUL); below: the chip's own
_TIMERS = {0x24: "timer A hi", 0x25: "timer A lo", 0x26: "timer B"}
_FNUM_LO, _FNUM_HI, _CH3_LO, _CH3_HI, _ALGORITHM, _PAN = 0xA0, 0xA4, 0xA8, 0xAC, 0xB0, 0xB4
_ALL_SLOTS = 0xF
_KEY_CHANNELS = {0: 0, 1: 1, 2: 2, 4: 3, 5: 4, 6: 5}      # $28's channel bits -> FM1-FM6
_KEY_CHANNEL_BITS = 7
_OPERATOR_OF_SLOT = {0x0: 1, 0x4: 3, 0x8: 2, 0xC: 4}       # the chip's register order: OP1 OP3 OP2 OP4
_FIELDS = {OperatorReg.DT_MUL: "DT/MUL", OperatorReg.TL: "TL", OperatorReg.KS_AR: "KS/AR",
           OperatorReg.AM_D1R: "AM/D1R", OperatorReg.D2R: "D2R", OperatorReg.D1L_RR: "D1L/RR",
           OperatorReg.SSG_EG: "SSG-EG"}

_PSG_LATCH, _PSG_ATT, _PSG_NOISE_CHANNEL = 0x80, 0x10, 3
_PSG_DATA_LOW, _PSG_DATA_HIGH = 0x0F, 0x3F
_PSG_NAMES = ("PSG1", "PSG2", "PSG3", "NOISE")
_PSG_CHANNEL_SHIFT, _PSG_CHANNEL_MASK = 5, 3
_FM_TRACKS = 2 * _PART_CHANNELS  # track_of's: FM1-FM6, then the PSG's channels
_CHANNEL_MASK, _NO_CHANNEL = 3, 3   # a channel register's low bits: its channel in the part (3: none)
_KIND_MASK = 0xFC                # ... its other bits: what it sets
_FM3 = 2                        # the channel $A8-$AE (FM3's special mode) belong to
_PART_REGISTERS = 0x100         # tone_register's number: a YM part's registers each, then the PSG's tones
_PSG_TONES = 2 * _PART_REGISTERS
GLOBAL = "YM"


@dataclass(frozen=True)
class DecodedWrite:
    sample: int
    channel: str                # FM1-FM6, PSG1-PSG3, NOISE, DAC, or YM (the chip's own registers)
    text: str


def decoded_writes(log: VgmLog) -> Iterator[DecodedWrite]:
    """Every write but the DAC's data bytes, decoded, in order."""
    psg = _PsgLatch()
    writes = log.writes
    i = 0
    while i < len(writes):
        w = writes[i]
        i += 1
        if w.op is VgmOp.PCM_SEEK:
            yield DecodedWrite(w.sample, "DAC", f"seek {w.value}")
        elif w.op is VgmOp.PSG:
            after = writes[i] if i < len(writes) and writes[i].op is VgmOp.PSG else None
            decoded, took_next = psg.write(w.value, None if after is None else after.value)
            i += took_next
            yield DecodedWrite(w.sample, *decoded)
        elif not is_dac_byte(w):
            yield DecodedWrite(w.sample, *_fm(w))


def is_dac_byte(w: VgmWrite) -> bool:
    """A DAC data byte: what decoded_writes leaves out."""
    return w.op is VgmOp.FM and not w.port and w.reg == _DAC_DATA


def tone_register(w: VgmWrite) -> int | None:
    """The register `w` sets a channel's frequency in (a YM part's $A0-$AE, a PSG tone latch), as a
    number; None for any other write.  A driver writes each once an update."""
    if w.op is VgmOp.FM:
        return w.port * _PART_REGISTERS + w.reg if _FNUM_LO <= w.reg < _ALGORITHM else None
    if w.op is not VgmOp.PSG or not w.value & _PSG_LATCH or w.value & _PSG_ATT:
        return None
    channel = _psg_channel(w.value)
    return None if channel == _PSG_NOISE_CHANNEL else _PSG_TONES + channel


def track_of(w: VgmWrite) -> int | None:
    """The channel `w` writes, in a driver's track order: FM1-FM6 0-5, PSG1-PSG3 and noise 6-9.
    None: the chip's own registers, a DAC byte or seek, a PSG data byte (its latch's)."""
    if w.op is VgmOp.PSG:
        return _FM_TRACKS + _psg_channel(w.value) if w.value & _PSG_LATCH else None
    if w.op is not VgmOp.FM:
        return None
    if not w.port and w.reg == _KEY:
        return _KEY_CHANNELS.get(w.value & _KEY_CHANNEL_BITS)
    if w.reg < _OPERATOR_FIRST:
        return None
    if (w.reg & _KIND_MASK) in (_CH3_LO, _CH3_HI):
        return w.port * _PART_CHANNELS + _FM3
    channel = w.reg & _CHANNEL_MASK
    return None if channel == _NO_CHANNEL else w.port * _PART_CHANNELS + channel


def _psg_channel(latch: int) -> int:
    return (latch >> _PSG_CHANNEL_SHIFT) & _PSG_CHANNEL_MASK


def _fm(w: VgmWrite) -> tuple[str, str]:
    """(channel, text) of a YM2612 write."""
    reg, value = w.reg, w.value
    operator = split_operator_register(reg)
    if operator is not None:
        base, slot, channel = operator
        return _fm_name(w.port, channel), f"{_FIELDS[base]} op{_OPERATOR_OF_SLOT[slot]} ${value:02X}"
    if not w.port and reg == _KEY:
        return f"FM{_KEY_CHANNELS.get(value & _KEY_CHANNEL_BITS, 0) + 1}", _key(value >> 4)
    if not w.port and reg < _FNUM_LO:
        return GLOBAL, _global(reg, value)
    channel = reg & _CHANNEL_MASK
    kind = reg & _KIND_MASK
    names = {_FNUM_LO: "fnum lo", _FNUM_HI: "block/fnum hi", _CH3_LO: f"ch3 fnum lo #{channel}",
             _CH3_HI: f"ch3 block/fnum hi #{channel}", _ALGORITHM: "FB/ALG", _PAN: "pan/AMS/FMS"}
    if kind in (_CH3_LO, _CH3_HI):
        return _fm_name(w.port, _FM3), f"{names[kind]} ${value:02X}"
    return _fm_name(w.port, channel), f"{names.get(kind, f'reg ${reg:02X}')} ${value:02X}"


def _fm_name(port: int, channel: int) -> str:
    return f"FM{port * _PART_CHANNELS + channel + 1}"


def _key(slots: int) -> str:
    return "key off" if not slots else "key on" if slots == _ALL_SLOTS else f"key on slots {slots:X}"


def _global(reg: int, value: int) -> str:
    if reg == _LFO:
        return f"LFO ${value:02X}"
    if reg == _CH3_MODE:
        return f"ch3 mode / timers ${value:02X}"
    if reg == _DAC_ENABLE:
        return "DAC on" if value & 0x80 else "DAC off"
    return f"{_TIMERS.get(reg, f'reg ${reg:02X}')} ${value:02X}"


class _PsgLatch:
    """The SN76489's latch: a data byte belongs to the channel and register latched last."""

    def __init__(self) -> None:
        self._channel = 0
        self._attenuation = False
        self._low = 0

    def write(self, byte: int, after: int | None) -> tuple[tuple[str, str], int]:
        """((channel, text), 1 where `after`, the next byte, was its data byte and is read with it)."""
        if not byte & _PSG_LATCH:
            return self._data(byte), 0
        self._channel = _psg_channel(byte)
        self._attenuation = bool(byte & _PSG_ATT)
        name = _PSG_NAMES[self._channel]
        if self._attenuation:
            return (name, f"att {byte & _PSG_DATA_LOW:X}"), 0
        if self._channel == _PSG_NOISE_CHANNEL:
            return (name, f"form {byte:02X}"), 0
        self._low = byte & _PSG_DATA_LOW
        if after is None or after & _PSG_LATCH:
            return (name, f"tone lo {self._low}"), 0
        return self._data(after), 1

    def _data(self, byte: int) -> tuple[str, str]:
        name = _PSG_NAMES[self._channel]
        if self._attenuation or self._channel == _PSG_NOISE_CHANNEL:
            return name, f"data ${byte:02X}"
        return name, f"tone {(byte & _PSG_DATA_HIGH) << 4 | self._low}"
