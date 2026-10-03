"""Where notes start in a log, and what each channel sounds when: read from the chip replay.

The chips have no notes, so these are rules, each the driver's behaviour seen from the chips:

    FM     a key-on starts a note, unless the channel is keyed already within `mod_cents` of the
           pitch it was keyed at: the Sonic 1 driver keys on again under smpsNoAttack (it only
           skips the key-off), so `nA5, $10, smpsNoAttack, $3B` logs a second key-on the chip
           ignores - a tie.  A tie with a new smpsDetune is still a tie (Scrap Brain FM4 scoops
           every phrase start up 36 cents); a key-on at another note is a legato note.
    PSG    the channel becoming audible, or its period leaving the note's starting pitch by more
           than `mod_cents`: smaller moves are smpsModSet vibrato rewriting the divider.
    NOISE  the noise channel becoming audible.
    DAC    a PCM seek (a sample starts; some loggers merge back-to-back restarts of one sample).

note_starts lists them; pitch_segments is the other view, every channel's sounding pitch as a
timeline of change points.  The lift's notes (Phase 1) will refine these per frame.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import NamedTuple

from ..audio import db_to_gain
from ..smps import fm_level_db, psg_level_db
from .chipstate import FM_CHANNELS, NOISE_CHANNEL, PSG_SILENT, PSG_TONE_CHANNELS, Change, ChangeKind, ChipState
from .reader import VGM_SAMPLE_RATE, VgmLog

FM_NAMES = tuple(f"FM{ch + 1}" for ch in range(FM_CHANNELS))
PSG_NAMES = (*(f"PSG{ch + 1}" for ch in range(PSG_TONE_CHANNELS)), "NOISE")
DAC_NAME = "DAC"
DEFAULT_MOD_CENTS = 70.0        # pitch moves within this of a note's start are modulation
_TIE_CENTS_FLOOR = 1e-9         # mod_cents 0 still counts a key-on at the very same pitch a tie
_CENTS_PER_OCTAVE = 1200.0

Segment = tuple[float, float | None]          # (seconds, Hz sounding from then; None = silent)


class NoteStart(NamedTuple):
    sample: int
    channel: str        # FM1..FM6, PSG1..PSG3, NOISE, DAC
    data: int           # FM: FNUM; PSG: period; NOISE: noise register; DAC: PCM bank offset
    block: int          # FM: block; otherwise 0
    hz: float           # FM / PSG: pitch; NOISE: the LFSR's shift rate; DAC: 0
    gain: float         # linear level at the start: FM carriers summed, PSG attenuation; DAC: 0
    tone2: int = 0      # NOISE: tone 2's period, which a rate-3 LFSR follows

    @property
    def ms(self) -> float:
        return self.sample * 1000.0 / VGM_SAMPLE_RATE

    @property
    def chip(self) -> str:
        """'fm', 'psg' or 'dac'."""
        if self.channel in FM_NAMES:
            return "fm"
        return "dac" if self.channel == DAC_NAME else "psg"


def _cents_apart(a: float, b: float) -> float:
    return abs(_CENTS_PER_OCTAVE * math.log2(a / b))


class NoteTracker:
    """Note starts from a replay's changes, fed one at a time (the rules above)."""

    def __init__(self, state: ChipState, mod_cents: float = DEFAULT_MOD_CENTS) -> None:
        self._state = state
        self._mod_cents = mod_cents
        self._fm_keyed = [False] * FM_CHANNELS
        self._fm_keyed_hz = [0.0] * FM_CHANNELS
        self._psg_start_period = [0] * PSG_TONE_CHANNELS      # period the sounding note started on

    def feed(self, change: Change) -> NoteStart | None:
        kind, ch = change.kind, change.channel
        if kind is ChangeKind.FM_KEY:
            return self._fm_key(change)
        if kind is ChangeKind.PSG_VOLUME:
            # Silent -> audible
            return self._psg_start(change.sample, ch) if change.previous == PSG_SILENT and change.value < PSG_SILENT else None
        if kind is ChangeKind.PSG_TONE and self._psg_moved(ch):
            return self._psg_start(change.sample, ch)
        if kind is ChangeKind.PCM_SEEK:
            return NoteStart(change.sample, DAC_NAME, change.value, 0, 0.0, 0.0)
        return None

    # ---- FM ----

    def _fm_key(self, change: Change) -> NoteStart | None:
        ch, state = change.channel, self._state
        if not change.value:
            self._fm_keyed[ch] = False
            return None

        hz, was = state.fm_hz(ch), self._fm_keyed_hz[ch]
        if self._fm_keyed[ch] and hz > 0 and was > 0 and _cents_apart(hz, was) <= max(self._mod_cents, _TIE_CENTS_FLOOR):
            return None
        self._fm_keyed[ch], self._fm_keyed_hz[ch] = True, hz

        fnum, block = state.fm_fnum_block(ch)
        if fnum == 0:
            return None
        gain = sum(db_to_gain(fm_level_db(tl)) for tl in state.fm_carrier_tls(ch))
        return NoteStart(change.sample, FM_NAMES[ch], fnum, block, hz, gain)

    # ---- SN76489 ----

    def _psg_moved(self, ch: int) -> bool:
        """Audible, and the period has left the sounding note's (see mod_cents)."""
        new, old = self._state.psg_period(ch), self._psg_start_period[ch]
        if not self._state.psg_audible(ch) or new == old:
            return False
        if new <= 0 or old <= 0 or self._mod_cents <= 0:
            return True
        return _cents_apart(new, old) > self._mod_cents

    def _psg_start(self, sample: int, ch: int) -> NoteStart:
        state = self._state
        gain = db_to_gain(psg_level_db(state.psg_attenuation(ch)))
        if ch == NOISE_CHANNEL:
            return NoteStart(sample, PSG_NAMES[ch], state.noise, 0, state.noise_shift_hz(), gain,
                             state.psg_period(2))
        period = state.psg_period(ch)
        self._psg_start_period[ch] = period
        return NoteStart(sample, PSG_NAMES[ch], period, 0, state.psg_hz(ch), gain)


