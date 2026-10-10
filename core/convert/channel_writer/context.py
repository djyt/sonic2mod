"""What every channel's writer shares in one conversion, and what they count."""

from dataclasses import dataclass, field

from ...config import ConversionConfig, SynthesisSettings
from ...diagnostics import Diagnostics
from ...merge import MergePlan
from ...mod import ModFile
from ...plan import DetunePlan, Timeline, detune_cents, fm_catalogue
from ...smps import C1_SEMITONE, SmpsSong
from ..vibrato import VibratoSpeed


@dataclass
class EmissionStats:
    """What the channel writers counted, for the conversion's infos."""
    bank_delays_dropped: int = 0   # banked drum notes whose EDx gave way to the 9xx offset
    bank_cuts: int = 0             # banked notes cut before the next sound in their slot
    bank_cxx_moved: int = 0        # banked melodic notes whose attack-row Cxx went to the next row
    tie_retunes: dict = field(default_factory=lambda: {'placed': 0, 'skipped': 0})   # E1x / E2x on ties


@dataclass
class WriterContext:
    """What every channel's writer shares in one conversion."""
    mod: ModFile
    config: ConversionConfig
    song: SmpsSong
    synth: SynthesisSettings | None
    timeline: Timeline
    diag: Diagnostics
    vibrato: VibratoSpeed
    merge: MergePlan | None
    detune: DetunePlan | None
    fm_volume_mode: str                     # "baked" | "absolute" | "off"
    psg_volume_mode: str                    # "baked" | "absolute"
    pan_law_db: float
    fm_baseline_db: dict[int, float]        # baked levels: what each sample_list volume stands for
    psg_baseline_db: dict[int, float]
    release: dict[int, float | None]        # {instrument: release rate dB/s}
    release_slides: bool                    # end FM notes with a volume slide instead of C00
    player: str                             # settings.yaml `player`: the 4xy depth table
    leading_rests: dict[int, str]           # MOD channel -> source; laid out by ModLayout.leading_rests
    stats: EmissionStats
    # {instrument: (sample index its level starts to fall at, dB per sample)}: a sliding sustain
    # loop's fall (loop_decay: slide), which Fades.decay writes into each note as volume slides
    decay: dict[int, tuple[int, float]] = field(default_factory=dict)
    # {instrument: the sources whose note-ons play it} (core/convert/sample_names.py)
    played: dict[int, set] = field(default_factory=dict)
    _sample_detunes: dict[int, float] | None = field(default=None, init=False)

    @property
    def amiga_clock(self) -> float:
        return self.synth.amiga_clock if self.synth else SynthesisSettings().amiga_clock

    @property
    def legato_mode(self) -> str:
        return self.synth.legato if self.synth else "strict"

    def sample_cents(self, inst: int) -> float:
        """Cents an FM instrument's sample is detuned by (core.plan.detune): its FNUM offset at the
        pitch it is rendered at."""
        if self._sample_detunes is None:
            self._sample_detunes = {i.inst: detune_cents(C1_SEMITONE + i.synth_idx, i.layers[0].fnum_offset,
                                                         self.song.rules.fm_frequencies)
                                    for i in fm_catalogue(self.song, self.config).instruments.values()
                                    if len(i.layers) == 1}
        return self._sample_detunes.get(inst, 0.0)
