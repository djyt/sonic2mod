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
    raw = Path(path).read_bytes()
    return gzip.decompress(raw) if raw[:2] == _GZIP_MAGIC else raw


def read_vgm(path: str | Path) -> VgmLog:
    return decode_vgm(vgm_bytes(path))


def decode_vgm(data: bytes) -> VgmLog:
    """A VGM file's bytes -> its header, writes and data blocks."""
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
            pos = self._command(pos)

        log.pcm = bytes(self._pcm)
        log.end_sample = self._sample
        return log

    def _need(self, pos: int, length: int) -> None:
        if pos + length > len(self._data):
            raise VgmError(f"command {self._data[pos]:#04x} at {pos:#x} runs past the end of the file")

    def _write(self, op: VgmOp, port: int, reg: int, value: int) -> None:
        self._log.writes.append(VgmWrite(self._sample, op, port, reg, value))

    def _command(self, pos: int) -> int:
        """Decode the command at `pos`; returns the next command's offset."""
        data = self._data
        cmd = data[pos]

        # Chip writes
        if cmd in (_CMD_FM_PORT0, _CMD_FM_PORT1):
            self._need(pos, 3)
            self._write(VgmOp.FM, cmd - _CMD_FM_PORT0, data[pos + 1], data[pos + 2])
            return pos + 3
        if cmd == _CMD_PSG:
            self._need(pos, 2)
            self._write(VgmOp.PSG, 0, 0, data[pos + 1])
            return pos + 2

        # The DAC: 0x8n writes the bank's next byte to 0x2A, then waits n
        if cmd >> 4 == 0x8:
            byte = self._pcm[self._pcm_pos] if self._pcm_pos < len(self._pcm) else _DAC_SILENCE
            self._write(VgmOp.FM, 0, _DAC_REG, byte)
            self._pcm_pos += 1
            self._sample += cmd & 0x0F
            return pos + 1
        if cmd == _CMD_PCM_SEEK:
            self._need(pos, 5)
            self._pcm_pos = _u32(data, pos + 1)
            self._write(VgmOp.PCM_SEEK, 0, 0, self._pcm_pos)
            return pos + 5

        # Waits
        if cmd >> 4 == 0x7:
            self._sample += (cmd & 0x0F) + 1
            return pos + 1
        if cmd == _CMD_WAIT:
            self._need(pos, 3)
            self._sample += struct.unpack_from("<H", data, pos + 1)[0]
            return pos + 3
        if cmd in (_CMD_WAIT_NTSC, _CMD_WAIT_PAL):
            self._sample += _NTSC_FRAME if cmd == _CMD_WAIT_NTSC else _PAL_FRAME
            return pos + 1

        if cmd == _CMD_DATA_BLOCK:
            return self._data_block(pos)
        if cmd in _SKIP_LENGTHS:
            self._need(pos, _SKIP_LENGTHS[cmd])
            return pos + _SKIP_LENGTHS[cmd]
        raise VgmError(f"unknown VGM command {cmd:#04x} at {pos:#x}")

    def _data_block(self, pos: int) -> int:
        """0x67 0x66 tt ssssssss data: type-0 blocks append to the PCM bank."""
        self._need(pos, 7)
        block_type = self._data[pos + 2]
        size = _u32(self._data, pos + 3) & _BLOCK_SIZE_MASK
        self._need(pos, 7 + size)
        if block_type == _PCM_BLOCK_TYPE:
            self._pcm += self._data[pos + 7:pos + 7 + size]
        return pos + 7 + size
