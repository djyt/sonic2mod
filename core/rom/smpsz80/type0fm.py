"""SMPS Z80 Type 0 FM: Golden Axe.  An early Type 1 FM: FM drums, no DAC music, no pan / note fill
/ modulation flags.  No disassembly: its flag table at Z80 $0B65, read with a disassembler
(docs/todo/binary_import.md, Phase 3).

A flag handler starts at the first operand and the driver steps past one more byte after it: a
flag with no handler of its own skips one operand.  The drum track's notes name drums (low nibble:
an FM drum program on FM3, drums.py; bits 4-6 a PSG drum: no song plays one).
"""

from __future__ import annotations

from ...smps import FIRST_NOTE, LAST_NOTE, CoordFlag, FmDrum, SmpsDriver, SmpsSongHeader
from ..flags import CALL, JUMP, LOOP, NO_ATTACK, RETURN, STOP, FlagSpec, drop, effect, refuse
from ..image import RomImage
from ..variant import SmpsVariant
from .drums import drum_name, read_fm_drums
from .layout import HEADER_TYPE0, VOICE_TYPE0
from .locate import fm_frequencies, locate_type0, sound_bank
from .memory import BankedZ80Memory

GOLDEN_AXE_REV_A_SHA1 = "2ce17105ca916fbbe3ac9ae3a2086e66b07996dd"


# No handler of its own: one operand skipped
_NO_OPS = (*range(0xE0, 0xE5), *range(0xE8, 0xEF), 0xF1, 0xF3, 0xF4, 0xF5, 0xFA, 0xFF)

_FLAGS: dict[int, FlagSpec] = {
    **{byte: drop(f"${byte:02X} (no handler)", 1) for byte in _NO_OPS},
    0xE5: effect(CoordFlag.ALTER_VOL),
    0xE6: effect(CoordFlag.ALTER_VOL),
    0xE7: NO_ATTACK,
    0xEF: effect(CoordFlag.SET_VOICE),
    0xF0: effect(CoordFlag.SET_VOL),
    0xF2: STOP,
    0xF6: JUMP,
    0xF7: LOOP,
    0xF8: CALL,
    0xF9: RETURN,
    0xFB: effect(CoordFlag.CHANGE_TRANSPOSITION),
    0xFC: refuse("slide mode (each note: slide, a skipped byte, duration)", 1),
    0xFD: refuse("raw-frequency mode (each note an fnum word)", 1),
    0xFE: refuse("FM3 special mode", 4),
}


def _fm_drums(rom: RomImage, header: SmpsSongHeader, fm_frequencies: tuple[int, ...]) -> dict[str, FmDrum]:
    return read_fm_drums(rom, header, fm_frequencies, _FLAGS)


def _memory(image: RomImage) -> BankedZ80Memory:
    return BankedZ80Memory(image, sound_bank(image))


TYPE0FM = SmpsVariant(
    name=SmpsDriver.TYPE0FM,
    memory=_memory,
    locate=locate_type0,
    flags=_FLAGS,
    envelope_commands={},          # no PSG envelope table located (no song uses the PSG)
    header=HEADER_TYPE0,
    voice_layout=VOICE_TYPE0,
    dac_names={b: drum_name(b) for b in range(FIRST_NOTE, LAST_NOTE + 1)},
    fm_frequencies=fm_frequencies,
    fm_drums=_fm_drums,
    known_roms={GOLDEN_AXE_REV_A_SHA1: ()},
)
