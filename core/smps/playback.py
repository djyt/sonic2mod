"""What a song plays, note by note: the driver's output with the assembly's spelling gone.

Two songs that play the same give the same PlayedSong, however their events are written: a
call or a loop, the order of flags before a note, a transposition or the note byte itself, a
voice's own TL or the track volume.  A parse and a VGM lift are compared through it.

    SmpsSong ──song_prep (tempo-divider re-timing, loops replayed)──► walk every channel
             ──► PlayedSong: tempo, loop, end + per channel [PlayedNote ...]

Consecutive rests are one rest (the second keys nothing off).  A tie (smpsNoAttack) stays a
note of its own: the driver restarts its modulation and note fill there.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import StrEnum

from ..chips import TL_MASK
from .driver_tables import FM_FREQUENCIES, PSG_FREQUENCIES_EXTENDED, fm_note_index, psg_note_index
from .names import source_names
from .song import CoordFlag, SmpsNote, SmpsSong, SmpsVoice
from .song_prep import apply_global_tempo_div, extend_looping_channels
from .track import TrackState


class Aspect(StrEnum):
    """One thing two songs can disagree on; each PlayedNote field belongs to one."""

    TIMING = "timing"           # where notes and rests start, how long they last
    ATTACK = "attack"           # keyed on, or not (smpsNoAttack: a tie or a legato)
    PITCH = "pitch"             # the frequency word written: note, transposition and detune in one
    VOICE = "voice"             # FM: the operator registers but the carriers' TL; PSG: the envelope
    LEVEL = "level"             # FM: the carriers' TL; PSG: the attenuation
    PAN = "pan"
    MODULATION = "modulation"   # smpsModSet's parameters, while on
    FILL = "fill"               # smpsNoteFill frames
    NOISE = "noise"             # the noise register byte (smpsPSGform)
    DAC = "dac"                 # the DAC sample


@dataclass(frozen=True, slots=True)
class PlayedNote:
    tick: int
    duration: int
    rest: bool
    attack: bool = True
    pitch: int | None = None                # FM: block << 11 | fnum; PSG: the divider
    voice: object = None                    # FM: (B0, ((register, byte) ...)); PSG: envelope name
    level: object = None                    # FM: carrier TLs; PSG: attenuation
    pan: str = "C"
    modulation: tuple[int, ...] | None = None
    fill: int = 0
    noise: int | None = None
    dac: str = ""

    def aspect(self, aspect: Aspect) -> object:
        """This note's value for `aspect`."""
        return tuple(getattr(self, name) for name in _ASPECT_FIELDS[aspect])


_ASPECT_FIELDS = {
    Aspect.TIMING: ("duration", "rest"), Aspect.ATTACK: ("attack",), Aspect.PITCH: ("pitch",),
    Aspect.VOICE: ("voice",), Aspect.LEVEL: ("level",), Aspect.PAN: ("pan",),
    Aspect.MODULATION: ("modulation",), Aspect.FILL: ("fill",), Aspect.NOISE: ("noise",), Aspect.DAC: ("dac",),
}


@dataclass(frozen=True)
class PlayedSong:
    tempo: tuple[int, int]                          # (modifier, divider) the header starts with
    tempo_changes: tuple[tuple[int, CoordFlag, int], ...]   # (tick, smpsSetTempoMod / Div, value)
    loop_tick: int | None
    end_tick: int
    channels: dict[str, list[PlayedNote]]           # "DAC", "FM1" .. "PSG3"


_TEMPO_FLAGS = (CoordFlag.SET_TEMPO_MOD, CoordFlag.SET_TEMPO_DIV)


def played_song(song: SmpsSong) -> PlayedSong:
    """What `song` plays (a copy is prepared; `song` is left as it is)."""
    song = copy.deepcopy(song)
    apply_global_tempo_div(song)
    extend_looping_channels(song)

    voices = {v.index: v for v in song.voices}
    changes = sorted({(ev.tick_position, ev.effect.flag, ev.effect.params[0])
                      for ch in song.channels for ev in ch.events
                      if ev.is_effect and ev.effect.flag in _TEMPO_FLAGS})
    channels = {name: _played_channel(ch, voices) for name, ch in zip(source_names(song), song.channels, strict=True)}
    return PlayedSong((song.header.tempo_modifier, song.header.tempo_divider), tuple(changes),
                      song.loop_target_tick(), song.end_tick(), channels)


def _played_channel(channel, voices: dict[int, SmpsVoice]) -> list[PlayedNote]:
    st = TrackState.for_header(channel.header)
    played: list[PlayedNote] = []
    base: int | None = None            # the table word of the last note: a retrigger re-keys it

    for ev in channel.events:
        if ev.is_effect:
            st.apply(ev.effect)
            continue
        note = ev.note
        if note.duration <= 0:
            continue

        # A rest after a rest only lengthens it
        if note.is_rest:
            last = played[-1] if played else None
            if last is not None and last.rest and last.tick + last.duration == ev.tick_position:
                played[-1] = PlayedNote(last.tick, last.duration + note.duration, rest=True)
            else:
                played.append(PlayedNote(ev.tick_position, note.duration, rest=True))
            continue

        if note.is_dac:
            played.append(PlayedNote(ev.tick_position, note.duration, rest=False, dac=note.dac_name))
            continue

        if not note.is_retrigger or base is None:
            base = _table_word(note, st)
        played.append(_note(ev.tick_position, note, st, base + st.detune, voices))
    return played


def _table_word(note: SmpsNote, st: TrackState) -> int:
    """The frequency word the driver reads for a note byte at the track's transposition."""
    if st.is_psg:
        return PSG_FREQUENCIES_EXTENDED[psg_note_index(note.note_value, st.transpose)]
    return FM_FREQUENCIES[fm_note_index(note.note_value, st.transpose)]


def _note(tick: int, note: SmpsNote, st: TrackState, pitch: int, voices: dict[int, SmpsVoice]) -> PlayedNote:
    if st.is_psg:
        voice, level = st.envelope, st.att
    else:
        voice, level = _fm_voice(voices.get(st.voice) if st.voice is not None else None, st.tl)
    return PlayedNote(tick, note.duration, rest=False, attack=not note.is_no_attack, pitch=pitch,
                      voice=voice, level=level, pan=st.pan,
                      modulation=st.modulation if st.modulation_on else None, fill=st.fill,
                      noise=st.noise_form, dac="")


def _fm_voice(voice: SmpsVoice | None, tl_offset: int) -> tuple[object, object]:
    """(the voice's registers but the carriers' TL, the carriers' TL) at the track volume."""
    if voice is None:
        return None, None
    regs = voice.registers(tl_offset)
    carriers = voice.carrier_registers
    timbre = tuple(sorted((r, v) for r, v in regs.items() if r not in carriers))
    return (voice.feedback_algorithm, timbre), tuple(regs[r] & TL_MASK for r in carriers)

