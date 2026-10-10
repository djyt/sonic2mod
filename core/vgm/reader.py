"""VGM / VGZ container: commands -> a flat, timestamped list of chip writes.

No interpretation: a write is what the command says, at the VGM sample (44 100 Hz) it happens.
What the writes do to the chips is chipstate.py's.

    file ──gunzip──> header ──> command stream ──> VgmLog
                     clocks     0x52/0x53  YM2612 write       writes  [VgmWrite(sample, op, port, reg, value)]
                     loop       0x50       SN76489 byte       pcm     the type-0 data block (the DAC bank)
                     GD3        0x8n       DAC byte + wait    loop_sample, end_sample, tags
                                0xE0       PCM bank seek
                                0x61..0x7n waits

A 0x8n command writes the next PCM bank byte to YM2612 register 0x2A, so it is logged as that
write: the DAC stream is ordinary FM writes, the bank pointer is the reader's.

retimed: the same file with every wait scaled (a rip whose timer ran slow, put on its driver's
clock: core.audit.rip_scale), each command as it was; command_span is both directions' one
reading of a command's length and wait.
"""

from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import NamedTuple

VGM_SAMPLE_RATE = 44_100
VGM_SUFFIXES = (".vgm", ".vgz")

_MAGIC = b"Vgm "
_GZIP_MAGIC = b"\x1f\x8b"
_GD3_MAGIC = b"Gd3 "

# Header fields (VGM spec 1.71): byte offsets.  Offsets marked "rel" count from their own position.
_H_EOF = 0x04               # rel
_H_VERSION = 0x08
_H_PSG_CLOCK = 0x0C
_H_GD3 = 0x14               # rel
_H_TOTAL_SAMPLES = 0x18
_H_LOOP = 0x1C              # rel
_H_LOOP_SAMPLES = 0x20
_H_RATE = 0x24
_H_FM_CLOCK = 0x2C          # YM2612, v1.10+
_H_DATA = 0x34              # rel, v1.50+
_LEGACY_DATA_START = 0x40
_V150 = 0x150
_CLOCK_MASK = 0x3FFF_FFFF   # bits 30-31 are flags (dual chip, T6W28)
_BLOCK_SIZE_MASK = 0x7FFF_FFFF

# Commands this reader decodes
_CMD_PSG = 0x50
_CMD_FM_PORT0 = 0x52
_CMD_FM_PORT1 = 0x53
_CMD_WAIT = 0x61
_CMD_WAIT_NTSC = 0x62
_CMD_WAIT_PAL = 0x63
_CMD_END = 0x66
_CMD_DATA_BLOCK = 0x67
_CMD_PCM_SEEK = 0xE0
_NTSC_FRAME = 735
_PAL_FRAME = 882
_PCM_BLOCK_TYPE = 0x00      # YM2612 PCM data, what 0x8n and 0xE0 read
_DAC_WRITE = 0x80           # 0x8n: the DAC byte, then n samples' wait
_LOW_NIBBLE = 0x0F
_SHORT_WAIT = 0x70          # 0x7n: n + 1 samples
_SHORT_WAITS = 16
_LONGEST_WAIT = 0xFFFF      # 0x61's word
_DAC_STREAMS = range(0x90, 0x96)    # stream control: its rates are Hz, not waits (not retimed)
_DAC_REG = 0x2A
_DAC_SILENCE = 0x80         # a byte read past the bank's end (a broken rip)

# Commands for other chips and the DAC stream control: total length in bytes (command included)
_SKIP_LENGTHS = {
    0x4F: 2, 0x68: 12,
    0x90: 5, 0x91: 5, 0x92: 6, 0x93: 11, 0x94: 2, 0x95: 5,
    **{c: 2 for c in range(0x30, 0x40)},
    **{c: 3 for c in range(0x40, 0x4F)},
    **{c: 3 for c in range(0x51, 0x60) if c not in (_CMD_FM_PORT0, _CMD_FM_PORT1)},
    **{c: 3 for c in range(0xA0, 0xC0)},
    **{c: 4 for c in range(0xC0, 0xE0)},
    **{c: 5 for c in range(0xE1, 0x100)},
}

_GD3_FIELDS = ("track", "track_jp", "game", "game_jp", "system", "system_jp",
               "author", "author_jp", "date", "ripper", "notes")


class VgmError(ValueError):
    """Not a VGM file, or one this reader cannot follow."""


class VgmOp(IntEnum):
    """What a write addresses."""

    FM = 0          # YM2612 register `reg` on `port` (0: FM1-3 and globals, 1: FM4-6)
    PSG = 1         # SN76489 byte `value`
    PCM_SEEK = 2    # the PCM bank pointer moves to `value` (the start of a DAC sample)


class VgmWrite(NamedTuple):
    sample: int     # VGM samples (44 100 Hz) from the log's start
    op: VgmOp
    port: int
    reg: int
    value: int


