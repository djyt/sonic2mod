"""A song's track code as the driver reads it, and the one walk that turns it into events.

Two front ends read a song into the same instructions; the driver's reading rules live here once:

    .asm   SmpsParser (macros, dc.b)  ──┐
                                        ├──> SmpsCode ──> song_from_code ──> SmpsSong
    ROM    core.rom (bytes)           ──┘

The rules: a note waits for the duration byte after it (a flag or a label completes it with the
saved one); a duration with no note re-keys the last note; smpsNoAttack marks the next read;
smpsLoop is unrolled, smpsCall inlined, smpsJump ends the channel (a loop) or is followed (a
forward jump into code not yet walked); a channel's loop starts where ITS walk reached the target.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, auto

from .driver_tables import psg_voice_name
from .effects import ChanTempoDiv, CoordFlag, Pan, PsgVoice, SelectSample, SetVoice, SmpsEffect, effect_of
from .percussion import FmDrum
from .rules import PlaybackRules
from .run_out import apply_run_out
from .song import (
    REST,
    ChannelType,
    SmpsChannel,
    SmpsChannelHeader,
    SmpsEvent,
    SmpsNote,
    SmpsSong,
    SmpsSongHeader,
    SmpsVoice,
)

# Track bytes: durations below the rest (REST, song.py), notes after it to nAs7, flags above.
FIRST_NOTE = 0x81     # nC0
LAST_NOTE = 0xDF      # nAs7
FIRST_FLAG = LAST_NOTE + 1     # $E0: coordination flags from here
NO_ATTACK = 0xE7      # smpsNoAttack
SELECTED_SAMPLE = 0x100   # a drum track's note that plays the sample DAC_SAMPLE chose (no SMPS byte)


def signed_byte(value: int) -> int:
    """A track byte as the driver adds it: two's complement ($F4 = -12)."""
    return value - 0x100 if value > 0x7F else value


class OpKind(Enum):
    LABEL = auto()     # name: a jump / loop / call target, or a channel's start; emits no byte
    NOTE = auto()      # value: its SMPS number: $80 rest, $81 nC0 ... ($E0 B7: past SMPS's bytes), a DAC sample
    DURATION = auto()  # value: ticks
    NO_ATTACK = auto() # smpsNoAttack (value: its byte)
    EFFECT = auto()    # effect: a coordination flag the song keeps as an event
    CALL = auto()      # name: the target
    RETURN = auto()
    LOOP = auto()      # name: the target; value: the play count; index: the counter's slot
    LOOP_EXIT = auto() # name: its loop's target; on the loop's last pass, out past the loop's end
    JUMP = auto()      # name: the target
    STOP = auto()


# What a front end's track bytes read as: what the walk treats as one byte of the track
TRACK_BYTES = frozenset({OpKind.NOTE, OpKind.DURATION, OpKind.NO_ATTACK})


@dataclass(frozen=True)
class Op:
    kind: OpKind
    value: int = 0
    name: str = ""
    effect: SmpsEffect | None = None
    index: int = 0


def track_byte(value: int) -> Op | None:
    """An SMPS track byte as an op: $00-$7F a duration, $80-$DF a rest, note or DAC sample, $E7
    smpsNoAttack; None for any other (a flag's)."""
    if value == NO_ATTACK:
        return Op(OpKind.NO_ATTACK, value=value)
    if value < REST:
        return Op(OpKind.DURATION, value=value)
    if value <= LAST_NOTE:
        return Op(OpKind.NOTE, value=value)
    return None


@dataclass
class SongCode:
    """A song as a front end reads it, before the walk: header, code, voices, and the rules its
    driver plays it by."""

    header: SmpsSongHeader
    code: SmpsCode
    voices: list[SmpsVoice]
    rules: PlaybackRules
    address: int | None = None                                   # a ROM's: the header's
    addresses: dict[str, int] = field(default_factory=dict)      # ... and each label's (voices too)
    dropped: dict[str, int] = field(default_factory=dict)        # flags read and left out, by name
    fm_drums: dict[str, FmDrum] = field(default_factory=dict)    # the drum track's FM programs

    def song(self) -> SmpsSong:
        song = song_from_code(self.header, self.code, self.voices, self.rules)
        song.dropped = dict(self.dropped)
        song.fm_drums = dict(self.fm_drums)
        return song


