"""The baked levels: for each MOD instrument, the level most of its notes play at.

Its sample_list volume stands for that level, so those notes need no Cxx; the rest get one by the
chip's law (core/smps/levels.py).  FM samples are also rendered at it (fm_render_levels), so the chip
clips a multi-carrier voice as the hardware does at that level.  Every count walks the channels
with the same DriverState the conversion does.
"""

from ..audio import db_to_gain
from ..chips import FM_TL_SILENT, PSG_ATT_SILENT, fm_level_db, psg_level_db
from ..config import ConversionConfig
from ..merge import MergePlan
from ..mod import MOD_MAX_VOLUME
from ..plan import DetunePlan, enabled_channels, walk_channel
from ..smps import (
    SmpsSong,
)


def fm_tl_to_mod(tl_offset: int) -> int:
    """YM2612 TL offset -> absolute MOD volume 0-64 (smpsHeaderFM volume, smpsAlterVol)."""
    if tl_offset >= FM_TL_SILENT:
        return 0
    return round(MOD_MAX_VOLUME * db_to_gain(fm_level_db(tl_offset)))


def psg_att_to_mod(attenuation: int) -> int:
    """SN76489 attenuation -> absolute MOD volume 0-64 (0 = max, 15 = silent)."""
    if attenuation >= PSG_ATT_SILENT:
        return 0
    return round(MOD_MAX_VOLUME * db_to_gain(psg_level_db(attenuation)))


def modal_level(counts: dict[float, int]) -> float:
    """The level most notes play at — what a "baked" sample_list volume stands for.

    Ties go to the louder level, so the others are attenuated by Cxx rather than
    boosted past 64.
    """
    return max(counts, key=lambda level: (counts[level], level))


class LevelPlanner:
    def __init__(self, song: SmpsSong, config: ConversionConfig, plan: MergePlan | None,
                 detune: DetunePlan | None, pan_law_db: float, gained: dict[str, set[int]]) -> None:
        self._song = song
        self._config = config
        self._plan = plan
        self._detune = detune
        self._pan_law_db = pan_law_db
        self._gained = gained          # {"FM"/"PSG": instruments unison chords play louder}, filled by the counts

    def levels(self, kind: str) -> dict[int, float]:
        """"baked" volume mode: the level (dB) each MOD instrument's sample_list volume stands for.

        The level with the most notes is the instrument's baseline — those notes need no Cxx.
        Ties go to the louder level so the others are attenuated rather than boosted past 64.

        FM levels come from the TL offset (smpsHeaderFM volume + smpsAlterVol) and the pan;
        PSG levels from the attenuation (smpsHeaderPSG volume + smpsPSGAlterVol).
        """
        pan_law = self._pan_law_db
        counts = self._count(kind, lambda st, res: st.level_db(pan_law) + res.gain_db)
        return self._with_variants({inst: modal_level(levels) for inst, levels in counts.items()})

    def fm_render_levels(self) -> dict[int, tuple[int, bool]]:
        """{MOD instrument: (carrier TL offset, hard-panned)} its FM sample is rendered at.

        The level most of the instrument's notes play at, chosen as levels() chooses its
        baseline (ties to the louder), so the sample carries the level its sample_list volume
        stands for.  The driver adds the track volume to the carrier TLs before the chip sums
        them (SetVoice), so rendering at that offset clips a multi-carrier voice exactly as
        much as the hardware does at that level — at TL 0 every GHZ lead clipped a third of
        its samples where the hardware, at the channel's +18 TL, clips none.
        """
        pan_law = self._pan_law_db
        counts = self._count("FM", lambda st, _res: (st.tl, st.hard_panned), sources_keep_votes=True)
        return self._with_variants({inst: max(per, key=lambda k: (per[k], fm_level_db(k[0], k[1], pan_law)))
                                    for inst, per in counts.items()})

    def _count(self, kind: str, level_of, sources_keep_votes: bool = False) -> dict[int, dict]:
        """{MOD instrument: {level_of(state, resolved): notes}} over every enabled channel of
        `kind` ("FM" or "PSG"), walked with the same DriverState the conversion uses.  A PSG
        note at attenuation 15 is silent and does not vote.  The instruments a merged build's
        unison chords play louder (ResolvedNote.gain_db) are noted in `gained`.

        In a merged build a composite's slot counts only the notes that play the composite.
        `sources_keep_votes`: except where the slot's former instrument is a mix source kept
        aside under that number (MergePlan.mix_only) - it is still rendered, at its own notes'
        level, for the mixer (the render levels ask for that).  A detune variant (core.plan.detune)
        votes as its base instrument: it is that sample a few cents off.
        """
        counts: dict[int, dict] = {}
        plan = self._plan
        owned = set(plan.instruments) if plan is not None else set()
        if plan is not None and sources_keep_votes:
            owned -= plan.mix_only
        for chan_cfg, channel in enabled_channels(self._song, self._config, (kind,)):
            for event, st, res in walk_channel(channel, self._config, chan_cfg):
                if res is None or (st.is_psg and st.is_silent):
                    continue
                # A composite owns its slot: a note that resolves to the slot's former instrument
                # (a follower's folded note, a dropped channel's) does not vote for the composite's
                # level.  Green Hill's FM4 voice $07 notes set the level of the FM2+PSG1 chord that
                # had taken slot 13, and its one note got a C40.
                if (plan is not None and res.instrument in owned
                        and (chan_cfg.source, event.tick_position) not in plan.ticks):
                    continue
                inst = self._detune.base_of(res.instrument) if self._detune else res.instrument
                if plan is not None:
                    member = plan.bank_members.get((chan_cfg.source, event.tick_position))
                    if member is not None:
                        inst = member.bank_id       # the bank's slot holds other sounds' levels too
                per = counts.setdefault(inst, {})
                k = level_of(st, res)
                per[k] = per.get(k, 0) + 1
                if res.gain_db:
                    self._gained.setdefault(kind, set()).add(inst)
        return counts

    def _with_variants(self, per_inst: dict) -> dict:
        """A per-instrument plan with every detune variant given its base's value."""
        return self._detune.share_base(per_inst) if self._detune else per_inst
