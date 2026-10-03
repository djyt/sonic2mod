"""A ROM's PSG volume envelopes: PSG_Index, a long per envelope, each a string of attenuation
steps ended by a command byte its driver knows (drivers.py: Sonic 1's $80 holds; Type 1a's $83
holds, $80 restarts, $85 nn jumps).  smpsPSGvoice n names envelope n (fTone_0n).

    Sonic 1 rev01   PSG_Index $719A8, 9 envelopes (= core.smps.SONIC1_ENVELOPES)
    Moonwalker      PSG_Index $60020, 6; envelope 6 has no command and runs on into envelope 5,
                    as the driver reads it
"""

from __future__ import annotations

from ..smps import PsgEnvelope, psg_voice_name
from .drivers import EnvelopeCommand, RomDriver
from .image import RomError, RomImage

_LONG = 4
_COMMAND = 0x80             # a byte from $80 is a command
_MAX_STEPS = 0x100          # past this an envelope is not data


def read_envelopes(rom: RomImage, table: int, driver: RomDriver) -> dict[str, PsgEnvelope]:
    """Every envelope PSG_Index points at, by smpsPSGvoice name.  The table ends where the first
    envelope's bytes begin."""
    pointers: list[int] = []
    at = table
    while not pointers or at < min(pointers):
        address = rom.long(at)
        if not rom.contains(address):
            break
        pointers.append(address)
        at += _LONG
    return {psg_voice_name(i + 1): _envelope(rom, address, driver) for i, address in enumerate(pointers)}


def _envelope(rom: RomImage, address: int, driver: RomDriver) -> PsgEnvelope:
    steps: list[int] = []
    for at in range(address, address + _MAX_STEPS):
        value = rom.byte(at)
        if value < _COMMAND:
            steps.append(value)
            continue

        command = driver.envelope_commands.get(value)
        if command is None:
            raise RomError(f"PSG envelope at ${address:X}: ${value:02X} at ${at:X} is no {driver.name} envelope command")
        if command is EnvelopeCommand.HOLD or not steps:
            return PsgEnvelope(tuple(steps))
        loop_to = 0 if command is EnvelopeCommand.RESTART else rom.byte(at + 1)
        if loop_to >= len(steps):
            raise RomError(f"PSG envelope at ${address:X}: loops to step {loop_to} of {len(steps)}")
        return PsgEnvelope(tuple(steps), loop_to)
    raise RomError(f"PSG envelope at ${address:X}: no command in {_MAX_STEPS} bytes")
