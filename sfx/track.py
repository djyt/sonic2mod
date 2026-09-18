"""Per-channel SFX track state — the offline equivalent of SMPS_Track RAM.

Field names deliberately mirror the driver's RAM struct so `sfx/driver.py` reads
alongside `sonic_1/s1.sounddriver.asm` without translation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.driver_tables import HW_FM_CHANNEL, PSG_CHANNEL

# SMPS_Track.Freq is a signed word; -1 marks "no valid note" (PSGSetFreq .restpsg).
FREQ_INVALID = -1


@dataclass
class SfxTrack:
    """One SFX channel.

    Sound_PlaySFX zero-fills the whole track RAM before writing the header fields,
    so every counter starts at 0 and modulation starts disabled — see :1059.
    """

    channel_type: str            # "FM" | "PSG"
    voice_control: int           # $02/$04/$05 (FM) or $80/$A0/$C0 (PSG); -> $E0 after smpsPSGform
    events: list                 # flat SmpsEvent list; smpsLoop is already unrolled
    name: str = ""               # for diagnostics only

    # --- PlaybackControl bits ---
    playing: bool = True         # bit 7
    at_rest: bool = False        # bit 1
    no_attack: bool = False      # bit 4
    mod_active: bool = False     # bit 3

    # --- header-derived ---
    transpose: int = 0           # header pitch byte, then += smpsChangeTransposition
    volume: int = 0              # header vol byte, then += smpsAlterVol

    # --- playback cursor ---
    event_index: int = 0         # replaces DataPointer
    duration_timeout: int = 1    # first update parses immediately (:1071)
    saved_duration: int = 0

    # --- note state ---
    freq: int = 0
    detune: int = 0              # smpsAlterNote / smpsDetune — SET, not accumulated
    voice_index: int = 0         # FM: smpsSetvoice index. PSG: envelope index (0 = none)
    vol_env_index: int = 0

    # --- note fill (smpsNoteFill) — no Sonic 1 SFX uses it, kept for completeness ---
    note_timeout: int = 0
    note_timeout_master: int = 0

    # --- modulation (smpsModSet) ---
    mod_data: tuple = (0, 0, 0, 0)   # wait, speed, delta, steps as written
    mod_wait: int = 0
    mod_speed: int = 0
    mod_delta: int = 0               # unsigned byte; sign-extended at use
    mod_steps: int = 0
    mod_val: int = 0

    # --- FM only ---
    ams_fms_pan: int = 0xC0      # $B4; SFX FM tracks init to centre (:1077)
    voice: object | None = None  # SmpsVoice currently programmed, for TL re-sends

    # --- diagnostics ---
    warnings: list = field(default_factory=list)

    @property
    def is_fm(self) -> bool:
        return self.channel_type == "FM"

    @property
    def hw_ch(self) -> int:
        """OPN2 channel index (FM) or SN76489 channel index (PSG)."""
        if self.is_fm:
            return HW_FM_CHANNEL[self.voice_control]
        return PSG_CHANNEL[self.voice_control]

    @classmethod
    def from_header(cls, ch_header, events, name=""):
        """Build a track from a parsed SFX channel header, per Sound_PlaySFX."""
        return cls(
            channel_type=ch_header.channel_type,
            voice_control=ch_header.hw_channel,
            events=events,
            name=name,
            transpose=ch_header.pitch_offset,
            volume=ch_header.volume,
        )
