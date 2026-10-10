"""SMPS Z80 (core/drivers/smpsz80/): banked pointers, the driver the copy loop loads, the sound
bank and its indexes found by their shape.

    python -m pytest tests/core/drivers/smpsz80/test_smpsz80.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from core.drivers.reference import FM_FREQUENCIES
from core.drivers.smpsz80.memory import BankedZ80Memory
from core.drivers.smpsz80.program import fm_table
from core.drivers.smpsz80.type0fm.locate import locate_type0, sound_bank
from core.rom import RomError, RomImage
from core.rom.z80 import z80_ram

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")


def _rom(song: bytes) -> RomImage:
    return RomImage(_HEADER + song)


class SmpsZ80(unittest.TestCase):
    """The Z80 family on a hand-built ROM: a copy loop loads the driver, a bank holds the sounds."""

    _BANK = 0x8000
    _DRIVER = 0x400              # the driver's bytes in the ROM, copied to Z80 $0000
    _FM_TABLE = 0x10             # its FM table's Z80 address

    def _rom(self, music_count: int = 3) -> RomImage:
        rom = bytearray(_HEADER.ljust(2 * self._BANK, b"\0"))

        # lea (z80_ram).l,a6 / lea (driver).l,a5 / move.w #n-1,d0 / move.b (a5)+,(a6)+ / dbra
        driver = bytearray(0x80)
        octaves = b"".join(w.to_bytes(2, "little") for w in FM_FREQUENCIES[:24])      # two octaves
        driver[self._FM_TABLE:self._FM_TABLE + len(octaves)] = octaves
        rom[self._DRIVER:self._DRIVER + len(driver)] = driver
        copy = (bytes.fromhex("4DF900A00000 4BF9".replace(" ", "")) + self._DRIVER.to_bytes(4, "big")
                + bytes.fromhex("303C") + (len(driver) - 1).to_bytes(2, "big") + bytes.fromhex("1CDD51C8FFFC"))
        rom[0x300:0x300 + len(copy)] = copy

        # The sound header at Z80 $8000: +4 the music index, +6 the SFX index; each sound a header
        # and a lone smpsStop, three SFX on FM6
        def z80(at: int) -> bytes:
            return (0x8000 + at).to_bytes(2, "little")

        music_at, sfx_at, sounds_at = 0x10, 0x20, 0x40
        bank = bytearray(0x100)
        bank[4:8] = z80(music_at) + z80(sfx_at)
        at = sounds_at
        for i in range(music_count):
            bank[music_at + 2 * i:music_at + 2 * i + 2] = z80(at)
            bank[at:at + 11] = bytes([0, 0, 1, 0, 1, 0]) + z80(at + 10) + bytes([0, 0, 0xF2])
            at += 11
        for i in range(3):
            bank[sfx_at + 2 * i:sfx_at + 2 * i + 2] = z80(at)
            bank[at:at + 11] = bytes([0, 0, 1, 1, 0x80, 0x06]) + z80(at + 10) + bytes([0, 0, 0xF2])
            at += 11
        rom[self._BANK:self._BANK + len(bank)] = bank
        return RomImage(bytes(rom))

    def test_pointers_are_little_endian_z80_addresses_in_the_bank(self):
        memory = BankedZ80Memory(self._rom(), self._BANK)
        self.assertEqual(memory.word(self._BANK + 4), 0x8010)
        self.assertEqual(memory.header_pointer(self._BANK, self._BANK + 4), self._BANK + 0x10)
        self.assertFalse(memory.contains(self._BANK - 1))
        with self.assertRaisesRegex(RomError, "outside the bank"):
            memory.byte(self._BANK - 1)

    def test_a_fix_reads_in_the_banks_own_memory(self):
        # A data fix's bytes spliced in: little-endian, the bank's pointers unchanged
        patch = BankedZ80Memory(self._rom(), self._BANK).patched(self._BANK + 0x40, bytes([0x12, 0x34]))
        self.assertEqual(patch.word(self._BANK + 0x40), 0x3412)
        self.assertEqual(patch.header_pointer(self._BANK, self._BANK + 4), self._BANK + 0x10)

    def test_the_driver_is_what_the_copy_loop_loads(self):
        self.assertEqual(fm_table(z80_ram(self._rom())), self._FM_TABLE)

    def test_a_copy_past_z80_ram_is_refused(self):
        # The loop's destination moved to $1FF0: its $80 bytes would run past the 8 KB
        rom = bytearray(self._rom().data)
        rom[0x304:0x306] = (0x1FF0).to_bytes(2, "big")
        with self.assertRaisesRegex(RomError, "past its 8 KB"):
            z80_ram(RomImage(bytes(rom)))

    def test_the_bank_and_its_indexes_are_found_by_their_shape(self):
        rom = self._rom()
        index = locate_type0(rom)
        self.assertEqual(sound_bank(rom), self._BANK)
        self.assertEqual(sorted(index.music), [0x81, 0x82, 0x83])
        self.assertEqual(sorted(index.sfx), [0x90, 0x91, 0x92])
        self.assertEqual(index.music[0x81], self._BANK + 0x40)
        self.assertEqual(index.envelopes, ())

    def test_the_music_index_ends_at_the_sfx_index(self):
        # Eight songs would run into the SFX index at +$20: the index stops there
        index = locate_type0(self._rom(music_count=8))
        self.assertEqual(len(index.music), 8)

    def test_a_68k_rom_has_no_z80_driver_to_find(self):
        with self.assertRaisesRegex(RomError, "Z80"):
            sound_bank(_rom(b"\0" * 0x100))


if __name__ == "__main__":
    unittest.main()
