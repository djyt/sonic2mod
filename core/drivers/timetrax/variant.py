"""Time Trax (prototype): Tim Follin's own Z80 driver, no SMPS.  No disassembly: its code read with a
disassembler (docs/todo/time_trax.md).

    songs       a list of tracks each (header.py); the bank, its tables and the FM table by the code
                that reads each (locate.py)
    tracks      FM1-FM5, two songs FM6; a duration counts frames (no tempo), V-int at 50 Hz on the rips
    code        commands.py: what each byte is; grammar.py: what it plays as; walk.py: what the bytes
                do not say (fixed durations, loop ends, the FM3 -> FM5 sync)
    voice       26 bytes in register order (voices.py), a table of their addresses
    FM table    96 words from B (index 1 C0): A = fnum $44B, +25 cents on the NTSC clock, +10 on
                PAL's; played as written

Its playback rules: a rest writes nothing, the note sounds on to a read's key-off (rest_holds); the
volume scales operators (volume.py: its table of squares from the ROM), envelopes move it frame by
frame (envelope.py, from the ROM); the word slides frame by frame (glide.py: a glide's step across a
block).  The rest are Sonic 1's.
"""

from __future__ import annotations

from dataclasses import replace

from core.rom.header import RomHeader
from core.rom.image import RomImage
from core.rom.memory import SoundMemory
from core.rom.variant import HeaderLayout, SmpsVariant, TrackSlot
from core.smps import FM_CHANNEL_NAMES, ChannelType, KeyOffRules, PitchRules, PlaybackRules, TrackRules, VolumeRules

from ..names import SmpsDriver
from .envelope import read_envelopes
from .glide import TimeTraxGlide
from .grammar import FLAGS, instruction
from .header import read_header, track_numbers
from .locate import driver_tables, locate, time_trax_memory
from .memory import TimeTraxMemory
from .voices import VOICE, read_voices
from .volume import TimeTraxVolume
from .walk import walk_song

# The tracks as the generic header reader would lay them out (read_header reads them)
_HEADER = HeaderLayout(fm_slots=tuple(TrackSlot(ChannelType.FM, name) for name in FM_CHANNEL_NAMES),
                       sfx_channels=frozenset(), tempo=False)


def _song_memory(memory: SoundMemory, head: RomHeader) -> SoundMemory:
    """The memory a song's tracks read through: its walk's fixed durations, loop ends and waits."""
    if not isinstance(memory, TimeTraxMemory):
        raise TypeError("a Time Trax song reads through its driver's tables (TimeTraxMemory)")
    return memory.with_reading(walk_song(memory, track_numbers(head)))


def _rules_from_rom(rom: RomImage, rules: PlaybackRules) -> PlaybackRules:
    """Its FM table, read from the driver (note n plays entry n: $80 + n), its volume law's table and
    its volume envelopes."""
    tables = driver_tables(rom)
    fm = rules.track(ChannelType.FM)
    law = VolumeRules(law=TimeTraxVolume(tables.squares, tables.master),
                      envelopes=read_envelopes(time_trax_memory(rom), tables.envelopes))
    return replace(rules, fm_frequencies=tables.fm_words, tracks={ChannelType.FM: replace(fm, volume=law)})


TIMETRAX = SmpsVariant(
    name=SmpsDriver.TIMETRAX,
    memory=time_trax_memory,
    locate=locate,
    flags={ChannelType.FM: FLAGS, ChannelType.PSG: {}, ChannelType.DAC: {}},
    envelope_commands={},
    header=_HEADER,
    voice_layout=VOICE,
    rules=PlaybackRules(driver=SmpsDriver.TIMETRAX, fm_frequencies=(), psg_frequencies=(), psg_read=(),
                        psg_envelopes={}, dac_names={},
                        tracks={ChannelType.FM: TrackRules(key_off=KeyOffRules(rest_holds=True),
                                                           pitch=PitchRules(slide=TimeTraxGlide()))}),
    rules_from_rom=_rules_from_rom,
    grammar=instruction,
    music_header_reader=read_header,
    voice_reader=read_voices,
    song_memory=_song_memory,
)
