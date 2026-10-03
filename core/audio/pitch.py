"""Real pitch: Hz <-> MIDI numbers, note names in standard spelling (A4 = 440 Hz, C#4), cents.

What the VGM tools print for a chip's frequency.  SMPS labels and config spellings are
core/smps/names.py's.
"""

from __future__ import annotations

import math

A4_HZ = 440.0
A4_MIDI = 69
NOTE_NAMES = ('C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B')
NO_PITCH = "---"
_SEMITONES = 12
_CENTS_PER_OCTAVE = 1200


def hz_to_midi(hz: float) -> float:
    """440 Hz -> 69.0; fractional between semitones."""
    return A4_MIDI + _SEMITONES * math.log2(hz / A4_HZ)


def midi_name(midi: int) -> str:
    """69 -> 'A4', 61 -> 'C#4' (octave -1 starts at MIDI 0)."""
    return f"{NOTE_NAMES[midi % _SEMITONES]}{midi // _SEMITONES - 1}"


def pitch_name(hz: float) -> str:
    """The nearest semitone's name; NO_PITCH for 0 Hz."""
    return midi_name(round(hz_to_midi(hz))) if hz > 0 else NO_PITCH


def cents(f: float, ref: float) -> float:
    """How far `f` is above `ref`; NaN when either is 0."""
    return _CENTS_PER_OCTAVE * math.log2(f / ref) if f > 0 and ref > 0 else float('nan')
