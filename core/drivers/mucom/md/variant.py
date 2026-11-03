"""Mucom-MD 0.85: Streets of Rage 2 (Bare Knuckle II).  No disassembly: its driver ($40000-$429CE)
read with a disassembler (docs/drivers/streets_of_rage_2.md).

    songs       MUCOM88's images, byte for byte (header.py); the index and tables by the code that
                reads each (locate.py)
    tracks      FM1-FM5, PSG1-PSG3 and the drum track on FM6 as DAC; a tick a frame
    voice       MUCOM88's 25 bytes (core.rom.variant.MUCOM_VOICE), as Streets of Rage's
    flags       one table per kind of track (FM $41B74, PSG $423E8, the drum track $4285A); the
                grammar (grammar.py) reads the rest
    volume      FM: $F1 v keeps step v - 2 (a byte, $41C02), 19 at most; TL = the volume table's;
                $FB n adds to the step kept, clamped 0-19 ($41DC0).  PSG: $F1 v keeps v (0 under it),
                att = v ^ 15; $FB n adds, clamped 0-15
    DAC         the Z80 player's samples $81-$84 (dac.py)

    envelopes   the PSG's $F0 n a preset of the ROM's (locate.psg_presets), $FA six bytes: an ADSR
                envelope each (core/smps/adsr.py), an instrument per envelope and volume
    noise       $F7 n takes the noise channel (white, fixed rate), $F7 8 gives it back, $F8 n its rate:
                the track's notes play on a NOISE channel of their own (core.smps.playback)
"""

from __future__ import annotations

from dataclasses import replace

from core.rom.flags import NO_ATTACK, FlagSpec, drop, effect, read, refuse
from core.rom.grammar.mucom import dac_sample, detune, loop_end, loop_exit, loop_start, register_write
from core.rom.image import RomImage
from core.rom.variant import MUCOM_VOICE, HeaderLayout, SmpsVariant, TrackSlot
from core.smps import (
    ADSR_ENVELOPES,
    ChannelType,
    CoordFlag,
    KeyOffRules,
    ModulationRules,
    PitchRules,
    PlaybackRules,
    SoftReleaseLaw,
    TieRules,
    TrackRules,
    VolumeRules,
)

from ...names import SmpsDriver
from .dac import MUSIC_SAMPLES, mucom_md_dac
from .grammar import (
    DIALECT,
    PSG_FIRST,
    PSG_NOTES,
    drum_prefix,
    drum_vibrato,
    envelope,
    noise,
    noise_rate,
    prefix,
    psg_vibrato,
)
from .header import read_image
from .locate import FM_VOLUME_STEPS, PSG_MAX_DIVIDER, fm_frequencies, fm_volumes, locate, psg_octave, psg_presets
from .memory import MucomMemory

_BYTE_VALUES = 0x100
_SIGNED_STEPS = range(-0x80, 0x80)          # a flag's byte, as TrackRules keys it
_FM_STEP_OFFSET = 2                          # $F1 v: step v - 2
_FM_STEPS = range(FM_VOLUME_STEPS)           # the step kept: 0-19
_PSG_VOLUME = 0xF                            # att = v ^ 15
_PSG_STEPS = range(_PSG_VOLUME + 1)          # $FB's sum clamped 0-15
_PSG_KEPT = range(0x80)                      # $F1 v: v, 0 under it
_INITIAL_VOLUME = 8                          # every track starts at step 8 ($408BE, $40940)
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
    0xF0: effect(CoordFlag.ENVELOPE_PRESET),
    0xF1: effect(CoordFlag.VOLUME_STEP),
    0xF2: read(detune),
    0xF3: effect(CoordFlag.GATE),
    0xF4: read(psg_vibrato),
    **_LOOPS,
    0xF7: read(noise),
    0xF8: read(noise_rate),
    0xF9: drop("$F9 (skipped)", 1),
    0xFA: read(envelope),
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
    """Its FM octave and volume table, its PSG table and envelope presets, read from the ROM."""
    psg = _psg_dividers(psg_octave(rom))
    track = rules.track(ChannelType.FM)
    volumes = fm_volumes(rom)
    fm = replace(track, volume=replace(track.volume, steps={step: volumes[step] for step in _FM_STEPS}))
    return replace(rules, fm_frequencies=fm_frequencies(rom), psg_frequencies=psg[:PSG_NOTES], psg_read=psg,
                   tracks={**rules.tracks, ChannelType.FM: fm}, adsr_presets=psg_presets(rom))


