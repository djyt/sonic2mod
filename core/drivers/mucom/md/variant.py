"""Mucom-MD 0.85: Streets of Rage 2 (Bare Knuckle II).  No disassembly: its driver ($40000-$429CE)
read with a disassembler (docs/todo/streets_of_rage_2.md § 1).

    songs       MUCOM88's images, byte for byte (header.py); the index and tables by the code that
                reads each (locate.py)
    tracks      FM1-FM5, PSG1-PSG3 and the drum track on FM6 as DAC; a tick a frame
    voice       MUCOM88's 25 bytes (core.rom.variant.MUCOM_VOICE), as Streets of Rage's
    flags       one table per kind of track (FM $41B74, PSG $423E8, the drum track $4285A); the
                grammar (grammar.py) reads the rest
    volume      FM: $F1 v sets step clamp(v - 2, 0, 19), TL = the volume table's; $FB n adds,
                clamped.  PSG: $F1 v (0-15), att = v ^ 15; $FB n adds, clamped
    DAC         the Z80 player's samples $81-$84 (dac.py)

Dropped and reported until Phase 3: soft release, PSG envelopes ($F0, $FA), noise ($F7, $F8).
"""

from __future__ import annotations

from dataclasses import replace

from core.rom.flags import NO_ATTACK, FlagSpec, drop, effect, read, refuse
from core.rom.grammar.mucom import dac_sample, detune, loop_end, loop_exit, loop_start, register_write
from core.rom.image import RomImage
from core.rom.variant import MUCOM_VOICE, HeaderLayout, SmpsVariant, TrackSlot
from core.smps import ChannelType, CoordFlag, PlaybackRules, TrackRules

from ...names import SmpsDriver
from .dac import MUSIC_SAMPLES, mucom_md_dac
from .grammar import DIALECT, PSG_FIRST, PSG_NOTES, drum_prefix, drum_vibrato, prefix
from .header import read_image
from .locate import FM_VOLUME_STEPS, PSG_MAX_DIVIDER, fm_frequencies, fm_volumes, locate, psg_octave
from .memory import MucomMemory

_BYTE_VALUES = 0x100
_SIGNED_STEPS = range(-0x80, 0x80)          # a flag's byte, as TrackRules keys it
_FM_STEP_OFFSET = 2                          # $F1 v: step v - 2
_PSG_VOLUME = 0xF                            # att = v ^ 15
_SEMITONES = 12
_PSG_READ = 128                              # the words a 7-bit index reads

# The tracks as the generic header reader would lay them out (read_image reads them)
_HEADER = HeaderLayout(
    fm_slots=(*[TrackSlot(ChannelType.FM, f"FM{n}") for n in range(1, 6)], TrackSlot(ChannelType.DAC)),
    sfx_channels=frozenset(),
    tempo=False,
    psg_slots=("PSG1", "PSG2", "PSG3"),
)

# The loops, the same on every kind of track
_LOOPS: dict[int, FlagSpec] = {0xF5: read(loop_start), 0xF6: read(loop_end), 0xFE: read(loop_exit)}

_FM_FLAGS: dict[int, FlagSpec] = {
    0xF0: effect(CoordFlag.SET_VOICE),
    0xF1: effect(CoordFlag.VOLUME_STEP),
    0xF2: read(detune),
    0xF3: effect(CoordFlag.GATE),
    0xF4: read(DIALECT.vibrato),
    **_LOOPS,
    0xF7: effect(CoordFlag.FM3_SPECIAL, 4),     # OP4 OP3 OP2 OP1's offsets
    0xF8: read(DIALECT.pan),
    0xF9: drop("$F9 (skipped)", 1),
    0xFA: read(register_write),
    0xFB: effect(CoordFlag.ALTER_VOLUME_STEP),
    0xFC: effect(CoordFlag.LFO, 3),
    0xFD: NO_ATTACK,
    0xFF: read(prefix),
}