def note_starts(log: VgmLog, state: ChipState | None = None, mod_cents: float = DEFAULT_MOD_CENTS) -> list[NoteStart]:
    """Every note start in `log`, in order (`state`: the chips to replay into; the log's clocks by default)."""
    state = state or ChipState.for_log(log)
    tracker = NoteTracker(state, mod_cents)
    return [n for n in map(tracker.feed, state.replay(log, dac=False)) if n is not None]


def pitch_segments(log: VgmLog, state: ChipState | None = None) -> tuple[dict[str, list[Segment]], float]:
    """Per FM / PSG tone channel, (seconds, Hz | None) at every change of what it sounds; and the
    log's length.  FM: a point at every key and frequency write.  PSG: only where the pitch or the
    audibility changes, not at every volume write - an envelope stepping every frame (Labyrinth
    Zone's fTone_09) would chop a 120 ms note into 17 ms slivers."""
    state = state or ChipState.for_log(log)
    out: dict[str, list[Segment]] = defaultdict(list)
    psg_last: list[float | None] = [None] * PSG_TONE_CHANNELS

    for change in state.replay(log, dac=False):
        t, ch = change.sample / VGM_SAMPLE_RATE, change.channel
        if change.kind in (ChangeKind.FM_KEY, ChangeKind.FM_FREQUENCY):
            hz = state.fm_hz(ch)
            out[FM_NAMES[ch]].append((t, hz if state.fm_slots(ch) and hz > 0 else None))
            continue
        if change.kind not in (ChangeKind.PSG_TONE, ChangeKind.PSG_VOLUME) or ch >= PSG_TONE_CHANNELS:
            continue

        f = state.psg_hz(ch) if state.psg_period(ch) > 0 and state.psg_audible(ch) else None
        if f == psg_last[ch]:
            continue
        psg_last[ch] = f
        out[PSG_NAMES[ch]].append((t, f))
    return out, log.seconds