@dataclass
class SmpsCode:
    """A song's instructions in source order (code falls through from one to the next, as FM5
    into FM1's data) and where each label sits."""

    ops: list[Op]
    labels: dict[str, int] = field(init=False)

    def __post_init__(self):
        self.labels = {op.name: i for i, op in enumerate(self.ops) if op.kind is OpKind.LABEL}


# Flags whose operand is a signed byte
_SIGNED_FLAGS = frozenset({CoordFlag.DETUNE, CoordFlag.ALTER_VOL, CoordFlag.CHANGE_TRANSPOSITION})


def effect_from_bytes(flag: CoordFlag, operands: list[int]) -> SmpsEffect:
    """A flag from its operand bytes, as the song keeps it: signed where the driver adds it as
    signed, smpsPSGvoice's envelope by name."""
    if flag in _SIGNED_FLAGS:
        return effect_of(flag, [signed_byte(b) for b in operands])
    if flag == CoordFlag.PSG_VOICE:
        return PsgVoice(psg_voice_name(operands[0]))
    return effect_of(flag, operands)


def song_from_code(header: SmpsSongHeader, code: SmpsCode, voices: list[SmpsVoice], rules: PlaybackRules) -> SmpsSong:
    """Each of the header's channels walked from its label, by its driver's `rules`.  A DAC
    track's byte without a name (rules.dac_names) is a plain note."""
    pans = {v.index: v.pan for v in voices if v.pan is not None}
    channels = [_Walker(code, ch_header, rules, pans).walk(header.tempo_divider) for ch_header in header.channels]
    song = SmpsSong(header=header, channels=channels, voices=voices, rules=rules)
    apply_run_out(song)
    return song


@dataclass
class _Cursor:
    """A channel's note state as track bytes read and leave it."""
    channel: SmpsChannel
    is_dac: bool
    tempo_div: int
    tick: int
    last_duration: int
    no_attack: bool
    pending: SmpsNote | None     # the note still waiting for its duration
    last_note_value: int         # the last note read, 0 after a rest: what a standalone duration re-keys


def _standalone_note(cur: _Cursor, duration: int) -> SmpsNote:
    """What a duration byte with no note before it plays.

    DAC: SavedDAC re-triggers; after a rest it stays silent.
    FM / PSG: the last note re-keys at its frequency (1-Up: `$03,$03,$06,$06` after nE7, a
    staccato arpeggio), unless smpsNoAttack precedes it: the note rings on (GHZ:
    `smpsNoAttack,$3C` after nF5; PSG skips the volume write, the envelope continues).  After a
    rest there is no frequency (TrackSetRest clears Freq, the PSG's sets it to -1): the track
    rests on (Credits PSG3, `nRst, $24` then 32 bare durations).
    """
    held = SmpsNote(note_value=REST, duration=duration, is_rest=True, is_no_attack=True)
    if cur.is_dac:
        last = next((e.note for e in reversed(cur.channel.events) if e.note is not None), None)
        if last is None or not last.is_dac:
            return held
        return SmpsNote(note_value=last.note_value, duration=duration, is_dac=True, dac_name=last.dac_name)

    if cur.last_note_value == 0:
        return SmpsNote(note_value=REST, duration=duration, is_rest=True, is_no_attack=cur.no_attack)
    if cur.no_attack:
        return held
    return SmpsNote(note_value=cur.last_note_value, duration=duration, is_retrigger=True)


# What a walk hands back to the walk it was called from:
# (tick, last_duration, pending note, last_note_value, tempo divider, smpsNoAttack pending)
_WalkState = tuple[int, int, SmpsNote | None, int, int, bool]


