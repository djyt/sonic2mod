"""What a song plays, note by note: the driver's output with the assembly's spelling gone.

Two songs that play the same give the same PlayedSong, however their events are written: a
call or a loop, the order of flags before a note, a transposition or the note byte itself, a
voice's own TL or the track volume.  A parse and a VGM lift are compared through it.

    SmpsSong ──song_prep (tempo-divider re-timing, loops replayed)──► walk every channel
             ──► PlayedSong: tempo modifier and changes, loop, end + per channel [PlayedNote ...]

Consecutive rests are one rest (the second keys nothing off).  A tie (smpsNoAttack) stays a
note of its own: the driver writes its frequency and a key-on (to a keyed channel: no attack).
A held duration (`smpsNoAttack, $34`, which the parser spells as a no-attack rest) is a tie too:
the last note re-keyed.  A channel that stops (smpsStop) rests to the song's end.
smpsNoteFill is a key-off: a note it cuts is a note and a rest from the
tick the key-off frame plays (as a lift reads it), the fill itself one more aspect.
smpsNoAttack only skips the key-off: after a rest, or once smpsNoteFill has keyed the note off
(it counts frames, from the last note that attacked), the key-on attacks - GHZ FM4's no-attack
note at the loop follows a rest.  A DAC rest stops nothing (the sample plays out): it is part of
the hit before it.
The tempo divider only says how durations are spelled (they are ticks here), so it is not part of
what plays; smpsSetTempoDiv neither, once song_prep has re-timed the song.
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass
from enum import StrEnum

from .driver_tables import FM_FREQUENCIES, PSG_FREQUENCIES_EXTENDED, fm_note_index, psg_note_index
from .names import source_names
from .song import CoordFlag, SmpsNote, SmpsSong, SmpsVoice
from .song_prep import apply_global_tempo_div, extend_looping_channels
from .tempo import TempoSegment, frame_of_tick, tempo_schedule, tick_at_frame
from .track import TrackState


class Aspect(StrEnum):
    """One thing two songs can disagree on; each PlayedNote field belongs to one."""

    ONSET = "onset"             # where the channel attacks: a keyed note or a DAC hit; the tempo, the loop
    LENGTH = "length"           # how long each note lasts: durations, rests, ties and legato notes
    NOTE = "note"               # the table note: note byte and transposition, no detune
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
    note: int | None = None                 # the table word of the note, before detune
    pitch: int | None = None                # FM: block << 11 | fnum; PSG: the divider
    voice: object = None                    # FM: (B0, ((register, byte) ...)); PSG: envelope name
    level: object = None                    # FM: carrier TLs; PSG: attenuation
    pan: str = "C"
    modulation: tuple[int, ...] | None = None
    fill: int = 0
    noise: int | None = None
    dac: str = ""

    @property
    def onset(self) -> bool:
        """The channel attacks here: a keyed note or a DAC hit."""
        return not self.rest and self.attack

    def aspect(self, aspect: Aspect) -> object:
        """This note's value for `aspect` (a tuple where the aspect is several fields: length)."""
        values = tuple(getattr(self, name) for name in _ASPECT_FIELDS[aspect])
        return values[0] if len(values) == 1 else values


_ASPECT_FIELDS = {
    Aspect.ONSET: ("attack",), Aspect.LENGTH: ("duration", "rest", "attack"), Aspect.NOTE: ("note",),
    Aspect.PITCH: ("pitch",),
    Aspect.VOICE: ("voice",), Aspect.LEVEL: ("level",), Aspect.PAN: ("pan",),
    Aspect.MODULATION: ("modulation",), Aspect.FILL: ("fill",), Aspect.NOISE: ("noise",), Aspect.DAC: ("dac",),
}


@dataclass(frozen=True)
class PlayedSong:
    modifier: int                                   # the tempo modifier the song starts with
    tempo_changes: tuple[tuple[int, int], ...]      # (tick, modifier) of every smpsSetTempoMod
    loop_tick: int | None
    end_tick: int
    channels: dict[str, list[PlayedNote]]           # "DAC", "FM1" .. "PSG3"

    @property
    def loop_span(self) -> int | None:
        """Ticks from the loop to the end: one pass of the looped part."""
        return None if self.loop_tick is None else self.end_tick - self.loop_tick


def played_song(song: SmpsSong) -> PlayedSong:
    """What `song` plays (a copy is prepared; `song` is left as it is)."""
    song = copy.deepcopy(song)
    apply_global_tempo_div(song)
    extend_looping_channels(song)

    voices = {v.index: v for v in song.voices}
    changes = sorted({(ev.tick_position, ev.effect.params[0]) for ch in song.channels for ev in ch.events
                      if ev.is_effect and ev.effect.flag == CoordFlag.SET_TEMPO_MOD})
    schedule = tempo_schedule(_NO_HOLDS if song.header.is_sfx else song.header.tempo_modifier, changes)
    end = song.end_tick()
    channels = {name: _played_channel(ch, voices, schedule, end)
                for name, ch in zip(source_names(song), song.channels, strict=True)}
    return PlayedSong(song.header.tempo_modifier, tuple(changes), song.loop_target_tick(), end, channels)


