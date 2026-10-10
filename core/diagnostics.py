"""What a conversion reports: warnings (problems, each with its fix in core/ui/report.py) and infos
(what was decided).  Every part of the converter writes here; convert.py's report reads it.

    diag.warn(WarningKind.CLAMP_HIGH, channel='FM1', ...)   ->  {'type': WarningKind.CLAMP_HIGH, 'channel': 'FM1', ...}

The kinds are StrEnums: a record's 'type' compares, hashes and prints as its value.
"""

from enum import StrEnum


class WarningKind(StrEnum):
    # pitch
    CLAMP_HIGH = 'clamp_high'                       # a note above the MOD's three octaves
    CLAMP_LOW = 'clamp_low'
    MAP_GAP = 'map_gap'                             # a note in no range of its voice's voice_map
    MISSING_SOURCE = 'missing_source'               # a channel's source is not in the song
    SYNTH_ROOT_AMBIGUOUS = 'synth_root_ambiguous'   # an entry's low note plays at several chip pitches
    DETUNE_NO_SLOT = 'detune_no_slot'               # detune variants with no free slot
    COPY_NO_SLOT = 'copy_no_slot'                   # voice copies (special mode, LFO) a minimal config plays as their voice
    # samples
    RATE3_SYNTH_ROOT = 'rate3_synth_root'           # a rate-3 synth_root outside the driver's PSG table
    NOISE_ENVELOPES = 'noise_envelopes'             # one noise sample for several envelopes
    SUSTAIN_SHORT = 'sustain_short'                 # a sample shorter than a note it plays
    SAMPLE_TRUNCATED = 'sample_truncated'           # a render cut to the sample limit
    SAMPLE_FILE_MISSING = 'sample_file_missing'     # a sample_list file not on disk: the slot stays empty
    VOICE_MISSING = 'voice_missing'                 # a map entry's voice the song does not define
    RENDER_LEVEL = 'render_level'                   # a sample rendered at another level than it is baked at
    # tempo and patterns
    TEMPO_NO_SLOT = 'tempo_no_slot'                 # a tempo Fxx found no free effect slot
    TEMPO_BPM_RANGE = 'tempo_bpm_range'             # a tempo change outside BPM 32..255
    PATTERN_OVERFLOW = 'pattern_overflow'           # a channel ran past max_patterns
    REST_NO_SLOT = 'rest_no_slot'                   # a leading rest's C00 found no slot
    LOOP_NO_SLOT = 'loop_no_slot'                   # the loop's Bxx found no free slot
    LOOP_DRIFT = 'loop_drift'                       # tracks' loops too far apart to unroll: out of step
    # merged build
    MERGE_LOST = 'merge_lost'                       # follower notes a fold loses or cuts
    MERGE_HEADROOM = 'merge_headroom'               # composites clamped past full scale
    MERGE_UNSUPPORTED = 'merge_unsupported'         # composites without a slot
    MERGE_MISSING_SAMPLE = 'merge_missing_sample'   # a mix whose source has no sample
    MERGE_FILL_LOST = 'merge_fill_lost'             # fill-pool notes with no silent channel
    MERGE_DROPPED = 'merge_dropped'                 # notes no group folds and no column holds
    MERGE_BANK_DROPPED = 'merge_bank_dropped'       # a bank sound left out
    MERGE_BANK_IDLE = 'merge_bank_idle'             # a reserved bank slot left empty
    MERGE_UNSPECIFIED = 'merge_unspecified'         # patterns no merge_patterns block names


class InfoKind(StrEnum):
    # song and timing
    LOOP_EXTENDED = 'loop_extended'
    TEMPO_DIV_CHANGE = 'tempo_div_change'
    TEMPO_CHANGE = 'tempo_change'
    LOOP_SET = 'loop_set'
    NARROWED = 'narrowed'
    VIBRATO_RATE_LIMIT = 'vibrato_rate_limit'
    # pitches and detune
    SYNTH_ROOTS = 'synth_roots'
    SYNTH_SHIFT = 'synth_shift'
    DETUNE_VARIANTS = 'detune_variants'
    DETUNE_TIES = 'detune_ties'
    # samples
    AUTO_SUSTAIN_FM = 'auto_sustain_fm'
    AUTO_SUSTAIN_PSG = 'auto_sustain_psg'
    RENDER_CACHE = 'render_cache'
    RATE3_DIVIDER = 'rate3_divider'
    NOISE_ENVELOPE = 'noise_envelope'
    DAC_SATURATED = 'dac_saturated'
    # merged build
    MERGE_FILL = 'merge_fill'
    MERGE_FOLDS = 'merge_folds'
    MERGE_UNUSED = 'merge_unused'
    MERGE_SLOTS = 'merge_slots'
    MERGE_GROUP = 'merge_group'
    MERGE_LIMITED = 'merge_limited'
    MERGE_UNISON_VOLUME = 'merge_unison_volume'
    MERGE_BANK = 'merge_bank'
    MERGE_BANK_NOTES = 'merge_bank_notes'
    MERGE_BANK_RETRY = 'merge_bank_retry'
    MERGE_BANK_SLOTS = 'merge_bank_slots'


class Diagnostics:
    def __init__(self) -> None:
        self.warnings: list[dict] = []
        self.infos: list[dict] = []
        self._seen: set = set()

    def warn(self, kind: WarningKind, /, **fields) -> None:
        """A warning, once per (kind, channel, context, note): a note clamped 40 times is one line."""
        assert 'type' not in fields, fields
        key = (
            kind,
            fields.get('channel'),
            fields.get('extra_ctx'),
            fields.get('src_name') or fields.get('note_name') or fields.get('source'),
        )
        if key in self._seen:
            return
        self._seen.add(key)
        self.warnings.append({'type': kind, **fields})

    def info(self, kind: InfoKind, /, **fields) -> None:
        assert 'type' not in fields, fields
        self.infos.append({'type': kind, **fields})

    def remap_instruments(self, mapping: dict[int, int]) -> None:
        """Renumber the instrument slots every warning and info names (ModFile.compact_samples):
        a slot under `instrument`, `missing` or `slot`, an "instrument N" context, and the items of
        an `instruments` / `composites` list (a slot, or a tuple that starts with one).  A count
        under the same names stays as it is."""
        def slot(v):
            return mapping.get(v, v) if isinstance(v, int) and not isinstance(v, bool) else v

        def item(v):
            if isinstance(v, (tuple, list)) and v:
                return type(v)([slot(v[0]), *v[1:]])
            return slot(v)

        for rec in (*self.warnings, *self.infos):
            for key in ("instrument", "missing", "slot"):
                if key in rec:
                    rec[key] = slot(rec[key])
            for key in ("instruments", "composites"):
                if isinstance(rec.get(key), list):
                    rec[key] = [item(v) for v in rec[key]]
            ctx = rec.get("extra_ctx")
            if isinstance(ctx, str) and ctx.startswith("instrument ") and ctx[11:].isdigit():
                rec["extra_ctx"] = f"instrument {slot(int(ctx[11:]))}"

    def infos_of(self, kind: InfoKind) -> list[dict]:
        return [i for i in self.infos if i['type'] == kind]

    def first_info(self, kind: InfoKind) -> dict | None:
        return next((i for i in self.infos if i['type'] == kind), None)
