"""A ROM's PSG volume envelopes: each a string of attenuation steps ended by a command byte its
driver knows (SmpsVariant.envelope_commands: Sonic 1's $80 holds; Type 1a's $83 holds, $80
restarts, $85 nn jumps; Streets of Rage's $81 holds, $83 silences).  smpsPSGvoice n names
envelope n (fTone_0n).  Where they are is the
variant's locate (SoundIndex.envelopes).

    Sonic 1 rev01   9 envelopes (= core.smps.SONIC1_ENVELOPES)
    Moonwalker      6; envelope 6 has no command and runs on into envelope 5, as the driver reads it
"""

from __future__ import annotations

from ..chips import PSG_ATT_SILENT
from ..smps import PsgEnvelope, psg_voice_name
from .flags import EnvelopeCommand
from .image import RomError
from .memory import SoundMemory
from .variant import SmpsVariant

_COMMAND = 0x80             # a byte from $80 is a command
_MAX_STEPS = 0x100          # past this an envelope is not data

# What a located envelope looks like: steps past $F occur (Sonic 1 PSG2: ... 8, $10; the driver
# clamps), a command within the first 64 bytes
_PLAUSIBLE_STEPS = 64
_PLAUSIBLE_ATTENUATION = 0x1F


def read_envelopes(memory: SoundMemory, addresses: tuple[int, ...], variant: SmpsVariant) -> dict[str, PsgEnvelope]:
    """The envelope at each address, by smpsPSGvoice name."""
    return {psg_voice_name(i + 1): _envelope(memory, address, variant) for i, address in enumerate(addresses)}


def _envelope(memory: SoundMemory, address: int, variant: SmpsVariant) -> PsgEnvelope:
    steps: list[int] = []
    for at in range(address, address + _MAX_STEPS):
        value = memory.byte(at)
        if value < _COMMAND:
            steps.append(value)
            continue

        command = variant.envelope_commands.get(value)
        if command is None:
            raise RomError(f"PSG envelope at ${address:X}: ${value:02X} at ${at:X} is no {variant.name} envelope command")
        if command is EnvelopeCommand.MUTE:
            return PsgEnvelope((*steps, PSG_ATT_SILENT))
        if command is EnvelopeCommand.HOLD or not steps:
            return PsgEnvelope(tuple(steps))
        loop_to = 0 if command is EnvelopeCommand.RESTART else memory.byte(at + 1)
        if loop_to >= len(steps):
            raise RomError(f"PSG envelope at ${address:X}: loops to step {loop_to} of {len(steps)}")
        return PsgEnvelope(tuple(steps), loop_to)
    raise RomError(f"PSG envelope at ${address:X}: no command in {_MAX_STEPS} bytes")


def is_envelope(memory: SoundMemory, address: int) -> bool:
    """Plausible as an envelope: attenuation steps (0-$1F) up to a command byte."""
    for i in range(_PLAUSIBLE_STEPS):
        if not memory.contains(address + i):
            return False
        value = memory.byte(address + i)
        if value >= _COMMAND:
            return i > 0
        if value > _PLAUSIBLE_ATTENUATION:
            return False
    return False
