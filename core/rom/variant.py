"""SmpsVariant: everything one SMPS driver differs by, as data and hooks the generic readers ask.
Each driver's folder (core/drivers/<family>/<driver>/variant.py) builds one; core/drivers/registry.py
lists them.

    readers   header.py tracks.py voices.py envelopes.py    ask the variant, never its name
    drivers   core/drivers/smps68k/ sonic1 type1a mucom, core/drivers/smpsz80/ type0fm   (the layer above)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..chips import OperatorReg
from ..smps import ChannelType, FmDrum, SmpsDriver, SmpsSongHeader
from .fixes import RomFix
from .flags import EnvelopeCommand, FlagSpec
from .grammar import Instruction, smps_instruction
from .image import RomError, RomImage
from .memory import SoundMemory

OPERATORS = 4


@dataclass(frozen=True)
class SoundIndex:
    """Each sound ID's header address, and each PSG envelope's."""

    music: dict[int, int]
    sfx: dict[int, int]
    envelopes: tuple[int, ...] = ()       # in index order (fTone_01 first); empty: not located

    def address(self, sound_id: int) -> int:
        found = self.music.get(sound_id, self.sfx.get(sound_id))
        if found is None:
            raise RomError(f"sound ${sound_id:02X}: not in the ROM's indexes "
                           f"(music ${min(self.music):02X}-${max(self.music):02X}, "
                           f"SFX ${min(self.sfx):02X}-${max(self.sfx):02X})")
        return found

    def is_sfx(self, sound_id: int) -> bool:
        return sound_id in self.sfx


@dataclass(frozen=True)
class DacSample:
    sound: int        # the DAC track's byte: $81 dKick
    name: str
    pcm: bytes        # signed 8-bit, as samples/*.raw hold it
    pitch: int        # the play loop's counter
    rate: float       # Hz
    of: int = 0       # a pitched copy: the byte whose sample it plays (Sonic 1's $88 -> $83)

    @property
    def is_pitched_copy(self) -> bool:
        return self.of != 0


# The register offset (operator slot) of each of a group's four bytes, as SMPS stores them
SMPS_OPERATOR_OFFSETS = (0x00, 0x08, 0x04, 0x0C)


@dataclass(frozen=True)
class VoiceLayout:
    """How a driver stores an FM voice: the feedback / algorithm byte and, if `pan`, the B4 byte
    (L R AMS FMS) - first, or last if `feedback_last` - and four bytes for each operator register
    in `groups` order, the register offset of each in `operator_offsets`."""

    groups: tuple[OperatorReg, ...]
    pan: bool = False
    feedback_last: bool = False
    operator_offsets: tuple[int, ...] = SMPS_OPERATOR_OFFSETS

    @property
    def size(self) -> int:
        return 1 + self.pan + len(self.groups) * OPERATORS

    @property
    def groups_at(self) -> int:
        return 0 if self.feedback_last else 1 + self.pan

    @property
    def feedback_at(self) -> int:
        return len(self.groups) * OPERATORS if self.feedback_last else 0


@dataclass(frozen=True)
class TrackSlot:
    """A music header's DAC / FM entry: what the driver's track table makes of it."""

    channel_type: ChannelType     # DAC (the percussion track) or FM
    chip_channel: str = ""         # the chip channel it plays on; "" = header order (Sonic 1's)


@dataclass(frozen=True)
class EntryLayout:
    """A music header's track entry: its size, and where each byte after the pointer word is
    (None: the driver stores none)."""

    size: int
    pitch: int | None = None
    volume: int | None = None
    mod: int | None = None
    envelope: int | None = None


SMPS_FM_ENTRY = EntryLayout(4, pitch=2, volume=3)                    # ptr.w pitch.b volume.b
SMPS_PSG_ENTRY = EntryLayout(6, pitch=2, volume=3, mod=4, envelope=5)  # ... mod.b envelope.b

_SHARED_BYTES = 4           # voices.w fm.b psg.b: every header's
_TEMPO_BYTES = 2            # divider.b modifier.b


@dataclass(frozen=True)
class HeaderLayout:
    """How a driver reads its song and SFX headers: voices.w fm.b psg.b, the tempo if `tempo`,
    then each track's entry."""

    fm_slots: tuple[TrackSlot, ...]          # the DAC / FM entries in header order, as many as it has tracks
    sfx_channels: frozenset[int]             # the channel ids an SFX track may name
    tempo: bool = True                       # divider.b modifier.b after the counts; without: a tick a frame
    fm_entry: EntryLayout = SMPS_FM_ENTRY
    psg_entry: EntryLayout = SMPS_PSG_ENTRY
    psg_slots: tuple[str, ...] = ()          # each PSG entry's chip channel; () = header order (PSG1 first)
    never_holds: int | None = None           # the tempo byte that never stalls (Type 0 FM's 0)
    tempo_phase: int = 0                     # frames the first hold comes late (Type 0 FM: 1)
    key_run_out: int | None = None           # frames a note keys without an attacking read (Type 0 FM: 256)

    @property
    def entries_at(self) -> int:
        """Where the first track entry starts."""
        return _SHARED_BYTES + (_TEMPO_BYTES if self.tempo else 0)


@dataclass(frozen=True, eq=False)     # one object per driver: compared by identity
class SmpsVariant:
    name: SmpsDriver
    memory: Callable[[RomImage], SoundMemory]
    locate: Callable[[RomImage], SoundIndex]
    flags: Mapping[ChannelType, Mapping[int, FlagSpec]]                 # each kind of track's flag table
    envelope_commands: Mapping[int, EnvelopeCommand]
    header: HeaderLayout
    voice_layout: VoiceLayout
    dac_names: Mapping[int, str]                                     # the DAC track's bytes that play a sample
    dac: Callable[[RomImage, Mapping[int, str]], list[DacSample]] | None = None   # every sample a song can play
    fm_frequencies: Callable[[RomImage], tuple[int, ...]] | None = None           # the FM table; None: Sonic 1's
    # The drum track's FM drum programs by DAC name, as a song's header and FM table play them
    fm_drums: Callable[[RomImage, SmpsSongHeader, tuple[int, ...]], dict[str, FmDrum]] | None = None
    known_roms: Mapping[str, tuple[RomFix, ...]] = field(default_factory=dict)   # SHA-1 -> its data fixes
    # The track grammar: the instruction at an address (grammar.py; SMPS's for every variant so far)
    grammar: Callable[[SoundMemory, int, SmpsVariant, ChannelType], Instruction] = smps_instruction
