"""VGM / VGZ register logs: the source when there is no disassembly, beside smps/ (which it reads).

    reader.py     the container: commands -> timestamped chip writes (VgmLog)
    chipstate.py  YM2612 + SN76489 registers replayed over the writes (ChipState, Change)
    frames.py     the log cut into V-int frames: each channel's state and writes per frame (FrameLog)
    realign.py    a frame log with the V-ints its driver lost or gained undone (realigned)
    notes.py      where notes start (NoteStart), each channel's pitch timeline (pitch_segments)
    cache.py      a rip's frame log kept on disk (load_frames)
    lift/         the frame log lifted back into the SmpsSong the driver played (lift_song)
"""

from .cache import load_frames
from .chipstate import (
    DAC_CHANNEL,
    FM_CHANNELS,
    NOISE_CHANNEL,
    PSG_SILENT,
    PSG_TONE_CHANNELS,
    Change,
    ChangeKind,
    ChipState,
    noise_rate,
    noise_white,
)
from .frames import DacFrame, FmFrame, Frame, FrameLog, PsgFrame, frame_log
from .lift import LIFTED_ASPECTS, LIFTED_KINDS, LiftOptions, VgmLiftError, lift_song
from .notes import (
    DAC_NAME,
    DEFAULT_MOD_CENTS,
    FM_NAMES,
    PSG_NAMES,
    NoteStart,
    NoteTracker,
    Segment,
    note_starts,
    pitch_segments,
)
from .reader import (
    VGM_SAMPLE_RATE,
    VgmError,
    VgmHeader,
    VgmLog,
    VgmOp,
    VgmWrite,
    decode_vgm,
    is_vgm_path,
    read_vgm,
    vgm_bytes,
)
from .realign import realigned

__all__ = [
    "DAC_CHANNEL",
    "DAC_NAME",
    "DEFAULT_MOD_CENTS",
    "FM_CHANNELS",
    "FM_NAMES",
    "LIFTED_ASPECTS",
    "LIFTED_KINDS",
    "NOISE_CHANNEL",
    "PSG_NAMES",
    "PSG_SILENT",
    "PSG_TONE_CHANNELS",
    "VGM_SAMPLE_RATE",
    "Change",
    "ChangeKind",
    "ChipState",
    "DacFrame",
    "FmFrame",
    "Frame",
    "FrameLog",
    "LiftOptions",
    "NoteStart",
    "NoteTracker",
    "PsgFrame",
    "Segment",
    "VgmError",
    "VgmHeader",
    "VgmLiftError",
    "VgmLog",
    "VgmOp",
    "VgmWrite",
    "decode_vgm",
    "frame_log",
    "is_vgm_path",
    "lift_song",
    "load_frames",
    "noise_rate",
    "noise_white",
    "note_starts",
    "pitch_segments",
    "read_vgm",
    "realigned",
    "vgm_bytes"
]
