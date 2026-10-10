"""An early SMPS Z80: Space Harrier II.  Golden Axe's ancestor (the note grammar, $EF $F0 $F2
$F6-$F9 $FB $FC $FD, a drum track on FM3), with its own songs (track lists, header.py) and voices
(register lists, voices.py).  No disassembly: its flag table at Z80 $05B7, read with a
disassembler (docs/todo/space_harrier_2.md).

The dispatch steps one byte past a handler's return, so a flag with no operand of its own still
skips one ($F5 on FM).  The drum track reads its bytes as drums (a bitmask, Phase 3 of the plan);
its flags are read here, what they do to the drums is the drum reader's.  It takes $7F for a drum
where SMPS reads a duration (Z80 $0CDC); no song has a $7F.

Read and dropped for now (reported by the reader): the drum track's own flags, the plan's Phase 3.  The follow-on song ($E9) is dropped: each
song converts alone.
"""

from __future__ import annotations

from dataclasses import replace

from core.rom.flags import CALL, JUMP, LOOP, RETURN, STOP, FlagSpec, drop, effect, read, refuse
from core.rom.grammar import Instruction
from core.rom.image import RomError, RomImage
from core.rom.memory import SoundMemory
from core.rom.variant import SmpsVariant
from core.smps import (
    FIRST_NOTE,
    LAST_NOTE,
    ChannelType,
    CoordFlag,
    FmDrum,
    Op,
    OpKind,
    PlaybackRules,
    SmpsSongHeader,
    effect_from_bytes,
)

from ...names import SmpsDriver
from ..program import driver_ram, fm_frequencies, psg_frequencies, psg_read
from .drums import read_sh2_drums
from .envelopes import read_pitch_envelopes
from .header import HEADER_SH2, read_track_list
from .locate import locate_sh2, sh2_memory
from .memory import Sh2Memory
from .voices import is_full_voice, read_sh2_voices, voice_list, voice_registers

_SET_VOICE_LENGTH = 2

# $E0-$E6 and $F3 share one handler: the PSG noise form (the operand ORed with $E0)
_NOISE_FORMS = (*range(0xE0, 0xE7), 0xF3)

# Read by both kinds of track
_CONTROL: dict[int, FlagSpec] = {
    0xE9: drop("follow-on song (each song converts alone)", 1),
    0xF2: STOP,
    0xF6: JUMP,
    0xF7: LOOP,
    0xF8: CALL,
    0xF9: RETURN,
    0xFA: effect(CoordFlag.CHAN_TEMPO_DIV),
    0xFB: effect(CoordFlag.CHANGE_TRANSPOSITION),
    0xFF: effect(CoordFlag.PAN),
}


def _set_voice(memory: SoundMemory, address: int) -> Instruction:
    """$EF n: voice n - refused where n's list is a patch over the voice before, not a voice."""
    if not isinstance(memory, Sh2Memory):
        raise TypeError("a voice is looked up through the driver's tables (Sh2Memory)")
    index = memory.byte(address + 1)
    if not is_full_voice(voice_registers(memory, voice_list(memory, memory.tables.voices, index))):
        raise RomError(f"${address:X}: $EF {index}: a register patch over the voice before, not a voice: not converted")
    op = Op(OpKind.EFFECT, effect=effect_from_bytes(CoordFlag.SET_VOICE, [index]))
    return Instruction((op,), _SET_VOICE_LENGTH, True)


_FM_FLAGS: dict[int, FlagSpec] = {
    **{byte: refuse("PSG noise form on an FM track", 1) for byte in _NOISE_FORMS},
    0xE7: refuse("LFO", 1),
    0xE8: refuse("stop flag", 1),
    0xEA: refuse("FMS", 1),
    0xEB: refuse("AMS", 1),
    0xEC: refuse("FM3 special mode on an FM track"),
    0xED: refuse("FM3 special mode on an FM track"),
    0xEE: effect(CoordFlag.LEGATO),
    0xEF: read(_set_voice),
    0xF0: effect(CoordFlag.SET_VOL),
    0xF1: effect(CoordFlag.SET_VOL),
    0xF4: effect(CoordFlag.PITCH_ENVELOPE),
    0xF5: drop("$F5 on FM (a PSG envelope)", 1),
    0xFC: refuse("slide mode", 1),
    0xFD: refuse("raw-frequency mode", 1),
    0xFE: effect(CoordFlag.ALTER_VOL),
    **_CONTROL,
}

# The drum track's: what its flags do to the drums is the drum reader's (Phase 3)
_DRUM_FLAGS: dict[int, FlagSpec] = {
    **{byte: drop("PSG noise form (drums)", 1) for byte in _NOISE_FORMS},
    0xE7: refuse("LFO", 1),
    0xE8: refuse("stop flag", 1),
    0xEA: refuse("FMS", 1),
    0xEB: refuse("AMS", 1),
    0xEC: drop("FM3 special mode on (drums)"),
    0xED: drop("FM3 special mode off (drums)"),
    0xEE: drop("legato (drums)", 1),
    0xEF: drop("voice (drums)", 1),
    0xF0: drop("volume (drums)", 1),
    0xF1: drop("volume (drums)", 1),
    0xF4: refuse("pitch envelope on the drum track", 1),
    0xF5: drop("PSG envelope (drums)", 1),
    0xFC: refuse("slide mode", 1),
    0xFD: drop("raw-frequency mode (no drum reads it)", 1),
    0xFE: drop("alter volume (drums)", 1),
    **_CONTROL,
}


def _fm_drums(rom: RomImage, header: SmpsSongHeader, fm_frequencies: tuple[int, ...]) -> dict[str, FmDrum]:
    """Every drum byte's drums, as the driver plays them (drums.py): the same in every song."""
    return {drum_name(byte): drum for byte, drum in read_sh2_drums(rom).items()}


def _rules_from_rom(rom: RomImage, rules: PlaybackRules) -> PlaybackRules:
    """Its FM and PSG tables and its pitch envelopes, read from the driver."""
    z80 = driver_ram(rom)
    return replace(rules, fm_frequencies=fm_frequencies(z80), psg_frequencies=psg_frequencies(z80), psg_read=psg_read(z80),
                   pitch_envelopes=read_pitch_envelopes(sh2_memory(rom)))


def drum_name(byte: int) -> str:
    """The drum track's byte: drumNN (its drums a bitmask, read in Phase 3)."""
    return f"drum{byte:02X}"


SH2 = SmpsVariant(
    name=SmpsDriver.SH2,
    memory=sh2_memory,
    locate=locate_sh2,
    # No music track is a PSG track: the PSG3 record is the drum track's half (header.py)
    flags={ChannelType.FM: _FM_FLAGS, ChannelType.DAC: _DRUM_FLAGS, ChannelType.PSG: {}},
    envelope_commands={},          # the PSG envelopes are the drums' (Phase 3)
    header=HEADER_SH2,
    voice_layout=None,
    # Its FM and PSG tables read from the driver (no PSG music: the drums' PSG half reads the PSG
    # table); the PSG envelopes: Phase 3
    rules=PlaybackRules(driver=SmpsDriver.SH2, fm_frequencies=(), psg_frequencies=(), psg_read=(), psg_envelopes={},
                        dac_names={b: drum_name(b) for b in range(FIRST_NOTE, LAST_NOTE + 1)}),
    rules_from_rom=_rules_from_rom,
    fm_drums=_fm_drums,
    music_header_reader=read_track_list,
    voice_reader=read_sh2_voices,
)
