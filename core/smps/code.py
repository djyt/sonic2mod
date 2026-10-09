"""A song's track code as the driver reads it, and the one walk that turns it into events.

Two front ends read a song into the same instructions; the driver's reading rules live here once:

    .asm   SmpsParser (macros, dc.b)  ──┐
                                        ├──> SmpsCode ──> song_from_code ──> SmpsSong
    ROM    core.rom (bytes)           ──┘

The rules: a note waits for the duration byte after it (a flag or a label completes it with the
saved one); a duration with no note re-keys the last note; smpsNoAttack marks the next read;
smpsLoop is unrolled, smpsCall inlined, smpsJump ends the channel (a loop) or is followed (a
forward jump into code not yet walked); a channel's loop starts where ITS walk reached the target.
A driver's own volume steps, detune adds and gate are resolved here, by the song's PlaybackRules
(effects.py); so are its jump's tie and its noise's pitch.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, auto

from .driver_tables import psg_voice_name
from .effects import (
    AlterVol,
    AlterVolumeStep,
    ChanTempoDiv,
    CoordFlag,
    Detune,
    DetuneAdd,
    Gate,
    Pan,
    PsgForm,
    PsgVoice,
    SelectSample,
    SetVoice,
    SetVol,
    SmpsEffect,
    VolumeStep,
    effect_of,
)
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
from .voice_patch import apply_voice_patches

# Track bytes: durations below the rest (REST, song.py), notes after it to nAs7, flags above.
FIRST_NOTE = 0x81     # nC0
LAST_NOTE = 0xDF      # nAs7
FIRST_FLAG = LAST_NOTE + 1     # $E0: coordination flags from here
NO_ATTACK = 0xE7      # smpsNoAttack
SELECTED_SAMPLE = 0x100   # a drum track's note that plays the sample DAC_SAMPLE chose (no SMPS byte)
MAX_PSG = 0xC6        # nMaxPSG: the PSG table's last entry, divider 0 (the chip clocks it as 1)

_BYTE, _WORD = 0x100, 0x10000


def signed_byte(value: int) -> int:
    """A track byte as the driver adds it: two's complement ($F4 = -12)."""
    return value - 0x100 if value > 0x7F else value


