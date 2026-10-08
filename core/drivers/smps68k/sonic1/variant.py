"""Sonic 1's driver: SMPS 68k Type 1b, modified (s1.sounddriver.asm coordflagLookup), and the data
fixes its disassembly's FixMusicAndSFXDataBugs makes, as byte edits to rev01."""

from __future__ import annotations

from core.rom.fixes import RomFix
from core.rom.flags import RETURN, STOP, EnvelopeCommand, drop, effect, every_kind
from core.rom.variant import SmpsVariant
from core.smps import SMPS_DAC_NAMES, CoordFlag, SmpsDriver

from ..common import FLAGS_68K, HEADER_68K, VOICE_68K
from ..locate import locate_68k
from ..memory import Relative68kMemory
from .dac import sonic1_dac

SONIC1_REV01_SHA1 = "1f1e480f768237eb0c0e725b622b0d791f47a7a9"

_REV01_FIXES = (
    RomFix(0x754BA, bytes.fromhex("8006C1030306 80B524"), bytes.fromhex("8006B5030306 80A924"),
           "Marble Zone PSG3: nE5 nE5 nE5 / nE4 an octave lower (off the PSG table as shipped)"),
    RomFix(0x781BD, bytes.fromhex("808080E60C"), b"",
           "Credits PSG2: three late rests and an FM-only smpsAlterVol $0C that mutes the passage"),
    RomFix(0x791A0, bytes.fromhex("90"), bytes.fromhex("10"),
           "SndBC Teleport FM5: transposition $90 -> $10"),
)

SONIC1 = SmpsVariant(
    name=SmpsDriver.SONIC1,
    memory=Relative68kMemory,
    locate=locate_68k,
    flags=every_kind({
        **FLAGS_68K,
        0xE3: RETURN,
        0xE4: STOP,                                   # smpsFade: the 1-Up jingle restores the song
        0xE5: effect(CoordFlag.CHAN_TEMPO_DIV),
        0xE9: effect(CoordFlag.CHANGE_TRANSPOSITION),
        0xEA: effect(CoordFlag.SET_TEMPO_MOD),
        0xEB: effect(CoordFlag.SET_TEMPO_DIV),
        0xED: drop("smpsClearPush"),
        0xEE: STOP,                                   # smpsStopSpecial: FM4 handed back to the music
        0xF9: drop("smpsMaxRelRate"),
    }),
    envelope_commands={0x80: EnvelopeCommand.HOLD},
    header=HEADER_68K,
    voice_layout=VOICE_68K,
    dac_names={v: k for k, v in SMPS_DAC_NAMES.items()},
    dac=sonic1_dac,
    known_roms={SONIC1_REV01_SHA1: _REV01_FIXES},
)
