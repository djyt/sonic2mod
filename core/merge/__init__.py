"""Folding SMPS channels onto one MOD channel — the Amiga port's 3- and 4-channel MODs.

A `merge:` group in the config names a **primary** channel and its **followers**; `merge_drop:`
lists channels the merged build leaves out altogether (a song with more independent voices
than the Amiga has channels — Green Hill Zone keeps drums, bass, lead and one harmony):

    merge:
      - primary: FM1          # the lead ...
        followers: [FM5]      # ... and its detuned double
      - primary: FM4
        followers: [FM3]      # chord stabs: FM3 a third / fourth above FM4
      - primary: DAC
        followers: [PSG3]     # the hi-hat lands on every drum hit

With `convert.py --merged`, the followers are dropped from the output and the primary
channel plays **composite instruments** wherever a follower sounds with it: one MOD
instrument per distinct (primary instrument, follower voice, interval, detune, level)
combination, rendered on the YM2612 with one channel per voice keyed together
(`ym2612.renderer.render_layers`), so the chip sums and clips them exactly as the hardware
does.  A pair that is not two FM voices (DAC + PSG hi-hat, FM + PSG tone) is mixed from the
two finished samples instead, the follower resampled by the period ratio of the two notes.

What merges, per primary note-on at tick t (a follower note-on within `merge_tolerance` ticks
counts as at t; a grace note and the smpsNoAttack note it bends into are one note at the
target pitch), and per follower note-on while the primary is silent:
  - a follower note-on at t with the same duration        → composite (the usual case); a
                                                             mixed (non-chip) composite needs no
                                                             equal durations: each sample plays
                                                             out as it is
  - a follower note-on at t that is longer                 → composite; the follower's tail is
                                                             cut where the primary's next event
                                                             falls (counted as `truncated`) or
                                                             re-attacked with the primary's next
                                                             note (`held`)
  - a follower note-on at t that is shorter                → composite, the follower keyed off at
                                                             its duration inside it (`shorter`, a
                                                             chip layer or a cut mix layer)
  - no follower note-on, follower resting                  → the primary alone (right)
  - no follower note-on, follower still sounding           → the primary alone (`held`: the ring
                                                             is lost)
  - a follower note-on while the primary sounds            → lost (`orphan`); with the group's
                                                             `cut_primary: true` it plays and cuts
                                                             the primary's tail instead (`cuts`),
                                                             what a hi-hat does to a drum's decay
                                                             on a 4-channel Amiga
  - a follower note-on while the primary is silent         → placed on the merged channel as
                                                             the follower's own note (`solo`);
                                                             a primary note-on before it ends
                                                             re-takes the channel (`solo_cut`)
`tools/merge_survey.py` measures every channel pair of a song against these rules before a
group is written; the converter reports the same counts for the groups it was given.

Effects on the merged channel are the primary's: its vibrato, note fill, volume and delay
apply to the composite as a whole.  A follower whose modulation differs is counted
(`vibrato`) but not reproduced.

The fill pool (`merge_fill: [PSG1, PSG2]`, and a group's `fill_lost: true` for the follower
notes it cannot fold) places notes on ANY output channel that is silent when they start, not
only on their group's primary - what puts a chime on the bass channel between bass notes and
the drums into a 3-channel version.  A pool note takes the channel that stays silent longest
(the whole note where one can; a channel whose next note-on cuts it is second choice, and a
note that would get less than `fill_min_ticks` is not placed); a note with no silent channel
is lost.  Pool notes are spliced in as solo notes are, so they keep their own instrument,
level and pitch.

The plan is built once the song's ticks are final (after the global duration divider and the
loop extension), before the samples are rendered — the FM composites are entries in the
instrument catalogue (`core.instruments`).
`walk_channel` reads the plan from `config.merge_plan`, so every pass (levels, sustain,
conversion) sees the composite instruments the same way.

Per-pattern folds, sample banks, unison chords and same-shape twins: docs/pipeline.md
§ Channel merging.  The parts, in the order a merged build runs them:

    prepare_merged_config   the config's channels → the merged build's columns
    channel_notes           a channel's notes as the converter places them (NoteOn)
    pair_channels           a follower against its primary (PairStats)
    build_merge_plan        → MergePlan: composites, unisons, solo notes, slots
      _Planner                collect → pair → fill pool → fold → settle
      fit_composites         slots; a twin gives its slot up first, stand-ins take its notes
    mix_pcm_composites      the mixed composites' samples (_Mixer); pack_banks (banks.py) packs the banked
    MergedBuild             inside one conversion: composite volumes, mixes, banks (build.py)
"""

from .banks import ALIGN, pack_banks
from .build import MergedBuild, bank_reserve_wanted, report_plan
from .mix import composite_dither, mix_pcm_composites
from .model import NO_SLOT, Composite, MergePlan
from .notes import (
    CHIP,
    MIX,
    CompositeKey,
    MixLayerKey,
    NoteOn,
    PairStats,
    channel_notes,
    composite_key,
    keyoff_secs,
    pair_channels,
    unison_gain_db,
)
from .plan import build_merge_plan, column_sources, prepare_merged_config
from .slots import drop_composite, stand_in, trigger_note

__all__ = [
    "ALIGN", "CHIP", "MIX", "NO_SLOT", "Composite", "CompositeKey", "MergePlan", "MergedBuild", "MixLayerKey",
    "NoteOn", "PairStats", "bank_reserve_wanted", "build_merge_plan", "channel_notes", "column_sources",
    "composite_dither", "composite_key", "drop_composite", "keyoff_secs", "mix_pcm_composites", "pack_banks",
    "pair_channels", "prepare_merged_config", "report_plan", "stand_in", "trigger_note", "unison_gain_db"
]
