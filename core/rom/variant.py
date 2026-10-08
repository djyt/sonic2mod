"""SmpsVariant: everything one SMPS driver differs by, as data the generic readers ask.  A family
package (smps68k/ ...) builds one per driver; variants.py lists them.

    readers   header.py tracks.py voices.py envelopes.py    ask the variant, never its name
    families  smps68k/                                       Sonic 1 (Type 1b), Moonwalker (Type 1a)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..chips import OperatorReg
from ..smps import SmpsDriver
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
    """How a driver stores an FM voice: the feedback / algorithm byte, then four bytes (one per
    operator, in register order) for each operator register in `groups` order."""

    groups: tuple[OperatorReg, ...]

    @property
    def size(self) -> int:
        return 1 + len(self.groups) * OPERATORS


@dataclass(frozen=True, eq=False)     # one object per driver: compared by identity
class SmpsVariant:
    name: SmpsDriver
    memory: Callable[[RomImage], SoundMemory]
    locate: Callable[[RomImage], SoundIndex]
    flags: Mapping[int, FlagSpec]
    envelope_commands: Mapping[int, EnvelopeCommand]
    voice_layout: VoiceLayout
    dac_names: Mapping[int, str]                                     # the DAC track's bytes that play a sample
    dac: Callable[[RomImage, Mapping[int, str]], list[DacSample]]    # every sample a song can play
    known_roms: Mapping[str, tuple[RomFix, ...]] = field(default_factory=dict)   # SHA-1 -> its data fixes
