"""VGM / VGZ register logs: the source when there is no disassembly, beside smps/ (which it reads).

    reader.py     the container: commands -> timestamped chip writes (VgmLog)
    chipstate.py  YM2612 + SN76489 registers replayed over the writes (ChipState, Change)
    frames.py     the log cut into V-int frames: each channel's state and writes per frame (FrameLog)
    lift.py       the log lifted back into the SmpsSong the driver played (lift_song)
"""

from .chipstate import (
    DAC_CHANNEL,
    FM_CHANNELS,
    NOISE_CHANNEL,
    PSG_SILENT,
    PSG_TONE_CHANNELS,
    Change,
    ChangeKind,
    ChipState,
    fm_frequency_hz,
    psg_frequency_hz,
)
from .frames import DacFrame, FmFrame, Frame, FrameLog, PsgFrame, frame_log
from .lift import LiftOptions, VgmLiftError, lift_song
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

__all__ = [
    "DAC_CHANNEL", "FM_CHANNELS", "NOISE_CHANNEL", "PSG_SILENT", "PSG_TONE_CHANNELS", "VGM_SAMPLE_RATE", "Change",
    "ChangeKind", "ChipState", "DacFrame", "FmFrame", "Frame", "FrameLog", "LiftOptions", "PsgFrame", "VgmError",
    "VgmHeader", "VgmLiftError", "VgmLog", "VgmOp", "VgmWrite", "decode_vgm", "fm_frequency_hz", "frame_log",
    "is_vgm_path", "lift_song", "psg_frequency_hz", "read_vgm", "vgm_bytes"
]