def _signed_word(value: int) -> int:
    """A sum kept in a 16-bit word (add.w), two's complement."""
    return (value + _WORD // 2) % _WORD - _WORD // 2


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
_SIGNED_FLAGS = frozenset({CoordFlag.DETUNE, CoordFlag.ALTER_VOL, CoordFlag.CHANGE_TRANSPOSITION,
                           CoordFlag.ALTER_VOLUME_STEP})


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
    apply_voice_patches(song)
    return song


@dataclass
class _Cursor:
    """One channel's walk state as the code leaves it; a nested walk (a call, a loop's replay, a
    forward jump) carries on with the same one."""
    channel: SmpsChannel
    is_dac: bool
    tempo_div: int
    tick: int = 0
    last_duration: int = 0
    no_attack: bool = False
    pending: SmpsNote | None = None     # the note still waiting for its duration
    last_note_value: int = 0            # the last note read, 0 after a rest: what a standalone duration re-keys


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


class _Walker:
    """One channel's walk through the song's code.

        _walk          op by op from an index: a label marks its tick, any other op but a track
                       byte completes a pending note, then the op's handler (_on_*) says where next
        _on_*          the index to go on from, or None: this walk ends (STOP, RETURN, a loop's
                       jump back, a forward jump walked to its end, a loop's last pass left)
    """

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

        # The driver's track state the rules resolve effects with (rules.py)
        self._rules = rules
        self._track = rules.track(header.channel_type)
        self._volume_step = 0                      # the RAM starts cleared
        self._level: int | None = None             # the level a volume step set, as the driver keeps it
        self._detune_word = 0
        self._gate = 0                             # frames before a note's end the driver keys it off
        self._noise = False                        # a PSG_FORM ran: notes are noise
        self._tone_note: int | None = None         # the last tone note: what tone 3 still holds

        # A jump back's tie: labels first reached with a tie pending that their first note keeps
        # (none read since), those since the last note, and the tie the jump left
        self._tied_labels: set[str] = set()
        self._open_labels: list[str] = []
        self._jump_tie = False

        self._handlers = {
            OpKind.STOP: self._on_end, OpKind.RETURN: self._on_end,    # RETURN: only reached in a call
            OpKind.JUMP: self._on_jump, OpKind.LOOP: self._on_loop, OpKind.LOOP_EXIT: self._on_loop_exit,
            OpKind.CALL: self._on_call, OpKind.EFFECT: self._on_effect,
            OpKind.NOTE: self._on_byte, OpKind.DURATION: self._on_byte, OpKind.NO_ATTACK: self._on_byte,
        }

    def walk(self, tempo_divider: int) -> SmpsChannel:
        channel = self._channel
        start = self._header.label
        if start not in self._labels:
            raise ValueError(f"{self._header.channel_type} track: its label '{start}' is not in the code")

        # The channel's own start: tick 0 (its loop, if it jumps back here, is taken by tick)
        self._label_ticks.setdefault(start, 0)
        cur = _Cursor(channel, self._is_dac, tempo_divider)
        self._walk(self._labels[start] + 1, cur, seen={start})

        # The loop as a tick and an event index: where THIS channel reached its jump's target.
        # Another channel's walk past the same label (code shared by fall-through or a jump)
        # reaches it at its own tick - Marble Zone's PSG2 4 ticks after PSG1.
        if channel.has_jump and channel.loop_label:
            channel.loop_tick = self._label_ticks.get(channel.loop_label)
            channel.loop_event_index = self._label_events.get(channel.loop_label)
            channel.replay_tie = self._replay_tie(channel.loop_label)
        return channel

    def _replay_tie(self, label: str) -> bool | None:
        """A replay's first note: tied by a tie the jump leaves; attacking where the first pass
        took its tie from the code before `label`, which a replay does not run; else None."""
        if self._jump_tie:
            return True
        return False if label in self._tied_labels else None

    def _walk(self, start: int, cur: _Cursor, seen: set[str], stop: int | None = None) -> None:
        """Walk ops from `start` (to `stop`, a loop body's end), appending events.  A note still
        pending at `stop` is left for the caller (the next pass may give it its duration)."""
        i = start
        while i < len(self._ops):
            if stop is not None and i >= stop:
                return

            op = self._ops[i]
            i += 1
            if op.kind is OpKind.LABEL:
                self._on_label(op, i, cur, seen)
                continue

            # Every other op but a track byte completes a pending note with the saved duration:
            # FMDoNext reads a non-duration byte after a note and puts it back
            if op.kind not in TRACK_BYTES:
                self._close_pending(cur)
            after = self._handlers[op.kind](op, i, cur, seen)
            if after is None:
                return
            i = after

        # The end of the code: the pending note still plays
        self._close_pending(cur)

    # --- the ops ---------------------------------------------------------------------------

    def _on_label(self, op: Op, i: int, cur: _Cursor, seen: set[str]) -> None:
        """A label records its tick.  A note still pending is finished first, so the label takes
        the tick after it - unless the next byte is a duration, which a label (no bytes) cannot
        separate from its note:  SndA3 - Death: nAb3 / label / dc.b $01"""
        seen.add(op.name)
        if cur.pending is not None and self._label_precedes_duration(i):
            self._mark_label(op.name, cur, len(cur.channel.events) + 1)   # after the pending note
            return
        self._close_pending(cur)
        self._mark_label(op.name, cur, len(cur.channel.events))

    def _on_end(self, op: Op, i: int, cur: _Cursor, seen: set[str]) -> int | None:
        return None

    def _on_jump(self, op: Op, i: int, cur: _Cursor, seen: set[str]) -> int | None:
        if self._track.jump_clears_tie:
            cur.no_attack = False

        # Back to code this channel walked (or an unknown target): the loop, the end
        if op.name in seen or op.name not in self._labels:
            cur.channel.has_jump = True
            cur.channel.loop_label = op.name
            self._jump_tie = cur.no_attack
            return None

        # Forward into code not walked yet: followed, and the label is reached here, now - a later
        # jump back to it loops from this tick (Labyrinth FM4 into FM3's code)
        seen.add(op.name)
        self._mark_label(op.name, cur, len(cur.channel.events))
        self._walk(self._labels[op.name] + 1, cur, seen)
        return None

    def _on_loop(self, op: Op, i: int, cur: _Cursor, seen: set[str]) -> int | None:
        """The first pass is behind; replay the body (target to here) count - 1 more times."""
        if op.name not in self._labels:
            return i
        body = self._labels[op.name] + 1
        for repeat in range(2, op.value + 1):
            self._passes[op.name] = repeat
            self._walk(body, cur, seen, stop=i - 1)
            self._close_pending(cur)
        self._passes.pop(op.name, None)
        return i

    def _on_loop_exit(self, op: Op, i: int, cur: _Cursor, seen: set[str]) -> int | None:
        """On its loop's last pass: out past the loop's end, a tie dropped (Streets of Rage's $FE)."""
        end = self._loop_end(i, op.name)
        if end is None or self._passes.get(op.name, 1) < self._ops[end].value:
            return i
        cur.no_attack = False
        if op.name in self._passes:                 # the last replay ends here
            return None
        return end + 1                              # a loop of one pass: past its end

    def _on_call(self, op: Op, i: int, cur: _Cursor, seen: set[str]) -> int | None:
        if op.name in self._labels:
            self._walk(self._labels[op.name] + 1, cur, seen=set())
        return i

    def _on_effect(self, op: Op, i: int, cur: _Cursor, seen: set[str]) -> int | None:
        assert op.effect is not None
        cur.tempo_div = self._effect(op.effect, cur.tick, cur.tempo_div)
        return i

    def _on_byte(self, op: Op, i: int, cur: _Cursor, seen: set[str]) -> int | None:
        """A track byte; the cursor carries a pending note across ops (and dc.b lines)."""
        self._byte(cur, op)
        if op.kind is OpKind.DURATION:
            self._cut(cur, i)
        return i

    # --- helpers ---------------------------------------------------------------------------

    def _loop_end(self, i: int, body: str) -> int | None:
        """The index of the LOOP op from `i` on that replays `body`: the loop a LOOP_EXIT leaves."""
        return next((j for j in range(i, len(self._ops))
                     if self._ops[j].kind is OpKind.LOOP and self._ops[j].name == body), None)

    def _mark_label(self, label: str, cur: _Cursor, event_index: int) -> None:
        """The walk reached `label` at the cursor's tick, before event `event_index`; the first
        time counts."""
        if label in self._label_ticks:
            return
        self._label_ticks[label] = cur.tick
        self._label_events[label] = event_index
        self._open_labels.append(label)
        if cur.no_attack:
            self._tied_labels.add(label)

    def _cut(self, cur: _Cursor, i: int) -> None:
        """The note or rest a duration just completed, as the driver keys it off: a gated note at
        its gate, a rest after a tie where the rules say (TrackRules.tied_rest_holds)."""
        event = cur.channel.events[-1]
        note = event.note
        if note is None:
            return
        if not note.is_rest:
            if self._gate and self._gated(note, i):
                self._split(cur, event, note.duration - self._gate, note)
            return
        holds = self._track.tied_rest_holds
        if note.is_no_attack and holds is not None and holds < note.duration:
            self._split(cur, event, holds, note)

    def _gated(self, note: SmpsNote, i: int) -> bool:
        """The gate keys `note` off: it outlasts the gate, and is not one the driver spares - a
        tied note where the key-off waits on the tie, one the next byte ties where the driver
        looks (it checks each frame; labels are no bytes)."""
        if note.duration <= self._gate:
            return False
        if note.is_no_attack and self._track.gate_spares_tied:
            return False
        return not (self._track.gate_sees_tie and self._next_op(i) is OpKind.NO_ATTACK)

    @staticmethod
    def _split(cur: _Cursor, event: SmpsEvent, held: int, note: SmpsNote) -> None:
        """`event` keyed off after `held` ticks (0: at once): what is left of it a rest; both cut."""
        rest = SmpsNote(note_value=REST, duration=note.duration - held, is_rest=True, cut=True)
        if not held:
            event.note = rest
            return
        event.note = dataclasses.replace(note, duration=held, cut=True)
        cur.channel.events.append(SmpsEvent(note=rest, tick_position=event.tick_position + held))

    def _next_op(self, i: int) -> OpKind | None:
        """The kind of the first op from `i` that is not a label."""
        while i < len(self._ops) and self._ops[i].kind is OpKind.LABEL:
            i += 1
        return self._ops[i].kind if i < len(self._ops) else None

    def _label_precedes_duration(self, i: int) -> bool:
        """True if the next byte-bearing op from `i` is a duration: a label between a note and
        its duration byte (labels emit no bytes) leaves the duration the note's."""
        return self._next_op(i) is OpKind.DURATION

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
        resolved = self._resolved(effect)
        if resolved is None:
            return tempo_div
        self._channel.events.append(SmpsEvent(effect=resolved, tick_position=tick))

        # A voice with its own pan byte: the driver writes B4 as it loads the voice
        pan = self._voice_pans.get(effect.index) if isinstance(effect, SetVoice) else None
        if pan is not None:
            self._channel.events.append(SmpsEvent(effect=Pan(pan), tick_position=tick))
        return tempo_div

    def _resolved(self, effect: SmpsEffect) -> SmpsEffect | None:
        """`effect` as the track plays it: a volume step a level (an AlterVol then moves that level as
        the driver keeps it, unclamped), a detune add the detune; None: the walk applies it (a
        gate, to the notes)."""
        match effect:
            case Gate(frames=frames):
                self._gate = frames
                return None
            case VolumeStep(step=step):
                return self._volume(signed_byte(step % _BYTE))
            case AlterVolumeStep(delta=delta):
                return self._volume(signed_byte((self._volume_step + delta) % _BYTE))
            case Detune(offset=offset):
                return self._detune(offset)
            case DetuneAdd(offset=offset):
                return self._detune(self._detune_word + offset)
            case AlterVol(delta=delta) if self._level is not None:
                self._level = signed_byte((self._level + delta) % _BYTE)
                return SetVol(self._level)
            case PsgForm():
                self._noise = True
        return effect

    def _volume(self, step: int) -> SetVol:
        """Volume step `step`: its level in the driver's table, the header volume added (add.b)."""
        level = self._track.volume_steps.get(step)
        if level is None:
            raise ValueError(f"{self._header.channel_type} track '{self._header.label}': volume step "
                             f"{step}, which its driver has no level for")
        self._volume_step = step
        self._level = signed_byte((level + self._header.volume) % _BYTE)
        return SetVol(self._level)

    def _detune(self, word: int) -> Detune:
        """The detune word `word` (add.w) as the track adds it: the PSG's shifted to a divider."""
        self._detune_word = _signed_word(word)
        return Detune(self._detune_word >> self._track.detune_shift)

    def _byte(self, cur: _Cursor, op: Op) -> None:
        """One track byte: smpsNoAttack, a duration, or a note / rest / DAC sample."""
        if op.kind is OpKind.NO_ATTACK:
            cur.no_attack = True
            self._tied_labels.difference_update(self._open_labels)      # their first note ties itself
        elif op.kind is OpKind.DURATION:
            self._duration(cur, op.value * cur.tempo_div)
        else:
            self._note(cur, op.value)

    def _note(self, cur: _Cursor, val: int) -> None:
        """A rest, note or (DAC channel) sample; SELECTED_SAMPLE the one DAC_SAMPLE chose (none yet:
        the driver plays nothing)."""
        if val == SELECTED_SAMPLE:
            val = REST if self._dac_sample is None else self._dac_sample
        if val != REST and self._header.channel_type == ChannelType.PSG:
            val = self._psg_note(val)
        if val == REST:
            note = SmpsNote(note_value=val, duration=0, is_rest=True, is_no_attack=cur.no_attack)
        elif cur.is_dac and val in self._dac_names:
            note = SmpsNote(note_value=val, duration=0, is_dac=True,
                            dac_name=self._dac_names[val], is_no_attack=cur.no_attack)
        else:
            note = SmpsNote(note_value=val, duration=0, is_no_attack=cur.no_attack)
        self._open_note(cur, note)

    def _psg_note(self, val: int) -> int:
        """A PSG note as its divider plays: a noise note whose driver leaves tone 3 alone plays
        the last tone note's (none: nMaxPSG)."""
        if not self._noise:
            self._tone_note = val
            return val
        if self._track.noise_writes_tone3:
            return val
        return MAX_PSG if self._tone_note is None else self._tone_note

    def _duration(self, cur: _Cursor, duration: int) -> None:
        """A duration (already scaled by the tempo divider): the pending note's, or a note of its own."""
        cur.last_duration = duration
        if cur.pending is not None:
            self._close_pending(cur)
            return

        cur.channel.events.append(SmpsEvent(note=_standalone_note(cur, duration), tick_position=cur.tick))
        cur.tick += duration
        cur.no_attack = False           # every read clears it (the bclr before FMDoNext / PSGDoNext)
        self._open_labels.clear()

    def _open_note(self, cur: _Cursor, note: SmpsNote | None) -> None:
        """Finalize the pending note; `note` (None: nothing) waits for its duration next."""
        self._close_pending(cur)
        cur.pending = note
        cur.no_attack = False
        self._open_labels.clear()

    def _close_pending(self, cur: _Cursor) -> None:
        """Emit the pending note (if any) with the last duration."""
        pending = cur.pending
        if pending is None:
            return
        pending.duration = cur.last_duration
        if pending.is_rest:
            cur.last_note_value = 0         # the driver clears the frequency: a bare duration rests on
        elif not pending.is_dac:
            cur.last_note_value = pending.note_value
        cur.channel.events.append(SmpsEvent(note=pending, tick_position=cur.tick))
        cur.tick += pending.duration
        cur.pending = None
