"""The VGM reader, chip state and frame log (core/vgm/) on hand-built logs.

    python -m pytest tests -q
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from vgm_build import FRAME as _FRAME
from vgm_build import HEADER_BYTES as _HEADER_BYTES
from vgm_build import fm as _fm
from vgm_build import fm_freq as _fm_freq
from vgm_build import psg as _psg
from vgm_build import vgm as _vgm
from vgm_build import wait as _wait

from core.vgm import (
    ChangeKind,
    ChipState,
    VgmError,
    VgmOp,
    decode_vgm,
    frame_log,
    load_frames,
    note_starts,
    pitch_segments,
)


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
        self.assertEqual([s.offset for s in dac.starts], [0])
        self.assertEqual((dac.writes, dac.since_start), (6, 3))
        # 3 samples apart before the seek, 2 after; the 400-sample silence between is no gap
        self.assertEqual(dac.gaps, ((2, 2), (3, 2)))

    def test_bytes_resuming_after_a_pause_start_a_sample(self):
        # The bank holds the next sample where the last ended: the ripper writes no seek
        bank = bytes(8)
        block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
        seek = b"\xE0" + struct.pack("<I", 2)
        fl = frame_log(decode_vgm(_vgm(block + seek + b"\x83\x83" + _wait(400) + b"\x83\x82")))
        dac = fl.frames[0].dac
        self.assertEqual([s.offset for s in dac.starts], [2, 4])
        self.assertEqual(dac.since_start, 2)

    def test_a_late_seek_belongs_to_the_burst_before_it(self):
        # Two bursts a frame apart; the Z80 starts a sample 600 samples after the first, in the
        # second's window but before its burst
        burst = _fm(0, 0x28, 0xF0)
        seek = b"\xE0" + struct.pack("<I", 0)
        fl = frame_log(decode_vgm(_vgm(_wait(300) + burst + _wait(600) + seek + b"\x80" + _wait(_FRAME - 600) + burst)))
        first = next(f.index for f in fl.frames if f.fm[0].keys)
        sample = next(f.dac.starts[0].sample for f in fl.frames if f.dac.starts)
        self.assertEqual(fl.frame_of(sample), first + 1)
        self.assertEqual(fl.burst_frame(sample), first)

    def test_the_bank_comes_with_the_frames(self):
        bank = bytes(range(8))
        block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
        self.assertEqual(frame_log(decode_vgm(_vgm(block + b"\x81"))).pcm, bank)


class FrameCache(unittest.TestCase):
    """A rip's frame log is kept on disk under a hash of the file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _rip(self, name: str, fnum: int) -> Path:
        path = self.dir / name
        path.write_bytes(_vgm(_fm_freq(0, fnum, 4) + _fm(0, 0x28, _ON) + _wait(_FRAME)))
        return path

    def test_a_cached_log_is_the_log(self):
        rip = self._rip("a.vgm", _A4[0])
        first = load_frames(rip, self.dir / "cache")
        self.assertEqual(load_frames(rip, self.dir / "cache"), first)
        self.assertEqual(first, frame_log(decode_vgm(rip.read_bytes())))
        self.assertEqual(len(list((self.dir / "cache").rglob("*.frames"))), 1)

    def test_another_file_is_another_log(self):
        a = load_frames(self._rip("a.vgm", _A4[0]), self.dir / "cache")
        b = load_frames(self._rip("b.vgm", _B4[0]), self.dir / "cache")
        self.assertNotEqual(a.frames[0].fm[0].fnum, b.frames[0].fm[0].fnum)

    def test_no_directory_reads_the_rip(self):
        rip = self._rip("a.vgm", _A4[0])
        self.assertEqual(load_frames(rip), frame_log(decode_vgm(rip.read_bytes())))


_A4 = (1083, 4)          # 440 Hz
_B4 = (1216, 4)          # two semitones up
_ON, _OFF = 0xF0, 0x00   # key register: all slots of FM1 on / off


class Notes(unittest.TestCase):
    def _starts(self, commands: bytes, **kw):
        return note_starts(decode_vgm(_vgm(commands)), **kw)

    def test_fm_key_on_starts_a_note_and_a_tie_does_not(self):
        # The driver keys on again under smpsNoAttack (it only skips the key-off): a tie
        tie = _fm_freq(0, *_A4) + _fm(0, 0x28, _ON) + b"\x62" + _fm(0, 0x28, _ON)
        starts = self._starts(tie)
        self.assertEqual([(n.channel, n.data, n.block) for n in starts], [("FM1", 1083, 4)])
        self.assertAlmostEqual(starts[0].hz, 440.0, delta=0.5)

    def test_fm_legato_and_retrigger_are_notes(self):
        legato = _fm_freq(0, *_A4) + _fm(0, 0x28, _ON) + b"\x62" + _fm_freq(0, *_B4) + _fm(0, 0x28, _ON)
        retrigger = _fm_freq(0, *_A4) + _fm(0, 0x28, _ON) + b"\x62" + _fm(0, 0x28, _OFF) + _fm(0, 0x28, _ON)
        self.assertEqual([n.data for n in self._starts(legato)], [1083, 1216])
        self.assertEqual([n.sample for n in self._starts(retrigger)], [0, 735])

    def test_fm_key_on_with_no_frequency_is_no_note(self):
        self.assertEqual(self._starts(_fm(0, 0x28, _ON)), [])

    def test_fm_level_is_the_carriers_summed(self):
        # Algorithm 7: four carriers at TL 0 -> 4.0
        commands = _fm(0, 0xB0, 0x07) + _fm_freq(0, *_A4) + _fm(0, 0x28, _ON)
        self.assertAlmostEqual(self._starts(commands)[0].gain, 4.0)

    def test_psg_notes_start_audible_and_leave_vibrato_alone(self):
        # PSG1 period 254, audible; 2 steps of vibrato (~14 cents) is no note, a whole tone is
        commands = (_psg(0x8E) + _psg(0x0F) + _psg(0x90) + b"\x62" + _psg(0x80) + _psg(0x10)
                    + b"\x62" + _psg(0x82) + _psg(0x0E))
        starts = self._starts(commands)
        self.assertEqual([(n.channel, n.data) for n in starts], [("PSG1", 0xFE), ("PSG1", 0xE2)])
        self.assertEqual([n.data for n in self._starts(commands, mod_cents=0)], [0xFE, 0x100, 0xE2])

    def test_noise_and_dac(self):
        bank = bytes(4)
        block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
        commands = block + _psg(0xC5) + _psg(0x00) + _psg(0xE7) + _psg(0xF2) + b"\xE0" + struct.pack("<I", 2)
        starts = self._starts(commands)
        self.assertEqual([(n.channel, n.data, n.tone2) for n in starts], [("NOISE", 7, 5), ("DAC", 2, 0)])

    def test_pitch_segments_follow_key_and_frequency(self):
        commands = _fm_freq(0, *_A4) + _fm(0, 0x28, _ON) + b"\x62" + _fm(0, 0x28, _OFF)
        segments, end = pitch_segments(decode_vgm(_vgm(commands)))
        fm1 = segments["FM1"]
        self.assertEqual([hz is None for _, hz in fm1], [True, False, True])
        self.assertAlmostEqual(end, 735 / 44100)


if __name__ == "__main__":
    unittest.main()
