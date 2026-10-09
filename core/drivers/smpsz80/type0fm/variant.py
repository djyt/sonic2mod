"""SMPS Z80 Type 0 FM: Golden Axe.  An early Type 1 FM: FM drums, no DAC music, no pan / note fill
/ modulation flags.  No disassembly: its flag table at Z80 $0B65, read with a disassembler
(docs/todo/binary_import.md, Phase 3).

A flag handler starts at the first operand and the driver steps past one more byte after it: a
flag with no handler of its own skips one operand.  The drum track's notes name drums (low nibble:
an FM drum program on FM3, drums.py; bits 4-6 a PSG drum: no song plays one).
"""

from __future__ import annotations

from dataclasses import replace

from core.rom.flags import CALL, JUMP, LOOP, NO_ATTACK, RETURN, STOP, FlagSpec, drop, effect, every_kind, refuse
from core.rom.image import RomImage
from core.rom.variant import SmpsVariant
from core.smps import FIRST_NOTE, LAST_NOTE, CoordFlag, FmDrum, PlaybackRules, SmpsSongHeader

from ...names import SmpsDriver
from ..memory import BankedZ80Memory
from .drums import drum_name, read_fm_drums
from .layout import HEADER_TYPE0, KEY_RUN_OUT, TEMPO_PHASE, VOICE_TYPE0
from .locate import fm_frequencies, locate_type0, sound_bank

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


def _rules_from_rom(rom: RomImage, rules: PlaybackRules) -> PlaybackRules:
    return replace(rules, fm_frequencies=fm_frequencies(rom))


def _fm_drums(rom: RomImage, header: SmpsSongHeader, fm_frequencies: tuple[int, ...]) -> dict[str, FmDrum]:
    return read_fm_drums(rom, header, fm_frequencies, _FLAGS)


def _memory(image: RomImage) -> BankedZ80Memory:
    return BankedZ80Memory(image, sound_bank(image))


TYPE0FM = SmpsVariant(
    name=SmpsDriver.TYPE0FM,
    memory=_memory,
    locate=locate_type0,
    flags=every_kind(_FLAGS),
    envelope_commands={},          # no PSG envelope table located (no song uses the PSG)
    header=HEADER_TYPE0,
    voice_layout=VOICE_TYPE0,
    # Its FM table read from the driver; no PSG table or envelopes located (no song uses the PSG);
    # tempo: the first hold a frame late; a note keyed 256 frames is keyed off
    rules=PlaybackRules(driver=SmpsDriver.TYPE0FM, fm_frequencies=(), psg_frequencies=(), psg_read=(), psg_envelopes={},
                        dac_names={b: drum_name(b) for b in range(FIRST_NOTE, LAST_NOTE + 1)},
                        tempo_phase=TEMPO_PHASE, key_run_out=KEY_RUN_OUT),
    rules_from_rom=_rules_from_rom,
    fm_drums=_fm_drums,
)
