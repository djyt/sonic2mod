"""The VGM reader, chip state and frame log (core/vgm/) on hand-built logs.

    python -m pytest tests -q
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.vgm import ChangeKind, ChipState, VgmError, VgmOp, decode_vgm, frame_log

_HEADER_BYTES = 0x80
_FRAME = 735


def _vgm(commands: bytes, loop_at: int | None = None, rate: int = 60) -> bytes:
    """A v1.50 VGM: header (MD clocks), then `commands`; `loop_at` = offset into the commands."""
    head = bytearray(_HEADER_BYTES)
    head[0:4] = b"Vgm "
    struct.pack_into("<I", head, 0x08, 0x150)
    struct.pack_into("<I", head, 0x0C, 3_579_545)
    struct.pack_into("<I", head, 0x24, rate)
    struct.pack_into("<I", head, 0x2C, 7_670_453)
    struct.pack_into("<I", head, 0x34, _HEADER_BYTES - 0x34)
    if loop_at is not None:
        struct.pack_into("<I", head, 0x1C, _HEADER_BYTES + loop_at - 0x1C)
    return bytes(head) + commands + b"\x66"


def _fm(port: int, reg: int, value: int) -> bytes:
    return bytes((0x52 + port, reg, value))


def _psg(value: int) -> bytes:
    return bytes((0x50, value))


def _wait(samples: int) -> bytes:
    return b"\x61" + struct.pack("<H", samples)


class Reader(unittest.TestCase):
    def test_writes_are_timed_by_the_waits_before_them(self):
        log = decode_vgm(_vgm(_fm(0, 0x28, 0xF0) + b"\x62" + _psg(0x9F) + b"\x70" + _fm(1, 0xA4, 0x22)))
        self.assertEqual([(w.sample, w.op, w.port, w.reg, w.value) for w in log.writes],
                         [(0, VgmOp.FM, 0, 0x28, 0xF0), (735, VgmOp.PSG, 0, 0, 0x9F), (736, VgmOp.FM, 1, 0xA4, 0x22)])
        self.assertEqual(log.end_sample, 736)
        self.assertEqual(log.header.fm_clock, 7_670_453)

    def test_dac_bytes_come_from_the_bank_and_seek_moves_the_pointer(self):
        bank = bytes((0x10, 0x20, 0x30, 0x40))
        block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
        seek = b"\xE0" + struct.pack("<I", 2)
        log = decode_vgm(_vgm(block + b"\x82\x83" + seek + b"\x81"))
        dac = [(w.sample, w.value) for w in log.writes if w.op is VgmOp.FM]
        self.assertEqual(dac, [(0, 0x10), (2, 0x20), (5, 0x30)])
        self.assertEqual([w.value for w in log.writes if w.op is VgmOp.PCM_SEEK], [2])
        self.assertEqual(log.pcm, bank)

    def test_the_loop_offset_becomes_a_sample(self):
        commands = _wait(100) + _psg(0x9F) + _wait(50)
        log = decode_vgm(_vgm(commands, loop_at=3))
        self.assertEqual(log.loop_sample, 100)

    def test_other_chips_are_skipped_and_unknown_commands_refused(self):
        log = decode_vgm(_vgm(bytes((0x51, 0x01, 0x02, 0xA0, 0x01, 0x02)) + _psg(0x9F)))
        self.assertEqual(len(log.writes), 1)
        with self.assertRaises(VgmError):
            decode_vgm(_vgm(b"\x6F"))
        with self.assertRaises(VgmError):
            decode_vgm(b"RIFF" + bytes(_HEADER_BYTES))


class Chips(unittest.TestCase):
    def _replay(self, commands: bytes):
        log = decode_vgm(_vgm(commands))
        state = ChipState.for_log(log)
        return state, list(state.replay(log))

    def test_fnum_high_byte_waits_for_the_low_byte(self):
        # A4 alone changes nothing; A0 latches the pair (one latch for the chip: Nuked-OPN2's reg_a4)
        state, changes = self._replay(_fm(0, 0xA4, 0x22) + _fm(1, 0xA1, 0x3B))
        self.assertEqual([(c.kind, c.channel) for c in changes], [(ChangeKind.FM_FREQUENCY, 4)])
        self.assertEqual(state.fm_fnum_block(4), (0x23B, 4))
        self.assertEqual(state.fm_fnum_block(1), (0, 0))

    def test_key_register_addresses_six_channels(self):
        state, changes = self._replay(_fm(0, 0x28, 0xF0) + _fm(0, 0x28, 0xF6) + _fm(0, 0x28, 0xF3) + _fm(0, 0x28, 0x02))
        self.assertEqual([(c.channel, c.value) for c in changes], [(0, 0xF), (5, 0xF), (2, 0)])
        self.assertTrue(state.fm_slots(0) and state.fm_slots(5) and not state.fm_slots(2))

    def test_a_period_latch_with_its_data_byte_is_one_change(self):
        # PSG2 period 0x1A5: latch writes 5, data byte the high bits.  Same instant: one change.
        _, together = self._replay(_psg(0xA5) + _psg(0x1A))
        self.assertEqual([(c.kind, c.channel, c.value) for c in together], [(ChangeKind.PSG_TONE, 1, 0x1A5)])
        _, apart = self._replay(_psg(0xA5) + b"\x70" + _psg(0x1A))
        self.assertEqual([c.value for c in apart], [0x005, 0x1A5])

    def test_a_change_carries_the_value_it_replaced(self):
        # PSG1 at 0x105, then 0x1A5 by latch + data byte: the pair's change replaces 0x105, not
        # the half-written 0x105 -> 0x105 the latch alone leaves
        _, changes = self._replay(_psg(0x85) + _psg(0x10) + b"\x70" + _psg(0x85) + _psg(0x1A) + _psg(0x93) + _psg(0x95))
        tones = [(c.value, c.previous) for c in changes if c.kind is ChangeKind.PSG_TONE]
        self.assertEqual(tones, [(0x105, 0x000), (0x1A5, 0x105)])
        volumes = [(c.value, c.previous) for c in changes if c.kind is ChangeKind.PSG_VOLUME]
        self.assertEqual(volumes, [(3, 0xF), (5, 3)])

    def test_volume_and_noise_writes(self):
        state, changes = self._replay(_psg(0xF3) + _psg(0x07) + _psg(0xE7))
        self.assertEqual([(c.kind, c.value) for c in changes],
                         [(ChangeKind.PSG_VOLUME, 0x3), (ChangeKind.PSG_VOLUME, 0x7), (ChangeKind.PSG_NOISE, 7)])
        self.assertEqual(state.psg_attenuation(3), 7)
        self.assertTrue(state.noise_white)
        self.assertEqual(state.noise_rate, 3)

    def test_carrier_tls_follow_the_algorithm(self):
        # Algorithm 4: carriers at register offsets 0x08 and 0x0C (operators 2 and 4)
        commands = _fm(0, 0xB0, 0x04) + b"".join(_fm(0, 0x40 + s, tl) for s, tl in ((0, 1), (4, 2), (8, 3), (12, 4)))
        state, _ = self._replay(commands)
        self.assertEqual(state.fm_carrier_tls(0), (3, 4))


class Frames(unittest.TestCase):
    def test_bursts_land_in_one_frame_each(self):
        # A burst every frame at +600, jittered: key-on, six writes 50 samples apart, key-off
        commands = b""
        now = 0
        for k, jitter in enumerate((0, 9, -6, 3, 0, -3)):
            start = k * _FRAME + 600 + jitter
            body = b"".join(_wait(50) + _fm(0, 0x40, 0) for _ in range(6))
            commands += _wait(start - now) + _fm(0, 0x28, 0xF0) + body + _fm(0, 0x28, 0x00)
            now = start + 300
        fl = frame_log(decode_vgm(_vgm(commands)))
        self.assertEqual(fl.phase, 600)
        keyed = [f.index for f in fl.frames if f.fm[0].keys]
        self.assertEqual(len(keyed), 6)
        self.assertEqual(len(set(keyed)), 6)
        self.assertTrue(all(f.fm[0].keys == (0xF, 0) for f in fl.frames if f.fm[0].keys))

    def test_dac_gaps_restart_at_a_seek(self):
        bank = bytes(8)
        block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
        seek = b"\xE0" + struct.pack("<I", 0)
        fl = frame_log(decode_vgm(_vgm(block + b"\x83\x83\x83" + _wait(400) + seek + b"\x82\x82\x80")))
        dac = fl.frames[0].dac
        self.assertEqual(dac.seeks, (0,))
        self.assertEqual((dac.writes, dac.since_seek), (6, 3))
        # 3 samples apart before the seek, 2 after; the 400-sample silence between is no gap
        self.assertEqual(dac.gaps, ((2, 2), (3, 2)))


if __name__ == "__main__":
    unittest.main()