# SFX run a tick every frame: a modifier no song reaches holds nothing
_NO_HOLDS = 1 << 30


def _played_channel(channel, voices: dict[int, SmpsVoice], schedule: tuple[TempoSegment, ...],
                    end: int) -> list[PlayedNote]:
    st = TrackState.for_header(channel.header)
    played: list[PlayedNote] = []
    base: int | None = None            # the table word of the last note: a retrigger re-keys it
    resting = True                     # the last read was a rest (the driver cleared Freq)
    keyed = False                      # the channel sounds a note
    fill_off: int | None = None        # the frame smpsNoteFill keys it off on

    for ev in channel.events:
        if ev.is_effect:
            st.apply(ev.effect)
            continue
        note = ev.note
        if note.duration <= 0:
            continue

        # A held duration re-keys the last note (a rest when there is none: TrackSetRest cleared it)
        if note.is_rest and note.is_no_attack and not resting and base is not None:
            note = dataclasses.replace(note, is_rest=False, is_retrigger=True)

        resting = note.is_rest
        if note.is_rest:
            keyed = False
            _rest(played, ev.tick_position, note.duration)
            continue
        if note.is_dac:
            played.append(PlayedNote(ev.tick_position, note.duration, rest=False, dac=note.dac_name))
            continue

        # Attacks unless smpsNoAttack finds the note still keyed; the fill restarts on an attack
        read = frame_of_tick(schedule, ev.tick_position)
        keyed = keyed and (fill_off is None or read <= fill_off)
        attack = not note.is_no_attack or not keyed
        if not note.is_no_attack:
            fill_off = read + st.fill if st.fill else None
        keyed = True

        if not note.is_retrigger or base is None:
            base = _table_word(note, st)
        played += _filled(_note(ev.tick_position, note, st, base, voices, attack), read, fill_off, schedule)

    # smpsStop keys the channel off: it rests while the others play on
    stop = played[-1].tick + played[-1].duration if played else 0
    if stop < end:
        _rest(played, stop, end - stop)
    return played


def _rest(played: list[PlayedNote], tick: int, duration: int) -> None:
    """A rest appended: after a rest, or a DAC hit (the sample plays out), it only lengthens it."""
    last = played[-1] if played else None
    if last is not None and (last.rest or last.dac) and last.tick + last.duration == tick:
        played[-1] = dataclasses.replace(last, duration=last.duration + duration)
        return
    played.append(PlayedNote(tick, duration, rest=True))


def _filled(note: PlayedNote, read: int, fill_off: int | None, schedule: tuple[TempoSegment, ...]) -> list[PlayedNote]:
    """`note`, or the note and a rest where smpsNoteFill keys it off before the next read.

        frame   0 1 2 3 . 5 6 7 8 . 10      m = 5, fill 2: off on frame 2
        note    C . . . . . . . . . next    ->  C 2 ticks, rest 6 ticks
    """
    if fill_off is None or not read < fill_off < frame_of_tick(schedule, note.tick + note.duration):
        return [note]
    sounds = tick_at_frame(schedule, fill_off) - note.tick
    if sounds >= note.duration:
        return [note]
    played = [dataclasses.replace(note, duration=sounds)]
    _rest(played, note.tick + sounds, note.duration - sounds)
    return played


def _table_word(note: SmpsNote, st: TrackState) -> int:
    """The frequency word the driver reads for a note byte at the track's transposition."""
    if st.is_psg:
        return PSG_FREQUENCIES_EXTENDED[psg_note_index(note.note_value, st.transpose)]
    return FM_FREQUENCIES[fm_note_index(note.note_value, st.transpose)]


def _note(tick: int, note: SmpsNote, st: TrackState, base: int, voices: dict[int, SmpsVoice],
          attack: bool) -> PlayedNote:
    if st.is_psg:
        voice, level = st.envelope, st.att
    else:
        voice, level = _fm_voice(voices.get(st.voice) if st.voice is not None else None, st.tl)
    return PlayedNote(tick, note.duration, rest=False, attack=attack, note=base, pitch=base + st.detune,
                      voice=voice, level=level, pan=st.pan,
                      modulation=st.modulation if st.modulation_on else None, fill=st.fill,
                      noise=st.noise_form, dac="")


def _fm_voice(voice: SmpsVoice | None, tl_offset: int) -> tuple[object, object]:
    """(the voice's registers but the carriers' TL, the carriers' TL) at the track volume."""
    if voice is None:
        return None, None
    regs = voice.chip_registers(tl_offset)
    carriers = voice.carrier_registers
    timbre = tuple(sorted((r, v) for r, v in regs.items() if r not in carriers))
    return (voice.feedback_algorithm, timbre), tuple(regs[r] for r in carriers)