@dataclass(frozen=True)
class VgmHeader:
    version: int
    fm_clock: int           # 0: no YM2612
    psg_clock: int          # 0: no SN76489
    rate: int               # the recording's frame rate (60 NTSC, 50 PAL); 0 when not given
    total_samples: int
    loop_samples: int       # 0: the log does not loop


@dataclass
class VgmLog:
    header: VgmHeader
    writes: list[VgmWrite] = field(default_factory=list)
    pcm: bytes = b""                    # the YM2612 PCM bank (type-0 data blocks, concatenated)
    end_sample: int = 0
    loop_sample: int | None = None      # where the loop starts; None: no loop
    tags: dict[str, str] = field(default_factory=dict)   # GD3: track, game, author, ...

    @property
    def seconds(self) -> float:
        return self.end_sample / VGM_SAMPLE_RATE


def is_vgm_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VGM_SUFFIXES


def vgm_bytes(path: str | Path) -> bytes:
    """A .vgm or .vgz file's VGM bytes (gunzipped when compressed, whatever the suffix)."""
    return _gunzipped(Path(path).read_bytes())


def command_span(data: bytes, pos: int) -> tuple[int, int]:
    """(bytes, samples waited) of the command at `pos`."""
    cmd = data[pos]
    if cmd in (_CMD_FM_PORT0, _CMD_FM_PORT1):
        return _spanned(data, pos, 3), 0
    if cmd == _CMD_PSG:
        return _spanned(data, pos, 2), 0
    if cmd >> 4 == _DAC_WRITE >> 4:
        return 1, cmd & _LOW_NIBBLE
    if cmd == _CMD_PCM_SEEK:
        return _spanned(data, pos, 5), 0
    if cmd >> 4 == _SHORT_WAIT >> 4:
        return 1, (cmd & _LOW_NIBBLE) + 1
    if cmd == _CMD_WAIT:
        return _spanned(data, pos, 3), struct.unpack_from("<H", data, pos + 1)[0]
    if cmd in (_CMD_WAIT_NTSC, _CMD_WAIT_PAL):
        return 1, _NTSC_FRAME if cmd == _CMD_WAIT_NTSC else _PAL_FRAME
    if cmd == _CMD_DATA_BLOCK:
        _spanned(data, pos, 7)
        return _spanned(data, pos, 7 + (_u32(data, pos + 3) & _BLOCK_SIZE_MASK)), 0
    if cmd in _SKIP_LENGTHS:
        return _spanned(data, pos, _SKIP_LENGTHS[cmd]), 0
    raise VgmError(f"unknown VGM command {cmd:#04x} at {pos:#x}")


def _spanned(data: bytes, pos: int, length: int) -> int:
    if pos + length > len(data):
        raise VgmError(f"command {data[pos]:#04x} at {pos:#x} runs past the end of the file")
    return length


def retimed(raw: bytes, scale: float) -> bytes:
    """The VGM (or VGZ) `raw` with every wait scaled by `scale`, uncompressed: each command as it
    was, the waits re-emitted to reach the scaled time (a 0x8n's folded back into it where it
    fits), the header's total, loop and GD3 fields moved with them."""
    data = _gunzipped(raw)
    if data[:4] != _MAGIC:
        raise VgmError("not a VGM file (bad magic bytes)")
    start = _data_start(data, _u32(data, _H_VERSION))
    loop_at = _rel_offset(data, _H_LOOP)
    out = bytearray(data[:start])
    old = new = 0                               # samples: the original's, the retimed's
    loop_pos = loop_sample = None

    pos = start
    while pos < len(data) and data[pos] != _CMD_END:
        if pos == loop_at:
            loop_pos, loop_sample = len(out), new
        cmd = data[pos]
        if cmd in _DAC_STREAMS:
            raise VgmError(f"DAC stream command {cmd:#04x} at {pos:#x}: its rate is not a wait, not retimed")
        length, wait = command_span(data, pos)
        if cmd >> 4 == _DAC_WRITE >> 4:
            out.append(_DAC_WRITE)
        elif not wait:
            out += data[pos:pos + length]
        old += wait
        new = _wait_to(out, new, round(old * scale), folds=cmd >> 4 == _DAC_WRITE >> 4)
        pos += length

    # The end, and what follows it (the GD3 tag)
    gd3 = _rel_offset(data, _H_GD3)
    out.append(_CMD_END)
    tail_at = len(out)
    out += data[pos + 1:] if pos < len(data) else b""
    _put_u32(out, _H_TOTAL_SAMPLES, new)
    _put_u32(out, _H_EOF, len(out) - _H_EOF)
    if gd3 is not None:
        _put_u32(out, _H_GD3, tail_at + (gd3 - (pos + 1)) - _H_GD3)
    if loop_pos is not None and loop_sample is not None:
        _put_u32(out, _H_LOOP, loop_pos - _H_LOOP)
        _put_u32(out, _H_LOOP_SAMPLES, new - loop_sample)
    return bytes(out)