class _Walker:
    """One channel's walk through the song's code."""

    def __init__(self, code: SmpsCode, header: SmpsChannelHeader, rules: PlaybackRules,
                 voice_pans: Mapping[int, int]):
        self._ops = code.ops
        self._dac_names = rules.dac_names
        self._voice_pans = voice_pans     # a voice that stores its B4 byte pans the track it is set on
        self._labels = code.labels
        self._header = header
        self._channel = SmpsChannel(header=header, rules=rules)
        self._is_dac = header.channel_type == ChannelType.DAC

        # Where this channel's walk first reached each label (a jump back to one replays from
        # there): the tick, and the index of the first event after it
        self._label_ticks: dict[str, int] = {}
        self._label_events: dict[str, int] = {}

        self._passes: dict[str, int] = {}          # each loop being replayed (by its body's label): the pass
        self._dac_sample: int | None = None        # DAC_SAMPLE's: what a drum track's SELECTED_SAMPLE plays

    def walk(self, tempo_divider: int) -> SmpsChannel:
        channel = self._channel
        start = self._header.label
        if start not in self._labels:
            raise ValueError(f"{self._header.channel_type} track: its label '{start}' is not in the code")

        # The channel's own start: tick 0 (its loop, if it jumps back here, is taken by tick)
        self._label_ticks.setdefault(start, 0)
        self._walk(self._labels[start] + 1, 0, 0, False, seen={start}, tempo_div=tempo_divider)

        # The loop as a tick and an event index: where THIS channel reached its jump's target.
        # Another channel's walk past the same label (code shared by fall-through or a jump)
        # reaches it at its own tick - Marble Zone's PSG2 4 ticks after PSG1.
        if channel.has_jump and channel.loop_label:
            channel.loop_tick = self._label_ticks.get(channel.loop_label)
            channel.loop_event_index = self._label_events.get(channel.loop_label)
        return channel

    def _loop_end(self, i: int, body: str) -> int | None:
        """The index of the LOOP op from `i` on that replays `body`: the loop a LOOP_EXIT leaves."""
        return next((j for j in range(i, len(self._ops))
                     if self._ops[j].kind is OpKind.LOOP and self._ops[j].name == body), None)

    def _mark_label(self, label: str, tick: int, event_index: int) -> None:
        """The walk reached `label` at `tick`, before event `event_index`; the first time counts."""
        self._label_ticks.setdefault(label, tick)
        self._label_events.setdefault(label, event_index)

    def _label_precedes_duration(self, i: int) -> bool:
        """True if the next byte-bearing op from `i` is a duration: a label between a note and
        its duration byte (labels emit no bytes) leaves the duration the note's."""
        while i < len(self._ops) and self._ops[i].kind is OpKind.LABEL:
            i += 1
        return i < len(self._ops) and self._ops[i].kind is OpKind.DURATION

    def _finalize_pending(self, pending: SmpsNote | None, tick: int, last_duration: int,
                          last_note_value: int) -> tuple[int, int]:
        """Emit a pending note with the saved duration: (tick after it, last_note_value)."""
        if pending is None:
            return tick, last_note_value

        pending.duration = last_duration
        if pending.is_rest:
            last_note_value = 0         # the driver clears the frequency: a bare duration rests on
        elif not pending.is_dac:
            last_note_value = pending.note_value
        self._channel.events.append(SmpsEvent(note=pending, tick_position=tick))
        return tick + pending.duration, last_note_value

    def _walk(self, start: int, tick: int, last_duration: int, no_attack: bool, stop: int | None = None,
              pending: SmpsNote | None = None, last_note_value: int = 0, seen: set[str] | None = None,
              tempo_div: int = 1) -> _WalkState:
        """Walk ops from `start` (to `stop`, a loop body's end), appending events.

        pending: a note still waiting for a duration byte (it may follow on the next dc.b line).
        last_note_value: the last note sounded, which a standalone duration re-keys.
        tempo_div: smpsChanTempoDiv; durations are multiplied by it here, so ticks compare
        across channels with different dividers.
        """
        seen = set() if seen is None else seen
        channel = self._channel
        i = start
        while i < len(self._ops):
            if stop is not None and i >= stop:
                return tick, last_duration, pending, last_note_value, tempo_div, no_attack

            op = self._ops[i]
            i += 1

            # A label records its tick.  A note still pending is finished first, so the label
            # takes the tick after it - unless the next byte is a duration, which a label (no
            # bytes) cannot separate from its note:  SndA3 - Death: nAb3 / label / dc.b $01
            if op.kind is OpKind.LABEL:
                seen.add(op.name)
                if pending is not None and self._label_precedes_duration(i):
                    self._mark_label(op.name, tick, len(channel.events) + 1)   # after the pending note
                    continue
                tick, last_note_value = self._finalize_pending(pending, tick, last_duration, last_note_value)
                pending = None
                self._mark_label(op.name, tick, len(channel.events))
                continue

            # Every other op but a track byte completes a pending note with the saved duration:
            # FMDoNext reads a non-duration byte after a note and puts it back
            if op.kind not in TRACK_BYTES:
                tick, last_note_value = self._finalize_pending(pending, tick, last_duration, last_note_value)
                pending = None

            if op.kind is OpKind.STOP:
                return tick, last_duration, None, last_note_value, tempo_div, no_attack

            if op.kind is OpKind.RETURN:      # only reached while a call is inlined
                return tick, last_duration, None, last_note_value, tempo_div, no_attack

            if op.kind is OpKind.JUMP:
                # Back to code this channel walked (or an unknown target): the loop, the end
                if op.name in seen or op.name not in self._labels:
                    channel.has_jump = True
                    channel.loop_label = op.name
                    return tick, last_duration, None, last_note_value, tempo_div, no_attack

                # Forward into code not walked yet: followed, and the label is reached here, now -
                # a later jump back to it loops from this tick (Labyrinth FM4 into FM3's code)
                seen.add(op.name)
                self._mark_label(op.name, tick, len(channel.events))
                return self._walk(self._labels[op.name] + 1, tick, last_duration, no_attack,
                                  last_note_value=last_note_value, seen=seen, tempo_div=tempo_div)

            if op.kind is OpKind.LOOP:
                # The first pass is behind; replay the body (target to here) count - 1 more times
                if op.name not in self._labels:
                    continue
                body = self._labels[op.name] + 1
                for repeat in range(2, op.value + 1):
                    self._passes[op.name] = repeat
                    tick, last_duration, body_pending, last_note_value, tempo_div, no_attack = self._walk(
                        body, tick, last_duration, no_attack, stop=i - 1,
                        last_note_value=last_note_value, seen=seen, tempo_div=tempo_div)
                    tick, last_note_value = self._finalize_pending(body_pending, tick, last_duration, last_note_value)
                self._passes.pop(op.name, None)
                continue

            if op.kind is OpKind.LOOP_EXIT:
                # On its loop's last pass: out past the loop's end, a tie dropped (Streets of Rage's $FE)
                end = self._loop_end(i, op.name)
                if end is None or self._passes.get(op.name, 1) < self._ops[end].value:
                    continue
                no_attack = False
                if op.name in self._passes:                 # the last replay ends here
                    return tick, last_duration, None, last_note_value, tempo_div, no_attack
                i = end + 1                                 # a loop of one pass: past its end
                continue

            if op.kind is OpKind.CALL:
                if op.name not in self._labels:
                    continue
                tick, last_duration, pending, last_note_value, tempo_div, no_attack = self._walk(
                    self._labels[op.name] + 1, tick, last_duration, no_attack,
                    last_note_value=last_note_value, tempo_div=tempo_div)
                continue

            if op.kind is OpKind.EFFECT:
                assert op.effect is not None
                tempo_div = self._effect(op.effect, tick, tempo_div)
                continue

            # A track byte; the cursor carries a pending note across ops (and dc.b lines)
            cur = _Cursor(channel, self._is_dac, tempo_div, tick, last_duration, no_attack, pending,
                          last_note_value)
            self._byte(cur, op)
            tick, last_duration, no_attack, pending, last_note_value = (
                cur.tick, cur.last_duration, cur.no_attack, cur.pending, cur.last_note_value)

        # The end of the code: the pending note still plays
        tick, last_note_value = self._finalize_pending(pending, tick, last_duration, last_note_value)
        return tick, last_duration, None, last_note_value, tempo_div, no_attack

    def _effect(self, effect: SmpsEffect, tick: int, tempo_div: int) -> int:
        """Keep a flag as an event; the tempo divider it leaves.

        smpsChanTempoDiv also scales the durations that follow.  Its event is kept so the
        converter's smpsSetTempoDiv re-timing knows which divider each note was read with.  No
        flag's parameter is a duration to scale: smpsNoteFill and smpsModSet count V-int frames.
        """
        if isinstance(effect, ChanTempoDiv):
            tempo_div = effect.divider
        if isinstance(effect, SelectSample):
            self._dac_sample = effect.sound
        self._channel.events.append(SmpsEvent(effect=effect, tick_position=tick))

        # A voice with its own pan byte: the driver writes B4 as it loads the voice
        pan = self._voice_pans.get(effect.index) if isinstance(effect, SetVoice) else None
        if pan is not None:
            self._channel.events.append(SmpsEvent(effect=Pan(pan), tick_position=tick))
        return tempo_div

    def _byte(self, cur: _Cursor, op: Op) -> None:
        """One track byte: smpsNoAttack, a duration, or a note / rest / DAC sample."""
        if op.kind is OpKind.NO_ATTACK:
            cur.no_attack = True
        elif op.kind is OpKind.DURATION:
            self._duration(cur, op.value * cur.tempo_div)
        else:
            self._note(cur, op.value)

    def _note(self, cur: _Cursor, val: int) -> None:
        """A rest, note or (DAC channel) sample; SELECTED_SAMPLE the one DAC_SAMPLE chose (none yet:
        the driver plays nothing)."""
        if val == SELECTED_SAMPLE:
            val = REST if self._dac_sample is None else self._dac_sample
        if val == REST:
            note = SmpsNote(note_value=val, duration=0, is_rest=True, is_no_attack=cur.no_attack)
        elif cur.is_dac and val in self._dac_names:
            note = SmpsNote(note_value=val, duration=0, is_dac=True,
                            dac_name=self._dac_names[val], is_no_attack=cur.no_attack)
        else:
            note = SmpsNote(note_value=val, duration=0, is_no_attack=cur.no_attack)
        self._open_note(cur, note)

    def _duration(self, cur: _Cursor, duration: int) -> None:
        """A duration (already scaled by the tempo divider): the pending note's, or a note of its own."""
        cur.last_duration = duration
        if cur.pending is not None:
            self._close_pending(cur)
            return

        cur.channel.events.append(SmpsEvent(note=_standalone_note(cur, duration), tick_position=cur.tick))
        cur.tick += duration
        cur.no_attack = False           # every read clears it (the bclr before FMDoNext / PSGDoNext)

    def _open_note(self, cur: _Cursor, note: SmpsNote | None) -> None:
        """Finalize the pending note; `note` (None: nothing) waits for its duration next."""
        self._close_pending(cur)
        cur.pending = note
        cur.no_attack = False

    def _close_pending(self, cur: _Cursor) -> None:
        """Emit the pending note with the last duration."""
        cur.tick, cur.last_note_value = self._finalize_pending(cur.pending, cur.tick, cur.last_duration,
                                                               cur.last_note_value)
        cur.pending = None
