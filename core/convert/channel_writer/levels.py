"""The MOD volume a note plays at: its instrument's sample_list volume, moved by the level the
driver holds for the channel (SynthesisSettings.fm_volume_mode, psg_volume_mode).

    baked     sample volume x the note's dB against its instrument's commonest level: a Cxx only
              where they differ (no accumulator: the level is read off DriverState)
    absolute  the level's own MOD volume (PSG attenuation, FM TL offset) x the sample volume
    off       FM only: the channel volume less the smpsAlterVol steps, x the sample volume
"""

from ...audio import db_to_gain
from ...config import SAMPLE_SLOT, SAMPLE_VOLUME, ChannelConfig
from ...merge import Composite
from ...mod import MOD_MAX_VOLUME, clamp_mod_volume
from ...plan import DriverState
from ...smps import AlterVol, ChannelType, SetVol, SmpsChannel
from ..level_plan import fm_tl_to_mod, psg_att_to_mod
from .context import WriterContext


class Levels:
    """One channel's levels as MOD volumes.  The FM law applies to every FM note on the channel,
    its own or one spliced in from another channel (core.merge: a solo or pool note on the drum
    channel keeps its level)."""

    def __init__(self, ctx: WriterContext, channel: SmpsChannel, chan_cfg: ChannelConfig, st: DriverState):
        self._ctx = ctx
        kind = channel.header.channel_type
        self._is_psg = kind == ChannelType.PSG
        self._volume = chan_cfg.volume                  # the channel's MOD volume
        self._header_volume = channel.header.volume

        # {inst_num: sample volume} from sample_list, for Cxx scaling
        self._sample_vols = {e[SAMPLE_SLOT]: (e[SAMPLE_VOLUME] if len(e) > SAMPLE_VOLUME else MOD_MAX_VOLUME)
                             for e in ctx.config.sample_list or []}

        # The modes; outside baked one, current_volume carries the level as a MOD volume
        self._fm_absolute = ctx.fm_volume_mode == "absolute"
        self._fm_baked = ctx.fm_volume_mode == "baked"
        self._psg_baked = ctx.psg_volume_mode == "baked"
        self._current_volume = chan_cfg.volume
        if self._is_psg or (self._fm_absolute and kind != ChannelType.DAC):
            self._current_volume = self._level_volume(st)

    def sample(self, inst: int) -> int:
        """The instrument's sample_list volume: what a note-on plays at with no Cxx."""
        return self._sample_vols.get(inst, MOD_MAX_VOLUME)

    def on_change(self, eff: AlterVol | SetVol, st: DriverState) -> None:
        """st.apply moved the TL offset / attenuation; the non-baked modes keep their own
        MOD-volume accumulator on top of it: the channel volume less the TL steps the song
        moved from its header volume (smpsAlterVol: by its delta; SET_VOL: to its level)."""
        if self._is_psg or self._fm_absolute:
            self._current_volume = self._level_volume(st)
            return
        if self._fm_baked:
            return

        if isinstance(eff, AlterVol):
            moved = self._current_volume - eff.delta
        else:                                   # SetVol: from the channel's header volume
            moved = self._volume - (eff.level - self._header_volume)
        self._current_volume = max(0, min(MOD_MAX_VOLUME, moved))

    def emit(self, st: DriverState, inst: int, gain_db: float, member: Composite | None) -> int:
        """MOD volume for a note on `inst` now (equals the sample volume → no Cxx).

        Read off `st`: this channel's state or, for a follower's solo note on a merged channel,
        the follower's (so a PSG hat on the drum channel keeps its law).  `gain_db`: what a
        unison chord adds (ResolvedNote.gain_db); `member`: the banked composite the note plays
        (core.merge.banks), measured under its own id.
        """
        sv = self.sample(inst)
        baked = self._psg_baked if st.is_psg else self._fm_baked
        if not baked:
            return round(self._current_volume * sv / MOD_MAX_VOLUME)
        if st.is_psg and st.is_silent:
            return 0

        level = st.level_db(self._ctx.pan_law_db) + gain_db
        baseline = self._ctx.psg_baseline_db if st.is_psg else self._ctx.fm_baseline_db
        key = member.bank_id if member is not None and member.inst == inst else inst
        rel_db = level - baseline.get(key, level)
        # A banked sound's bytes carry its own volume against the bank's (sv): at its own level
        # it needs no Cxx either
        return clamp_mod_volume(sv * db_to_gain(rel_db) * self._volume / MOD_MAX_VOLUME)

    def _level_volume(self, st: DriverState) -> int:
        """The MOD volume st's level stands for outside baked mode: the PSG attenuation, or the
        FM TL offset (absolute mode)."""
        level = psg_att_to_mod(st.att) if self._is_psg else fm_tl_to_mod(st.tl)
        return round(level * self._volume / MOD_MAX_VOLUME)
