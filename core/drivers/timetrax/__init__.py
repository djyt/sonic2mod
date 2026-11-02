"""Time Trax (prototype): Tim Follin's Z80 driver, a family of its own (docs/todo/time_trax.md).

    variant.py    the description: rules, tables read from the ROM, the song's walk
    locate.py     the bank, the song and voice tables, the FM and pan tables, by the code that reads each
    memory.py     big-endian addresses in the bank window; a song's reading
    header.py     a sound: its list of tracks
    commands.py   the track code's bytes and their operands
    walk.py       a song followed frame by frame: fixed durations, loop ends, waits
    grammar.py    each byte as the SMPS walk's ops
    voices.py     a table of 26-byte voices
"""

from .variant import TIMETRAX

__all__ = ["TIMETRAX"]