_PSG_FLAGS: dict[int, FlagSpec] = {
    0xF0: drop("envelope preset", 1),
    0xF1: effect(CoordFlag.VOLUME_STEP),
    0xF2: read(detune),
    0xF3: effect(CoordFlag.GATE),
    0xF4: read(DIALECT.vibrato),
    **_LOOPS,
    0xF7: drop("noise on / off", 1),
    0xF8: drop("noise rate", 1),
    0xF9: drop("$F9 (skipped)", 1),
    0xFA: drop("envelope", 6),
    0xFB: effect(CoordFlag.ALTER_VOLUME_STEP),
    0xFC: refuse("$FC on a PSG track (no LFO)"),
    0xFD: NO_ATTACK,
    0xFF: read(prefix),
}

# The drum track reads FM's handlers where they are FM's: what they set plays on no sample
_DAC_FLAGS: dict[int, FlagSpec] = {
    0xF0: read(dac_sample),
    0xF1: drop("FM6 volume", 1),
    0xF2: drop("$F2 (skipped)", 3),
    0xF3: drop("gate (the sample plays on)", 1),
    0xF4: read(drum_vibrato),
    **_LOOPS,
    0xF7: drop("$F7 (skipped)", 4),
    0xF8: read(DIALECT.pan),
    0xF9: drop("$F9 (skipped)", 1),
    0xFA: read(register_write),
    0xFB: drop("FM6 volume", 1),
    0xFC: refuse("$FC on the drum track (no LFO)"),
    0xFD: NO_ATTACK,
    0xFF: read(drum_prefix),
}


def _rules_from_rom(rom: RomImage, rules: PlaybackRules) -> PlaybackRules:
    """Its FM octave and volume table, and its PSG table, read from the ROM."""
    psg = _psg_dividers(psg_octave(rom))
    fm = replace(rules.track(ChannelType.FM), volume_steps=_fm_volume_steps(fm_volumes(rom)))
    return replace(rules, fm_frequencies=fm_frequencies(rom), psg_frequencies=psg[:PSG_NOTES], psg_read=psg,
                   tracks={**rules.tracks, ChannelType.FM: fm})


def _fm_volume_steps(volumes: tuple[int, ...]) -> dict[int, int]:
    """`$F1 v`'s TL for every byte v: the step v - 2 (unsigned), clamped to the table."""
    return {v: volumes[min(max((v % _BYTE_VALUES) - _FM_STEP_OFFSET, 0), FM_VOLUME_STEPS - 1)] for v in _SIGNED_STEPS}


def _psg_dividers(octave: tuple[int, ...]) -> tuple[int, ...]:
    """The divider each table index plays (0 = A2): the octave's word >> the note's octave,
    clamped to the tone register."""
    out = []
    for index in range(_PSG_READ):
        octave_number, semitone = divmod(index + PSG_FIRST, _SEMITONES)
        out.append(min(max(octave[semitone] >> octave_number, 1), PSG_MAX_DIVIDER))
    return tuple(out)


# How each kind of track reads; what Phase 3 checks against the driver is Sonic 1's until then:
#   $FA's writes are queued and written after the volume ($41996): a carrier's TL holds until the
#   next volume change or voice
_FM_TRACK = TrackRules(carrier_writes_hold=True)
_PSG_TRACK = TrackRules(volume_steps={v: max(v, 0) ^ _PSG_VOLUME for v in _SIGNED_STEPS})


MUCOM_MD = SmpsVariant(
    name=SmpsDriver.MUCOM_MD,
    memory=MucomMemory,
    locate=locate,
    flags={ChannelType.FM: _FM_FLAGS, ChannelType.PSG: _PSG_FLAGS, ChannelType.DAC: _DAC_FLAGS},
    envelope_commands={},
    header=_HEADER,
    voice_layout=MUCOM_VOICE,
    # Its FM octave, volume table and PSG table read from the ROM; the Z80 player's samples
    rules=PlaybackRules(driver=SmpsDriver.MUCOM_MD, fm_frequencies=(), psg_frequencies=(), psg_read=(),
                        psg_envelopes={}, dac_names={b: f"dac{b:02X}" for b in MUSIC_SAMPLES},
                        tracks={ChannelType.FM: _FM_TRACK, ChannelType.PSG: _PSG_TRACK}),
    rules_from_rom=_rules_from_rom,
    grammar=DIALECT.instruction,
    music_header_reader=read_image,
    dac=mucom_md_dac,
)
