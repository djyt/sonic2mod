"""MOD notes: the ProTracker period table, ModNote (C1..B3) and its config spellings."""

from enum import Enum

# https://eab.abime.net/showthread.php?t=66065
# Borrowed from: https://github.com/8bitbubsy/pt2-clone/blob/master/src/pt2_tables.c
PERIOD_TABLE = [
#   C-1  C#1  D-1  D#1  E-1  F-1  F#1  G-1  G#1  A-1  A#1  B-1
    856, 808, 762, 720, 678, 640, 604, 570, 538, 508, 480, 453,
#   C-2  C#2  D-2  D#2  E-2  F-2  F#2  G-2  G#2  A-2  A#2  B-2
    428, 404, 381, 360, 339, 320, 302, 285, 269, 254, 240, 226,
#   C-3  C#3  D-3  D#3  E-3  F-3  F#3  G-3  G#3  A-3  A#3  B-3
    214, 202, 190, 180, 170, 160, 151, 143, 135, 127, 120, 113, 0,
]


class ModNote(Enum):
    C1 = 0
    Cs1 = 1
    D1 = 2
    Ds1 = 3
    E1 = 4
    F1 = 5
    Fs1 = 6
    G1 = 7
    Gs1 = 8
    A1 = 9
    As1 = 10
    B1 = 11

    C2 = 12
    Cs2 = 13
    D2 = 14
    Ds2 = 15
    E2 = 16
    F2 = 17
    Fs2 = 18
    G2 = 19
    Gs2 = 20
    A2 = 21
    As2 = 22
    B2 = 23

    C3 = 24
    Cs3 = 25
    D3 = 26
    Ds3 = 27
    E3 = 28
    F3 = 29
    Fs3 = 30
    G3 = 31
    Gs3 = 32
    A3 = 33
    As3 = 34
    B3 = 35


# MOD note name -> ModNote: "C2", "Fs3" / "F#3" / "Gb3" (the DAC mapping's mod_note spelling)
MOD_NOTE_MAP: dict[str, ModNote] = {}
for _oct in range(1, 4):
    for _name, _enum in [
        ("C", "C"), ("C#", "Cs"), ("Cs", "Cs"), ("Db", "Cs"), ("D", "D"), ("D#", "Ds"), ("Ds", "Ds"),
        ("Eb", "Ds"), ("E", "E"), ("F", "F"), ("F#", "Fs"), ("Fs", "Fs"), ("Gb", "Fs"), ("G", "G"),
        ("G#", "Gs"), ("Gs", "Gs"), ("Ab", "Gs"), ("A", "A"), ("A#", "As"), ("As", "As"), ("Bb", "As"),
        ("B", "B"),
    ]:
        MOD_NOTE_MAP[f"{_name}{_oct}"] = ModNote[f"{_enum}{_oct}"]
del _oct, _name, _enum