def _fm_kept_step(v: int) -> int:
    """The step `$F1 v` keeps: v - 2 as a byte, 19 at most (v 0 and 1: 19; the tst.b before its
    bcc clears the carry, so the clamp to 0 never runs)."""
    return min((v - _FM_STEP_OFFSET) % _BYTE_VALUES, _FM_STEPS.stop - 1)


def _psg_dividers(octave: tuple[int, ...]) -> tuple[int, ...]:
    """The divider each table index plays (0 = A2): the octave's word >> the note's octave,
    clamped to the tone register."""
    out = []
    for index in range(_PSG_READ):
        octave_number, semitone = divmod(index + PSG_FIRST, _SEMITONES)
        out.append(min(max(octave[semitone] >> octave_number, 1), PSG_MAX_DIVIDER))
    return tuple(out)


# How each kind of track reads:
#   $FA's writes are queued and written after the volume ($41996): a carrier's TL holds until the
#     next volume change or voice
#   a PSG track starts at volume 8, att 7; an FM track at step 8 too, but at TL 0 until its first
#     $F1 (every one sets it before a note)
#   the gate ($40E64 FM, $4204E PSG) spares a note the next byte ties; FM keys a note no longer
#     than the gate 1 frame ($40F72), the PSG plays it whole ($4213C)
#   soft release: at the gate FM drops to step (step + n) >> 1 ($40EE0), the PSG to (v - n) >> 1
#     ($42076); a rest keys nothing off while it is on ($41020, $4216E), an FM voice set does ($416BA);
#     a PSG note under an envelope turns it off ($420FE)
#   the vibrato moves at each turn (no pause), its counter first loaded with half the count ($40DE4)
#   the PSG adds (detune + vibrato) >> the note's octave, the divider clamped to 1-1023 ($4226C)
#   the loop and a loop's exit ($FE) keep a tie (they leave the tie bit); a $FD after a flag sets the tie bit after
#     the note's end keyed it off ($41C9C): the next note attacks, unless a soft release spared it
#   the drum track: a tie asks for no sample ($4277A), and a rest stops none: the sample plays out
_NO_TURN_PAUSE = ModulationRules(turn_pause=False)
_LATE_TIE = TieRules(late_keys_off=True, exit_clears=False)
_FM_TRACK = TrackRules(
    volume=VolumeRules(kept={v: _fm_kept_step(v) for v in _SIGNED_STEPS}, step_range=_FM_STEPS,
                       tl_writes_hold=True),
    key_off=KeyOffRules(gate_sees_tie=True, gate_floor=1, soft_release=SoftReleaseLaw(sign=1, voice_keys_off=True)),
    ties=_LATE_TIE, modulation=_NO_TURN_PAUSE)
_PSG_TRACK = TrackRules(
    volume=VolumeRules(steps={v: v ^ _PSG_VOLUME for v in _PSG_KEPT}, kept={v: max(v, 0) for v in _SIGNED_STEPS},
                       step_range=_PSG_STEPS, initial_step=_INITIAL_VOLUME),
    key_off=KeyOffRules(gate_sees_tie=True, soft_release=SoftReleaseLaw(sign=-1, envelope_clears=True)),
    ties=_LATE_TIE, modulation=_NO_TURN_PAUSE, pitch=PitchRules(octave_shift_from=PSG_FIRST, divider_clamp=True, noise_writes_tone3=False))
_DRUM_TRACK = TrackRules(ties=TieRules(drum_restarts=False))


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
                        psg_envelopes=ADSR_ENVELOPES, dac_names={b: f"dac{b:02X}" for b in MUSIC_SAMPLES},
                        tracks={ChannelType.FM: _FM_TRACK, ChannelType.PSG: _PSG_TRACK, ChannelType.DAC: _DRUM_TRACK}),
    rules_from_rom=_rules_from_rom,
    grammar=DIALECT.instruction,
    music_header_reader=read_image,
    dac=mucom_md_dac,
)