def _wait_to(out: bytearray, now: int, target: int, folds: bool) -> int:
    """Waits from `now` to `target` appended (`folds`: the last byte a 0x80 that can carry up to 15);
    returns `target`."""
    left = target - now
    if folds and 0 < left <= _LOW_NIBBLE:
        out[-1] = _DAC_WRITE | left
        return target
    while left > 0:
        step = min(left, _LONGEST_WAIT)
        if step <= _SHORT_WAITS:
            out.append(_SHORT_WAIT | (step - 1))
        else:
            out += bytes([_CMD_WAIT]) + struct.pack("<H", step)
        left -= step
    return target


def _put_u32(out: bytearray, at: int, value: int) -> None:
    struct.pack_into("<I", out, at, value)


def _gunzipped(raw: bytes) -> bytes:
    return gzip.decompress(raw) if raw[:2] == _GZIP_MAGIC else raw


def read_vgm(path: str | Path) -> VgmLog:
    return decode_vgm(vgm_bytes(path))


def decode_vgm(data: bytes) -> VgmLog:
    """A .vgm or .vgz file's bytes -> its header, writes and data blocks."""
    data = _gunzipped(data)
    if data[:4] != _MAGIC:
        raise VgmError("not a VGM file (bad magic bytes)")
    return _Decoder(data).run()


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0] if offset + 4 <= len(data) else 0


def _header(data: bytes) -> VgmHeader:
    version = _u32(data, _H_VERSION)
    return VgmHeader(
        version=version,
        fm_clock=_u32(data, _H_FM_CLOCK) & _CLOCK_MASK if version >= 0x110 else 0,
        psg_clock=_u32(data, _H_PSG_CLOCK) & _CLOCK_MASK,
        rate=_u32(data, _H_RATE) if version >= 0x101 else 0,
        total_samples=_u32(data, _H_TOTAL_SAMPLES),
        loop_samples=_u32(data, _H_LOOP_SAMPLES),
    )


def _rel_offset(data: bytes, field_at: int) -> int | None:
    """A header offset counted from its own field; None when 0 (absent)."""
    rel = _u32(data, field_at)
    return field_at + rel if rel else None


def _data_start(data: bytes, version: int) -> int:
    if version < _V150:
        return _LEGACY_DATA_START
    return _rel_offset(data, _H_DATA) or _LEGACY_DATA_START


def _gd3(data: bytes) -> dict[str, str]:
    """The GD3 tag block's strings (UTF-16LE, each NUL-terminated), by field name."""
    at = _rel_offset(data, _H_GD3)
    if at is None or data[at:at + 4] != _GD3_MAGIC:
        return {}
    length = _u32(data, at + 8)
    text = data[at + 12:at + 12 + length].decode("utf-16-le", errors="replace")
    return {k: v for k, v in zip(_GD3_FIELDS, text.split("\0"), strict=False) if v}


class _Decoder:
    """One pass over the command stream."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._log = VgmLog(_header(data), tags=_gd3(data))
        self._loop_at = _rel_offset(data, _H_LOOP)
        self._sample = 0
        self._pcm = bytearray()
        self._pcm_pos = 0

    def run(self) -> VgmLog:
        data, log = self._data, self._log
        pos = _data_start(data, log.header.version)
        while pos < len(data):
            if pos == self._loop_at:
                log.loop_sample = self._sample
            if data[pos] == _CMD_END:
                break
            length, wait = command_span(data, pos)
            self._command(pos)
            self._sample += wait
            pos += length

        log.pcm = bytes(self._pcm)
        log.end_sample = self._sample
        return log

    def _write(self, op: VgmOp, port: int, reg: int, value: int) -> None:
        self._log.writes.append(VgmWrite(self._sample, op, port, reg, value))

    def _command(self, pos: int) -> None:
        """What the command at `pos` writes, at the current sample (its wait: command_span's)."""
        data = self._data
        cmd = data[pos]
        if cmd in (_CMD_FM_PORT0, _CMD_FM_PORT1):
            self._write(VgmOp.FM, cmd - _CMD_FM_PORT0, data[pos + 1], data[pos + 2])
        elif cmd == _CMD_PSG:
            self._write(VgmOp.PSG, 0, 0, data[pos + 1])
        elif cmd >> 4 == _DAC_WRITE >> 4:
            # The DAC: the bank's next byte to 0x2A, then the wait
            byte = self._pcm[self._pcm_pos] if self._pcm_pos < len(self._pcm) else _DAC_SILENCE
            self._write(VgmOp.FM, 0, _DAC_REG, byte)
            self._pcm_pos += 1
        elif cmd == _CMD_PCM_SEEK:
            self._pcm_pos = _u32(data, pos + 1)
            self._write(VgmOp.PCM_SEEK, 0, 0, self._pcm_pos)
        elif cmd == _CMD_DATA_BLOCK:
            self._data_block(pos)

    def _data_block(self, pos: int) -> None:
        """0x67 0x66 tt ssssssss data: type-0 blocks append to the PCM bank."""
        size = _u32(self._data, pos + 3) & _BLOCK_SIZE_MASK
        if self._data[pos + 2] == _PCM_BLOCK_TYPE:
            self._pcm += self._data[pos + 7:pos + 7 + size]
