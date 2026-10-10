"""OutRun's driver (core/drivers/smpsz80/outrun/): its flags on hand-built bytes, then the ROM's banks,
tables and every song when it is present.

    python -m pytest tests/core/drivers/smpsz80/outrun/test_variant.py -q
"""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers import detect_variant, read_rom_code, read_rom_song
from core.drivers.smpsz80.memory import BankedZ80Memory
from core.drivers.smpsz80.outrun import OUTRUN
from core.drivers.smpsz80.outrun.dac import outrun_dac
from core.drivers.smpsz80.outrun.locate import driver_tables, locate_outrun
from core.rom import RomError, RomImage
from core.smps import ChannelType, OpKind, Pan, SetVoice, TimerA, update_hz
from core.smps.pitch_envelope import Restart
from tests.roms import OUTRUN_ROM, needs_outrun

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_BANK = 0x8000


def _read(code: bytes, kind: ChannelType = ChannelType.FM):
    """The instruction `code` starts with, read by a `kind` track."""
    rom = bytearray(_HEADER.ljust(2 * _BANK, b"\0"))
    rom[_BANK:_BANK + len(code)] = code
    memory = BankedZ80Memory(RomImage(bytes(rom)), _BANK)
    return OUTRUN.grammar(memory, _BANK, OUTRUN, kind)


class Flags(unittest.TestCase):
    def test_a_voice_is_the_songs_own_or_refused(self):
        one = _read(bytes([0xEF, 0x03]))
        self.assertEqual((one.ops[0].effect, one.length), (SetVoice(3), 2))
        with self.assertRaisesRegex(RomError, "another song's table"):
            _read(bytes([0xEF, 0x83, 0x81]))

    def test_a_psg_track_steps_over_a_voice_one_operand_more_from_another_song(self):
        self.assertEqual([_read(bytes([0xEF, n, 0x81]), ChannelType.PSG).length for n in (0x03, 0x83)], [2, 3])

    def test_each_kind_ignores_the_others_volume(self):
        self.assertEqual(_read(bytes([0xE6, 0x02]), ChannelType.PSG).dropped, "FM volume on a PSG track (ignored)")
        self.assertEqual(_read(bytes([0xEC, 0x02])).dropped, "PSG volume on an FM track (ignored)")
        self.assertEqual(_read(bytes([0xEC, 0x02]), ChannelType.PSG).ops[0].kind, OpKind.EFFECT)

    def test_an_fm_track_keeps_its_detuned_word_in_the_octave(self):
        wrap = OUTRUN.rules.track(ChannelType.FM).octave_wrap
        assert wrap is not None
        self.assertEqual(wrap.wrapped(0x2A80), 0x2505)      # fnum $280: down a block, fnum + $285 ($85's FM4)
        self.assertEqual(wrap.wrapped(0x2509), 0x2A85)      # fnum $509: up a block, fnum - $284
        self.assertEqual(wrap.wrapped(0x2A84), 0x2A84)

    def test_a_sound_played_is_dropped_the_other_meta_commands_refused(self):
        one = _read(bytes([0xFF, 0x02, 0x97]))
        self.assertEqual((one.ops, one.length), ((), 3))
        for sub in (0x01, 0x04, 0x06):
            with self.assertRaisesRegex(RomError, f"\\$FF \\${sub:02X}"):
                _read(bytes([0xFF, sub, 0, 0, 0, 0]))


@needs_outrun
class OutRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(OUTRUN_ROM)
        cls.index = locate_outrun(cls.rom)

    def test_detected(self):
        self.assertIs(detect_variant(self.rom), OUTRUN)

    def test_the_banks_from_the_68ks_ram_writes(self):
        tables = driver_tables(self.rom)
        self.assertEqual((tables.sound_bank, tables.dac_bank, tables.music_index), (0xF0000, 0xF8000, 0xF0479))

    def test_five_songs_and_their_envelopes(self):
        self.assertEqual(self.index.music, {0x81: 0xF07F5, 0x82: 0xF1FC9, 0x83: 0xF38CA, 0x84: 0xF49AF, 0x85: 0xF4ECA})
        self.assertEqual(len(self.index.envelopes), 17)          # the 18th word names none

    def test_clocked_by_timer_a_at_126_hz(self):
        rules = OUTRUN.rules_from_rom(self.rom, OUTRUN.rules)
        self.assertEqual(rules.update_clock, TimerA(0x25A))
        self.assertAlmostEqual(update_hz(rules, "ntsc"), 126.225, places=3)

    def test_pitch_envelopes_two_and_five_are_detunes(self):
        envelopes = OUTRUN.rules_from_rom(self.rom, OUTRUN.rules).pitch_envelopes
        self.assertEqual((envelopes[2].steps, envelopes[5].steps), ((5, Restart()), (2, Restart())))

    def test_eleven_samples_at_their_loops_rates(self):
        samples = {s.sound: s for s in outrun_dac(self.rom, OUTRUN.rules.dac_names)}
        self.assertEqual(sorted(samples), [*range(0x81, 0x87), 0x88, *range(0x8A, 0x8E)])     # $87, $89 empty
        self.assertEqual((len(samples[0x81].pcm), samples[0x81].pitch), (1700, 18))         # DPCM: 2 a byte
        self.assertAlmostEqual(samples[0x81].rate, 3_579_545 / (160 + 13 * 18))
        self.assertAlmostEqual(samples[0x8B].rate, 3_579_545 / (171 + 13 * 11))              # raw PCM

    def test_a_sample_pans_the_drum_track_as_it_starts(self):
        song = read_rom_song(self.rom, 0x83)
        dac = next(c for c in song.channels if c.header.channel_type is ChannelType.DAC)
        first = next(i for i, e in enumerate(dac.events) if e.is_note and e.note.note_value == 0x83)
        self.assertEqual(dac.events[first - 1].effect, Pan(0x40))                            # $83: right only

    def test_every_song_reads(self):
        dropped: Counter[str] = Counter()
        for sound in self.index.music:
            dropped += read_rom_code(self.rom, sound, self.index).dropped
        self.assertEqual(dropped, {"$FF $02: play sound $97 (each song converts alone)": 1,
                                   "PSG voice on an FM track (ignored)": 3})


if __name__ == "__main__":
    unittest.main()
