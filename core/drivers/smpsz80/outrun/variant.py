"""OutRun's SMPS Z80 driver (smpsz80_outrun): Golden Axe's layout (the sound header at its bank's
start, the dispatch) with Sonic 1's flags, voices and DPCM, a DAC track, and Timer A for a clock.
No disassembly: its flag table at Z80 $0B25, read with a disassembler (docs/todo/done/outrun.md).

A handler starts at the first operand, and the dispatch steps one byte past where it leaves off: a
flag with no operand steps back ($E7).  $FF reads a sub-command's own operands ($0B65).  A track's
channel byte decides some flags: a PSG track ignores pan, FM volume and an FM voice, an FM track the
PSG volume and voice.  The drum track is FM6's (channel 6): it reads as an FM track.

Refused where no song plays them, or what they do the converter cannot play: the modulation ($F0 $F1:
none set), Timer A ($EA $EB: the update rate, T4), the note fill ($E8: frames x the divider), raw
YM writes, slide and raw-frequency modes, FM3 special mode, a voice from another song's table ($EF
with bit 7 set).  A sound played from a song ($FF $02) is dropped: each song converts alone.
"""

from __future__ import annotations

from dataclasses import replace

from core.rom.flags import (
    CALL,
    JUMP,
    LOOP,
    NO_ATTACK,
    RETURN,
    STOP,
    EnvelopeCommand,
    FlagSpec,
    drop,
    effect,
    read,
    refuse,
)
from core.rom.grammar import Instruction
from core.rom.image import RomError, RomImage
from core.rom.memory import SoundMemory
from core.rom.variant import SmpsVariant
from core.smps import (
    FIRST_NOTE,
    LAST_NOTE,
    ChannelType,
    CoordFlag,
    OctaveWrap,
    Op,
    OpKind,
    PlaybackRules,
    TimerA,
    TrackRules,
    effect_from_bytes,
)

from ...names import SmpsDriver
from ..pitch_envelopes import EnvelopeBytes, read_envelope_table
from ..program import driver_ram, fm_frequencies, psg_frequencies, psg_read
from .dac import dac_pans, outrun_dac
from .layout import HEADER_OUTRUN, TEMPO_PHASE, VOICE_OUTRUN
from .locate import driver_tables, locate_outrun, outrun_memory

# $EF n: n's bit 7 takes the voice from another song's table, the song the byte after names
_OTHER_SONG = 0x80
_VOICE_LENGTH = 2

# $FF n: the operands each sub-command reads ($04's: a word, a count, the bytes)
_META = {0x00: (1, "nothing"), 0x01: (1, "tempo add"), 0x02: (1, "play a sound"), 0x03: (1, "stop every track"),
         0x05: (1, "every track's divider"), 0x06: (4, "FM3 frequencies"), 0x07: (2, "track bytes $18-$19")}
_COPY = 0x04
_PLAY_SOUND = 0x02
_NOTHING = 0x00
_META_FIXED = 2                    # $FF and the sub-command


def _meta(memory: SoundMemory, address: int) -> Instruction:
    """$FF n ...: nothing and a sound played are dropped, the rest refused."""
    sub = memory.byte(address + 1)
    if sub not in _META:
        raise RomError(f"${address:X}: $FF ${sub:02X}{' (copy bytes)' if sub == _COPY else ''}: not converted")
    operands, what = _META[sub]
    length = _META_FIXED + operands
    if sub == _PLAY_SOUND:
        sound = memory.byte(address + _META_FIXED)
        return Instruction((), length, True, dropped=f"$FF $02: play sound ${sound:02X} (each song converts alone)")
    if sub == _NOTHING:
        return Instruction((), length, True, dropped="$FF $00 (nothing)")
    raise RomError(f"${address:X}: $FF ${sub:02X} {what}: not converted")


def _fm_voice(memory: SoundMemory, address: int) -> Instruction:
    """$EF n: voice n of the song's table; from another song's (bit 7) refused."""
    index = memory.byte(address + 1)
    if index & _OTHER_SONG:
        raise RomError(f"${address:X}: $EF ${index:02X}: a voice from another song's table: not converted")
    op = Op(OpKind.EFFECT, effect=effect_from_bytes(CoordFlag.SET_VOICE, [index]))
    return Instruction((op,), _VOICE_LENGTH, True)


def _psg_voice(memory: SoundMemory, address: int) -> Instruction:
    """$EF on a PSG track: its operands stepped over, nothing else ($0CF8)."""
    length = _VOICE_LENGTH + (memory.byte(address + 1) & _OTHER_SONG != 0)
    return Instruction((), length, True, dropped="$EF on a PSG track (ignored)")


