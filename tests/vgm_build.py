"""Hand-built VGM logs for the unit tests: commands, and a song's bursts frame by frame."""

from __future__ import annotations

import struct

HEADER_BYTES = 0x80
FRAME = 735                     # samples per NTSC frame
BURST = 300                     # where in its frame the driver's burst starts
KEY_ON, KEY_OFF = 0xF0, 0x00    # key register: all slots of FM1 on / off


def vgm(commands: bytes, loop_at: int | None = None, rate: int = 60) -> bytes:
    """A v1.50 VGM: header (MD clocks), then `commands`; `loop_at` = offset into the commands."""
    head = bytearray(HEADER_BYTES)
    head[0:4] = b"Vgm "
    struct.pack_into("<I", head, 0x08, 0x150)
    struct.pack_into("<I", head, 0x0C, 3_579_545)
    struct.pack_into("<I", head, 0x24, rate)
    struct.pack_into("<I", head, 0x2C, 7_670_453)
    struct.pack_into("<I", head, 0x34, HEADER_BYTES - 0x34)
    if loop_at is not None:
        struct.pack_into("<I", head, 0x1C, HEADER_BYTES + loop_at - 0x1C)
    return bytes(head) + commands + b"\x66"


def fm(port: int, reg: int, value: int) -> bytes:
    return bytes((0x52 + port, reg, value))


def psg(value: int) -> bytes:
    return bytes((0x50, value))


def wait(samples: int) -> bytes:
    return b"\x61" + struct.pack("<H", samples)


def fm_freq(ch: int, fnum: int, block: int) -> bytes:
    """FM1-3: the high byte (latched), then the low byte."""
    return fm(0, 0xA4 + ch, block << 3 | fnum >> 8) + fm(0, 0xA0 + ch, fnum & 0xFF)


def key(ch: int, on: bool) -> bytes:
    """FM1-3 keyed on (all slots) or off."""
    return fm(0, 0x28, (KEY_ON if on else KEY_OFF) | ch)


def bursts(writes: dict[int, bytes], frames: int, loop_frame: int | None = None) -> bytes:
    """A log of `frames` frames, each frame's writes in one burst at BURST: what a driver writes."""
    commands = bytearray()
    loop_at = None
    now = 0
    for index in range(frames):
        if index == loop_frame:
            commands += wait(index * FRAME - now)
            now = index * FRAME
            loop_at = len(commands)
        body = writes.get(index)
        if body:
            start = index * FRAME + BURST
            commands += wait(start - now) + body
            now = start
    commands += wait(frames * FRAME - now)
    return vgm(bytes(commands), loop_at)
