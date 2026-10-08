"""SmpsVariant: everything one SMPS driver differs by, as data the generic readers ask.  A family
package (smps68k/ ...) builds one per driver; variants.py lists them.

    readers   header.py tracks.py voices.py envelopes.py    ask the variant, never its name
    families  smps68k/                                       Sonic 1 (Type 1b), Moonwalker (Type 1a)
              smpsz80/                                       Golden Axe (Type 0 FM)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..chips import OperatorReg
from ..smps import FmDrum, SmpsDriver, SmpsSongHeader
from .fixes import RomFix
from .flags import EnvelopeCommand, FlagSpec
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


@dataclass(frozen=True)
class VoiceLayout:
    """How a driver stores an FM voice: the feedback / algorithm byte, the B4 byte if `pan`
    (L R AMS FMS), then four bytes (one per operator, in register order) for each operator
    register in `groups` order."""

    groups: tuple[OperatorReg, ...]
    pan: bool = False

    @property
    def size(self) -> int:
        return self.groups_at + len(self.groups) * OPERATORS

    @property
    def groups_at(self) -> int:
        return 1 + self.pan


@dataclass(frozen=True)
class TrackSlot:
    """A music header's DAC / FM entry: what the driver's track table makes of it."""

    channel_type: str              # "DAC" (the percussion track) or "FM"
    chip_channel: str = ""         # the chip channel it plays on; "" = header order (Sonic 1's)


@dataclass(frozen=True)
class HeaderLayout:
    """How a driver reads its song and SFX headers beyond the fields every SMPS header shares."""

    fm_slots: tuple[TrackSlot, ...]          # the DAC / FM entries in header order, as many as it has tracks
    sfx_channels: frozenset[int]             # the channel ids an SFX track may name
    never_holds: int | None = None           # the tempo byte that never stalls (Type 0 FM's 0)
    tempo_phase: int = 0                     # frames the first hold comes late (Type 0 FM: 1)


@dataclass(frozen=True, eq=False)     # one object per driver: compared by identity
class SmpsVariant:
    name: SmpsDriver
    memory: Callable[[RomImage], SoundMemory]
    locate: Callable[[RomImage], SoundIndex]
    flags: Mapping[int, FlagSpec]
    envelope_commands: Mapping[int, EnvelopeCommand]
    header: HeaderLayout
    voice_layout: VoiceLayout
    dac_names: Mapping[int, str]                                     # the DAC track's bytes that play a sample
    dac: Callable[[RomImage, Mapping[int, str]], list[DacSample]]    # every sample a song can play
    fm_frequencies: Callable[[RomImage], tuple[int, ...] | None]     # the FM table notes play from; None: Sonic 1's
    # The drum track's FM drum programs by DAC name, as a song's header and FM table play them
    fm_drums: Callable[[RomImage, SmpsSongHeader, tuple[int, ...]], dict[str, FmDrum]]
    known_roms: Mapping[str, tuple[RomFix, ...]] = field(default_factory=dict)   # SHA-1 -> its data fixes