# Read by every kind of track
_CONTROL: dict[int, FlagSpec] = {
    0xE1: effect(CoordFlag.DETUNE),
    0xE2: effect(CoordFlag.NOP),               # the communication byte
    0xE3: STOP,                                # $F2's, after a call ($09DF)
    0xE4: refuse("$E4 (5 bytes to the track's $11-$15: not known)", 5),
    0xE7: NO_ATTACK,
    0xE8: refuse("note fill (frames x the track's divider)", 1),
    0xE9: refuse("LFO (then AMS / FMS)", 2),
    0xEA: refuse("Timer A set (the update rate)", 2),
    0xEB: refuse("Timer A add (the update rate)", 2),
    0xED: refuse("YM register write, part I", 2),
    0xEE: refuse("YM register write, part II", 2),
    0xF0: refuse("modulation (no song sets one)", 4),
    0xF1: refuse("modulation on (no song sets one)", 2),
    0xF2: STOP,
    0xF4: effect(CoordFlag.PITCH_ENVELOPE),    # (ix+7): 0 none, $80 $F0's modulation
    0xF6: JUMP,
    0xF7: LOOP,
    0xF8: CALL,
    0xF9: RETURN,
    0xFA: effect(CoordFlag.CHAN_TEMPO_DIV),
    0xFB: effect(CoordFlag.CHANGE_TRANSPOSITION),
    0xFC: refuse("slide mode (each note: slide, a skipped byte, duration)", 1),
    0xFD: refuse("raw-frequency mode (each note an fnum word)", 1),
    0xFE: refuse("FM3 special mode", 4),
    0xFF: read(_meta),
}

_FM_FLAGS: dict[int, FlagSpec] = {
    **_CONTROL,
    0xE0: effect(CoordFlag.PAN),
    0xE5: effect(CoordFlag.SET_VOL),
    0xE6: effect(CoordFlag.ALTER_VOL),
    0xEC: drop("PSG volume on an FM track (ignored)", 1),
    0xEF: read(_fm_voice),
    0xF3: refuse("PSG noise on an FM track", 1),
    0xF5: drop("PSG voice on an FM track (ignored)", 1),
}

_PSG_FLAGS: dict[int, FlagSpec] = {
    **_CONTROL,
    0xE0: drop("pan on a PSG track (no write)", 1),
    0xE5: refuse("set volume on a PSG track (no song)", 1),
    0xE6: drop("FM volume on a PSG track (ignored)", 1),
    0xEC: effect(CoordFlag.ALTER_VOL),
    0xEF: read(_psg_voice),
    0xF3: effect(CoordFlag.PSG_FORM),
    0xF5: effect(CoordFlag.PSG_VOICE),
}

# PSG volume envelopes ($0F00): $80 restarts, $81 holds, $83 silences; any other command jumps to
# the step its next byte names
_ENVELOPE_COMMANDS = {
    **{byte: EnvelopeCommand.JUMP for byte in range(0x82, 0x100)},
    0x80: EnvelopeCommand.RESTART,
    0x81: EnvelopeCommand.HOLD,
    0x83: EnvelopeCommand.MUTE,
}

# Frequency envelopes ($04C7): the offset times 1 + the scale added to the word whole ($0516)
_PITCH_ENVELOPE_BYTES = EnvelopeBytes(restart=0x80, holds=frozenset({0x81, 0x83}), scale=0x84, jump=0x82,
                                      first_negative=0x85, byte_scale=False)


# An FM track's word and detune kept within an octave as the update adds them (Z80 $0563): at or
# under fnum $283 a block down, $285 up; over $508 a block up, $284 down
_FM_TRACK = TrackRules(octave_wrap=OctaveWrap(low=0x283, high=0x508, drop=0x285, rise=0x284),
                       tie_restarts_envelope=False)

# A tie skips the note's resets ($0391: bit 1 / ret nz): the PSG envelope plays on from the attack
_PSG_TRACK = TrackRules(tie_restarts_envelope=False)


def _rules_from_rom(rom: RomImage, rules: PlaybackRules) -> PlaybackRules:
    """Its FM and PSG tables, its frequency envelopes, its clock and its samples' pans, read from the driver."""
    z80 = driver_ram(rom)
    tables = driver_tables(rom)
    envelopes = read_envelope_table(outrun_memory(rom), tables.pitch_envelopes, _PITCH_ENVELOPE_BYTES)
    return replace(rules, fm_frequencies=fm_frequencies(z80), psg_frequencies=psg_frequencies(z80), psg_read=psg_read(z80),
                   pitch_envelopes=envelopes, update_clock=TimerA(tables.timer_a), dac_pans=dac_pans(rom))


def dac_name(byte: int) -> str:
    """The DAC track's byte: dacNN (the samples: Phase 3 of the plan)."""
    return f"dac{byte:02X}"


OUTRUN = SmpsVariant(
    name=SmpsDriver.OUTRUN,
    memory=outrun_memory,
    locate=locate_outrun,
    flags={ChannelType.FM: _FM_FLAGS, ChannelType.DAC: _FM_FLAGS, ChannelType.PSG: _PSG_FLAGS},
    envelope_commands=_ENVELOPE_COMMANDS,
    header=HEADER_OUTRUN,
    voice_layout=VOICE_OUTRUN,
    # Its FM and PSG tables, envelopes and Timer A read from the driver; tempo: the first hold a
    # frame late
    rules=PlaybackRules(driver=SmpsDriver.OUTRUN, fm_frequencies=(), psg_frequencies=(), psg_read=(), psg_envelopes={},
                        dac_names={b: dac_name(b) for b in range(FIRST_NOTE, LAST_NOTE + 1)}, tempo_phase=TEMPO_PHASE,
                        tracks={ChannelType.FM: _FM_TRACK, ChannelType.PSG: _PSG_TRACK}),
    rules_from_rom=_rules_from_rom,
    dac=outrun_dac,
)
