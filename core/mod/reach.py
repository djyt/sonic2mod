"""How far into each sample a written MOD is heard: ProTracker's replay of the file, tick by tick.

The converter cuts each one-shot sample there (ModFile.trim_tails, settings.yaml
`samples.trim_tails`): its own estimates of how long a sample must ring (core.convert.sustain_plan,
the mixer's `heard`, a bank member's region) allow for the row grid and a release padding, and the
written cells are what a player reads.

A channel's sample advances amiga_clock / period bytes a second for as long as it plays; its bytes
count as heard while its volume is above 0 (a C00, an ECx or a slide to 0 silences it; a later
Cxx would bring it back, so the position keeps advancing).  A looping song is played once more
from its loop target, so a note sounding across the loop is followed through it.  A note still
heard at the end may be heard to its sample's end, and that sample is reported None: the last
notes of a song that stops, or one that has rung through the whole second pass (one that started
in it is the first pass's counterpart, already followed through the loop).

Every step is taken SPEED_MARGIN faster than nominal, a vibrato row at its swing's highest pitch,
and the note an EDx note is about to replace at the faster of the two periods (ptplayer writes the
new one to Paula on the delay tick, a replayer that writes it at once would not), so the cut errs
long: finetune as 2^(-finetune/96) against ProTracker's own tables, an
NTSC machine's Paula clock (+0.9 %), a player's DMA start.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .file import ModImage, SampleInfo
from .timing import DEFAULT_BPM, DEFAULT_SPEED, TICK_SECS_AT_1_BPM

SPEED_MARGIN = 1.03         # every sample assumed to play this much faster than nominal
_VIBRATO_SWING = 2          # a 4xy swings at most 2 x depth periods (ProTracker: sine 255 x y >> 7)
_MIN_PERIOD, _MAX_PERIOD = 113, 856     # ProTracker clamps a slid period to B-3..C-1
_OFFSET_UNIT = 256          # 9xx: xx x 256 bytes
_FIRST_BPM = 0x20           # Fxx below this sets the speed

# The effects the replay models, by command; an E command by its sub-command.  Anything else could
# move a note's pitch or time in a way it does not follow, and the cut would be a guess.
#   0 (no parameter) none   3 tone portamento   4 vibrato   8 no-op (ptplayer: a trigger)
#   9 sample offset         A volume slide      B position jump   C volume   D pattern break   F speed
#   E0 filter   E1 / E2 fine slides   E8 no-op (ptplayer: _mt_E8Trigger)   EA / EB fine volume
#   EC note cut   ED note delay
_EFFECTS = frozenset({0x3, 0x4, 0x8, 0x9, 0xA, 0xB, 0xC, 0xD, 0xF})
_EXTENDED = frozenset({0x0, 0x1, 0x2, 0x8, 0xA, 0xB, 0xC, 0xD})


class UnmodelledEffect(ValueError):
    """A cell holds an effect heard_bytes does not model."""


@dataclass
class _Voice:
    instrument: int = 0         # the instrument a note without one plays (1-based; 0: none yet)
    sample: int = 0             # the sample sounding (0: none)
    pos: float = 0.0            # its play position, bytes
    period: float = 0.0
    target: float = 0.0         # 3xx's target period
    porta: int = 0              # 3xx's speed (memory)
    depth: int = 0              # 4xy's depth (memory)
    offset: int = 0             # 9xx's offset in bytes (memory)
    volume: int = 0
    started: int = 0            # the index of the row its note started on


def heard_bytes(mod: ModImage, amiga_clock: float) -> dict[int, int | None]:
    """{slot: bytes of its sample a note is heard to} for every one-shot sample a note plays; None
    for one a note is still heard at the end (the module docstring).  Looped samples are left
    out: they sound for as long as their notes.  Raises UnmodelledEffect for an effect the replay
    does not model."""
    rows, loop_at = mod.play_order()
    first_pass = len(rows)
    if loop_at is not None:
        rows = rows + rows[loop_at:]            # the loop played again: notes ringing across it
    voices = [_Voice() for _ in range(mod.channels)]
    reach: dict[int, float | None] = {}             # {slot: furthest byte heard}
    speed, bpm = DEFAULT_SPEED, DEFAULT_BPM

    for at, (pos, row) in enumerate(rows):
        cells = mod.patterns[mod.order[pos]][row]
        for _period, _ins, eff, par in cells:
            if eff == 0xF and par:
                bpm, speed = (par, speed) if par >= _FIRST_BPM else (bpm, par)
        step_secs = TICK_SECS_AT_1_BPM / bpm * SPEED_MARGIN
        for v, cell in zip(voices, cells, strict=True):
            _check(cell)
            if _play_row(v, cell, mod.samples, speed, step_secs, amiga_clock, reach):
                v.started = at

    for v in voices:                            # still heard at the end: all of it may be
        if (v.sample and v.volume and v.pos < mod.samples[v.sample - 1].length
                and (loop_at is None or v.started < first_pass)):
            reach[v.sample] = None
    return {slot: (None if n is None else math.ceil(n)) for slot, n in reach.items()
            if not mod.samples[slot - 1].looped}


def _check(cell) -> None:
    _period, _ins, eff, par = cell
    if eff == 0 and par == 0:
        return
    if eff == 0xE and (par >> 4) in _EXTENDED:
        return
    if eff != 0xE and eff in _EFFECTS:
        return
    raise UnmodelledEffect(f"effect {eff:X}{par:02X}")


def _tuned(period: int, sample: SampleInfo) -> float:
    return period * 2.0 ** (-sample.finetune / 96.0)


def _play_row(v: _Voice, cell, samples: list[SampleInfo], speed: int, step_secs: float,
              amiga_clock: float, reach: dict[int, float | None]) -> bool:
    """One channel through one row: the cell's tick-0 work, then each tick's effects and playing.
    True when a note started on it."""
    period, ins, eff, par = cell
    hi, lo = par >> 4, par & 0xF
    sub = hi if eff == 0xE else None
    delay = lo if sub == 0xD else 0
    if ins:
        v.instrument = ins
    porta = eff == 0x3 and period
    trigger = bool(period) and not porta and v.instrument
    if porta:
        v.target = _tuned(period, samples[(v.instrument or 1) - 1])
    pending = _tuned(period, samples[v.instrument - 1]) if trigger and delay else None
    if eff == 0x3 and par:
        v.porta = par
    if eff == 0x4 and lo:
        v.depth = lo
    if eff == 0x9 and par:
        v.offset = par * _OFFSET_UNIT
    if ins and not delay:
        v.volume = samples[ins - 1].volume          # an instrument number resets the volume

    for tick in range(speed):
        if tick == delay and trigger:
            s = samples[v.instrument - 1]
            v.sample, v.period = v.instrument, _tuned(period, s)
            v.pos = float(v.offset if eff == 0x9 else 0)
            if ins:
                v.volume = s.volume
        if tick == 0:
            if eff == 0xC:
                v.volume = min(par, 64)
            elif sub == 0x1:
                v.period = max(_MIN_PERIOD, v.period - lo)
            elif sub == 0x2:
                v.period = min(_MAX_PERIOD, v.period + lo)
            elif sub == 0xA:
                v.volume = min(64, v.volume + lo)
            elif sub == 0xB:
                v.volume = max(0, v.volume - lo)
        else:
            if eff == 0xA:
                v.volume = min(64, v.volume + hi) if hi else max(0, v.volume - lo)
            elif eff == 0x3 and v.target:
                d = v.target - v.period
                v.period += max(-v.porta, min(v.porta, d))
        if sub == 0xC and tick == lo:
            v.volume = 0

        # The tick's bytes, at the vibrato's highest pitch on a 4xy row
        if not v.sample or v.period <= 0:
            continue
        s = samples[v.sample - 1]
        if s.looped or v.pos >= s.length:
            continue
        p = v.period - (_VIBRATO_SWING * v.depth if eff == 0x4 else 0)
        if pending is not None and tick < delay:
            p = min(p, pending)
        v.pos = min(float(s.length), v.pos + amiga_clock / max(p, 1.0) * step_secs)
        if v.volume:
            reach[v.sample] = max(reach.get(v.sample) or 0.0, v.pos)
    return bool(trigger) and delay < speed
