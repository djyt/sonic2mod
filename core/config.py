"""Per-song conversion configuration."""

import os
import warnings
from dataclasses import dataclass, field
from typing import Any

from .mod import PAL_AMIGA_CLOCK, ModFile
from .pcm import DEFAULT_DITHER, DITHER_MODES, sample_limit_bytes
from .resample import DEFAULT_TAPS
from .tables import MOD_NOTE_MAP, ModNote, parse_smps_note, parse_synth_note, synth_note_name


@dataclass
class InstrumentRange:
    low: int            # inclusive lower bound — SMPS semitone from C0 (e.g. nA2 = 33)
    high: int           # inclusive upper bound — SMPS semitone from C0
    mod_instrument: int # MOD instrument number (1-31)
    root: ModNote | None = None  # MOD note where `low` plays;
                                    # out_note = root + (source_semitone - low)
                                    # if None: fall back to channel transpose for note
    synth_root: int | None = None  # SMPS semitone the sample is rendered at.  None in a config:
                                      # core.driver_state.resolve_synth_roots fills in the chip
                                      # pitch the entry's notes play most often (the song decides).
                                      # Stated: the rendering pitch, anywhere in the range.  Either
                                      # way `root` is where the pitch of `low` sounds (synth_shift)
    synth_shift: int = 0           # synth_root − the pitch `root` sounds, set by resolve_synth_roots;
                                      # the sample's rate is 2^(shift/12) times root's playback rate
    vibrato: int | None = None     # per-entry 4xy override; None = use channel smpsModSet
                                      # stored as raw byte: high nibble=speed, low nibble=depth
                                      # 0x00 = suppress; e.g. 0x12 = speed=1, depth=2
    loop_drift_db: float | None = None   # this instrument's sustain loop may freeze this far above the
                                      # settled level (the song's / settings.yaml's otherwise): lower
                                      # loops later, past more of the attack, for more bytes
    loop_min_ms: float | None = None     # its sustain loop is at least this long (core.loops' 30 ms
                                      # otherwise): a longer loop keeps a detuned voice's shimmer
                                      # moving where a short one freezes it into a buzz
    dither: str | None = None            # this sample's quantisation (core.pcm.DITHER_MODES); None:
                                      # settings.yaml samples.dither


def _parse_vibrato(v) -> int:
    """Parse a vibrato value from YAML (int or str) → raw byte (high=speed, low=depth).

    The value is treated as two ASCII hex digits (each nibble is a hex digit 0–F):
      vibrato: 12   (YAML int 12)  → str(12)="12" → speed=1, depth=2 → 0x12
      vibrato: "1A" (YAML string)  → upper="1A"   → speed=1, depth=10 → 0x1A
      vibrato: 0    (suppress)     → "00" → speed=0, depth=0
    """
    s = str(v).upper().zfill(2)
    if len(s) > 2:
        raise ValueError(f"vibrato value '{v}' exceeds 2 hex digits")
    return (int(s[0], 16) << 4) | int(s[1], 16)


def _mode_word(v) -> str:
    """A mode setting's value as written: YAML 1.1 reads a bare `off` as false (and `on` as true)."""
    if isinstance(v, bool):
        return "on" if v else "off"
    return str(v).lower()


def _dither(v, context: str) -> str:
    """A `dither` value: one of core.pcm.DITHER_MODES."""
    mode = _mode_word(v)
    if mode not in DITHER_MODES:
        raise ValueError(f"{context}: dither must be one of {', '.join(DITHER_MODES)} (got {v!r})")
    return mode


def _opt(d: dict, key: str, parse_fn):
    """Return parse_fn(d[key]) if key is present, otherwise None."""
    return parse_fn(d[key]) if key in d else None


def _require(d: dict, key: str, context: str):
    """Return d[key], raising ValueError with location context if key is missing."""
    if key not in d:
        raise ValueError(f"Config error: '{key}' is required in {context}")
    return d[key]


def _mod_note(v: str, context: str) -> 'ModNote':
    """Parse a ModNote by name, raising ValueError with context on failure."""
    try:
        return ModNote[v]
    except KeyError:
        valid = ', '.join(list(ModNote.__members__)[:8]) + ', ...'
        raise ValueError(f"Unknown note name '{v}' in {context}; valid names: {valid}") from None


def _parse_instrument_range(entry: dict, context: str = "voice_map entry") -> "InstrumentRange":
    """Parse a single InstrumentRange dict from YAML.

    Accepts both new key ``mod_instrument`` and deprecated ``instrument``
    (emits DeprecationWarning for the latter).
    """
    low  = parse_smps_note(_require(entry, 'low',  context))
    high = parse_smps_note(_require(entry, 'high', context))

    if 'mod_instrument' in entry:
        inst = entry['mod_instrument']
    elif 'instrument' in entry:
        warnings.warn(
            "YAML key 'instrument' in a range entry is deprecated; use 'mod_instrument'.",
            DeprecationWarning,
            stacklevel=4,
        )
        inst = entry['instrument']
    else:
        raise ValueError(f"Config error: 'mod_instrument' is required in {context}")

    root       = _opt(entry, 'root',       lambda v: _mod_note(v, f"{context}.root"))
    synth_root = _opt(entry, 'synth_root', parse_synth_note)
    vibrato    = _opt(entry, 'vibrato',    _parse_vibrato)
    drift      = _opt(entry, 'loop_drift_db', lambda v: _drift_db(v, context))
    min_ms     = _opt(entry, 'loop_min_ms', lambda v: _loop_min_ms(v, context))
    dither     = _opt(entry, 'dither', lambda v: _dither(v, context))

    return InstrumentRange(
        low=low, high=high, mod_instrument=inst, root=root, synth_root=synth_root,
        vibrato=vibrato, loop_drift_db=drift, loop_min_ms=min_ms, dither=dither,
    )


def _loop_min_ms(v, context: str) -> float:
    """A `loop_min_ms` override: milliseconds, positive."""
    try:
        ms = float(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{context}: loop_min_ms must be a number of milliseconds (got {v!r})") from e
    if ms <= 0:
        raise ValueError(f"{context}: loop_min_ms must be positive (got {ms})")
    return ms


def _drift_db(v, context: str) -> float:
    """A `loop_drift_db` override: dB, not negative."""
    try:
        db = float(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{context}: loop_drift_db must be a number of dB (got {v!r})") from e
    if db < 0:
        raise ValueError(f"{context}: loop_drift_db must not be negative (got {db})")
    return db



def load_yaml(stream):
    """yaml.safe_load that refuses a mapping with a key given twice.

    PyYAML keeps the last value silently, so a merge group written without its leading `- `
    ("primary: FM5" under the group above) rewrote that group's primary and column and the
    chords came out as an FM5 mix on the arp column; now it is an error at the line.
    """
    import yaml

    class _Strict(yaml.SafeLoader):
        pass

    def _mapping(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise ValueError(f"line {key_node.start_mark.line + 1}: key {key!r} is given twice in one mapping "
                                 f"(a list item missing its leading '- ' merges into the item above)")
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)
    _Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    return yaml.load(stream, Loader=_Strict)


@dataclass
class MergeGroup:
    """One `merge:` group: the followers fold onto the primary's MOD channel (core/merge/)."""
    primary: str
    followers: list[str]
    cut_primary: bool = False   # a follower note that starts while the primary still sounds cuts it
                                # (a hi-hat over a drum's tail) instead of being lost
    max_composites: int | None = None   # keep only the N most-played composites (the rest of the
                                        # chords play the primary alone): a memory budget
    fill_lost: bool = False     # a follower note the group cannot fold (an orphan, a shorter one)
                                # goes to the fill pool: any output channel silent at that moment
    fill_cut: bool = False      # a follower note whose ring the fold would cut (the primary's next
                                # note-on or rest falls inside it) plays whole on a channel silent
                                # for all of it when there is one; else it folds as before
    bank: bool = False          # this group's mixed composites share MOD instruments as sample banks,
                                # each sound chosen with 9xx (core/banks.py), which takes the note's
                                # effect slot (a melodic note's attack-row Cxx moves a row later)
    mod_channel: int | str | None = None   # merge_patterns only: the column the primary's notes take
                                # in the group's patterns — a channels: mod_channel number, or a source
                                # name (FM2: that channel's column), which must be folded or dropped
                                # there; resolved to `route`, the merged output channel.  A group with
                                # no followers and a mod_channel only moves the channel there
    route: int | None = None    # set by core.merge.prepare_merged_config
    cut_after: int | None = None   # merge_patterns only: in the block's patterns a pooled note may take
                                # this group's column once its note is that many ticks old, cutting the
                                # tail (the block's own merge_fill_cut_after for the primary's column)
    fill: bool = False          # merge_patterns only, no followers: the primary's notes in the block's
                                # patterns go to the fill pool - each on whichever column in use there is
                                # silent when it starts (sprinkled between the others' notes), lost where
                                # none is; the channel has no column of its own in those patterns
    mix_at: str | None = None   # "primary": a mixed composite is made at the primary's own note, so a
                                # looped primary keeps its loop (a lead under a chime: a few KB instead
                                # of the whole note unrolled); the followers are resampled down into it
    mix_note: int | None = None # highest MOD note (index, C1 = 0) a mixed composite of this group is
                                # made at: a mix is made at its fastest layer's note (a hat's A3, 28 kHz)
                                # unless that is above this; F2 halves the drum mixes' bytes and more
    loop_drift_db: float | None = None  # the sustain loop drift of this group's chip composites (the
                                # primary's entry's, then the song's, otherwise)
    loop_min_ms: float | None = None    # the shortest sustain loop of this group's chip composites
                                # (and of its looped mixes, loop_mix)
    treble_shelf_db: float | None = None   # a brightness shelf on this group's composites (the mixed
                                # sum, drums off disk included; a chip composite's render), on top of
                                # any song shelf; treble_shelf_hz its corner (None: the settings')
    treble_shelf_hz: float | None = None
    limit_db: float | None = None   # a mix of this group whose sum is past full scale has its peaks
                                # limited, by up to this many dB, instead of the whole sound turned
                                # down (core.pcm.limit_peaks); denser, a little transient distortion
    dither: str | None = None   # this group's composites' quantisation (core.pcm.DITHER_MODES);
                                # None: settings.yaml samples.dither
    loop_mix: bool = False      # a long mix of this group loops where its sum settles, found in the
                                # finished mix as a single voice's loop is (lossy: the chord's slow
                                # movement freezes there); for a pitched primary
    patterns: frozenset | None = None   # the MOD patterns (of the reference build) this group folds in;
                                        # None = the whole song.  A `merge_patterns:` group has one.

    @property
    def label(self) -> str:
        return f"{self.primary}+{'+'.join(self.followers)}" if self.followers else self.primary

    @property
    def where(self) -> str:
        """The group's pattern ranges as the config writes them (" [1-4, d-10]"), "" song-wide."""
        return f" [{format_patterns(self.patterns)}]" if self.patterns is not None else ""

    def covers(self, pattern: int) -> bool:
        return self.patterns is None or pattern in self.patterns


def parse_patterns(spec, ctx: str) -> frozenset:
    """The MOD pattern numbers a `merge_patterns:` block names.

    A string is hex, as Fast Tracker and the fold table write pattern numbers: "0", "d", a
    range "1-4" / "d-10", or several separated by commas ("0, 5-c").  A YAML integer is taken
    as decimal.  A list is the union of its items.
    """
    if isinstance(spec, bool):
        raise ValueError(f"{ctx}: patterns must be pattern numbers, not {spec!r}")
    if isinstance(spec, int):
        if spec < 0:
            raise ValueError(f"{ctx}: pattern {spec} is negative")
        return frozenset({spec})
    if isinstance(spec, (list, tuple)):
        out: set = set()
        for item in spec:
            out |= parse_patterns(item, ctx)
        return frozenset(out)
    if not isinstance(spec, str):
        raise ValueError(f"{ctx}: patterns must be a string, a number or a list (got {spec!r})")
    out = set()
    for raw_token in spec.split(","):
        token = raw_token.strip().lower()
        if not token:
            continue
        lo, dash, hi = token.partition("-")
        try:
            a = int(lo.strip(), 16)
            b = int(hi.strip(), 16) if dash else a
        except ValueError:
            raise ValueError(f"{ctx}: '{token}' is not a hex pattern number or range (like 0, a, d-10)") from None
        if b < a:
            raise ValueError(f"{ctx}: pattern range '{token}' runs backwards")
        out.update(range(a, b + 1))
    if not out:
        raise ValueError(f"{ctx}: no patterns in {spec!r}")
    return frozenset(out)


def format_patterns(patterns) -> str:
    """Pattern numbers as hex ranges: {1,2,3,4,13,14,15,16} -> "1-4, d-10"."""
    nums = sorted(patterns)
    parts: list[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(f"{nums[i]:x}" if i == j else f"{nums[i]:x}-{nums[j]:x}")
        i = j + 1
    return ", ".join(parts)


def _parse_merge_group(g, ctx: str, patterns=None) -> "MergeGroup":
    if not isinstance(g, dict):
        raise ValueError(f"{ctx}: a merge group is a mapping with primary: and followers:")
    followers = g.get('followers', [])
    if isinstance(followers, str):
        followers = [followers]
    _mc = g.get('max_composites')
    mix_at = g.get('mix_at')
    if mix_at is not None and str(mix_at).lower() != "primary":
        raise ValueError(f"{ctx}: mix_at must be 'primary' (got {mix_at!r})")
    mix_note = None
    if g.get('mix_note') is not None:
        note = MOD_NOTE_MAP.get(str(g['mix_note']))
        if note is None:
            raise ValueError(f"{ctx}: mix_note {g['mix_note']!r} is not a MOD note (C1 .. B3, like F2 or Fs2)")
        mix_note = note.value
    cut_after = g.get('cut_after')
    if cut_after is not None:
        if patterns is None:
            raise ValueError(f"{ctx}: cut_after is a merge_patterns option (song-wide: merge_fill_cut_after)")
        cut_after = max(1, int(cut_after))
    fill = bool(g.get('fill', False))
    if fill and (followers or g.get('mod_channel') is not None or patterns is None):
        raise ValueError(f"{ctx}: fill: true is for a merge_patterns group with no followers and no mod_channel "
                         f"(the channel's notes go wherever a column is silent)")
    target = g.get('mod_channel')
    if target is not None and not isinstance(target, (int, str)):
        raise ValueError(f"{ctx}: mod_channel is a channels: mod_channel number or a source name (got {target!r})")
    if target is not None and patterns is None:
        raise ValueError(f"{ctx}: mod_channel needs a merge_patterns block — a song-wide group has no column to borrow")
    return MergeGroup(str(_require(g, 'primary', ctx)), [str(f) for f in followers],
                      bool(g.get('cut_primary', False)),
                      int(_mc) if _mc is not None else None,
                      bool(g.get('fill_lost', False)),
                      bool(g.get('fill_cut', False)),
                      bool(g.get('bank', False)),
                      mod_channel=target, mix_note=mix_note, patterns=patterns, fill=fill,
                      cut_after=cut_after, mix_at=(str(mix_at).lower() if mix_at is not None else None),
                      loop_drift_db=_opt(g, 'loop_drift_db', lambda v: _drift_db(v, ctx)),
                      loop_min_ms=_opt(g, 'loop_min_ms', lambda v: _loop_min_ms(v, ctx)),
                      loop_mix=bool(g.get('loop_mix', False)),
                      treble_shelf_db=_opt(g, 'treble_shelf_db', float),
                      treble_shelf_hz=_opt(g, 'treble_shelf_hz', float),
                      limit_db=_opt(g, 'limit_db', lambda v: max(0.0, float(v))),
                      dither=_opt(g, 'dither', lambda v: _dither(v, ctx)))


@dataclass
class ChannelConfig:
    source: str          # "DAC", "FM1"-"FM5", "PSG1"-"PSG3"
    mod_channel: int     # 0-based MOD channel index
    transpose: int = 0   # Semitone offset
    instrument: int = 1  # MOD instrument number (1-31)
    volume: int = 64     # MOD volume (0-64)
    enabled: bool = True


@dataclass
class DacSampleConfig:
    name: str            # e.g. "dKick"
    mod_instrument: int  # MOD instrument number
    mod_note: str = "C3" # Note to trigger in MOD
    saturate_db: float = 0.0   # the drum soft-clipped until its body is this much louder at the same
                               # peak (core.pcm.saturate): presence, some added harmonics
    merge_saturate_db: float | None = None   # the same for the merged build only (over saturate_db there)

    def saturation_db(self, merged: bool) -> float:
        """The saturate_db this build uses: merge_saturate_db in the merged one, where given."""
        return self.merge_saturate_db if merged and self.merge_saturate_db is not None else self.saturate_db


@dataclass
class PsgInstrumentEntry:
    mod_instrument: int                      # MOD slot (1-based)
    type: str                                # "tone" | "white_noise" | "periodic_noise"
    root: 'ModNote'                          # MOD note anchor; determines target_rate + WHERE sample triggers
    synth_root: int | None = None        # SMPS semitone the tone is rendered at.  None in a config:
                                         # resolve_synth_roots fills in the pitch the chip plays
                                         # (see InstrumentRange.synth_root); a rate-3 noise entry
                                         # keeps None (the LFSR divider is derived separately)
    synth_shift: int = 0                 # synth_root − the pitch `root` sounds, set by resolve_synth_roots;
                                         # the sample's rate is 2^(shift/12) times root's playback rate
    low: int | None = None               # SMPS semitone lower bound for melodic root offset
    high: int | None = None              # SMPS semitone upper bound (inclusive); used for list-entry range dispatch
    noise_rate: int = 0                      # Only for noise types: 0, 1, 2 (preset dividers), 3 = follow tone ch2
    tone2_n: int | None = None           # noise_rate 3 only: explicit tone-ch2 divider N (1–1023) for the LFSR
                                             # clock; overrides the value derived from synth_root/root.
                                             # nMaxPSG in the Sonic 1 driver writes N=0, which the Sega
                                             # VDP PSG treats as N=1 (maximum shift rate) — use tone2_n: 1.
    envelope: str | list[int] | None = None  # Named table str ("fTone_04") or inline list[int].  A psg_map
                                             # (noise) entry leaves it None: the converter derives it from
                                             # the song (derive_noise_envelopes); stating it is an override
    base_volume: int = 0                     # SN76489 base attenuation (0=max, 15=silent)
    vibrato: int | None = None           # per-entry 4xy override; same semantics as InstrumentRange.vibrato
    envelopes: dict[str, int] = field(default_factory=dict)   # psg_map only: {smpsPSGvoice label: MOD
                                             # instrument} — a noise-mode envelope that gets its own sample
                                             # (Scrap Brain's fTone_08); other labels play mod_instrument
    dither: str | None = None                # as InstrumentRange.dither


def _parse_psg_voice_entry(v: dict, default_envelope: str, context: str = "psg_voice_map entry") -> 'PsgInstrumentEntry':
    """Parse a single psg_voice_map entry dict into a PsgInstrumentEntry (always a tone).

    A noise channel never consults psg_voice_map: once smpsPSGform ran, smpsPSGvoice only
    changes the envelope, and an envelope that needs its own sample is named under
    psg_map[<form>].envelopes.  A noise type here is therefore a config error.
    """
    if v.get('type', 'tone') != 'tone' or 'noise_rate' in v:
        raise ValueError(
            f"{context}: psg_voice_map entries are tones; a noise-mode envelope variant goes under "
            f"psg_map[<form byte>].envelopes: {{<label>: <mod_instrument>}}")
    root_note   = _mod_note(_require(v, 'root', context), f"{context}.root")
    synth_root  = _opt(v, 'synth_root', parse_synth_note)
    low         = _opt(v, 'low',        parse_smps_note)
    high        = _opt(v, 'high',       parse_smps_note)
    pvm_vibrato = _opt(v, 'vibrato',    _parse_vibrato)
    return PsgInstrumentEntry(
        mod_instrument=_require(v, 'mod_instrument', context),
        type=v.get('type', 'tone'),
        root=root_note,
        synth_root=synth_root,
        low=low,
        high=high,
        noise_rate=v.get('noise_rate', 0),
        tone2_n=_parse_tone2_n(v, context),
        envelope=v.get('envelope', default_envelope),
        base_volume=v.get('base_volume', 0),
        vibrato=pvm_vibrato,
        dither=_opt(v, 'dither', lambda d: _dither(d, context)),
    )


# The driver's PSGFrequencies table spans 130.98 Hz … 6580.02 Hz = synth_root C3 … Gs8 (indices
# 0–68).  Its one remaining entry, index 69 (nMaxPSG), is not a pitch: the divider is 0, which
# the Sega VDP PSG clocks as N=1.
_RATE3_SYNTH_ROOT_MIN = 36     # C3
_RATE3_SYNTH_ROOT_MAX = 104    # Gs8


def rate3_synth_root_issues(config: 'ConversionConfig') -> list[dict]:
    """Rate-3 noise entries whose ``synth_root`` is a frequency the driver can never write.

    With ``noise_rate: 3`` the LFSR is clocked by tone channel 2, and ``synth_root`` is turned
    into that channel's divider.  A value outside the driver's table (``A8`` is the usual one —
    where index 69 would fall if the table were chromatic) gives a plausible-looking but wrong
    LFSR clock: ~7 kHz instead of the ~112 kHz of nMaxPSG, a dull rattle instead of hiss.
    Entries with an explicit ``tone2_n`` are exempt (it overrides ``synth_root``).

    Returns one dict per offending entry: ``{'context', 'synth_root', 'above'}``.
    """
    entries: list[tuple[str, PsgInstrumentEntry]] = [
        (f"psg_map[0x{form:02X}]", e) for form, e in config.psg_map.items()
    ]
    for label, lst in config.psg_voice_map.items():
        entries.extend((f"psg_voice_map[{label}]", e) for e in lst)

    issues = []
    for context, e in entries:
        if e.noise_rate != 3 or e.tone2_n is not None or e.synth_root is None:
            continue
        if _RATE3_SYNTH_ROOT_MIN <= e.synth_root <= _RATE3_SYNTH_ROOT_MAX:
            continue
        issues.append({
            'context': context,
            'synth_root': synth_note_name(e.synth_root),
            'above': e.synth_root > _RATE3_SYNTH_ROOT_MAX,
        })
    return issues


def _parse_tone2_n(entry: dict, context: str) -> int | None:
    """Validate the optional ``tone2_n`` key (SN76489 tone-ch2 divider, 1–1023)."""
    if 'tone2_n' not in entry or entry['tone2_n'] is None:
        return None
    n = int(entry['tone2_n'])
    if not 1 <= n <= 1023:
        raise ValueError(f"{context}.tone2_n must be 1–1023 (got {n})")
    return n


def _psg_volume_mode(value) -> str:
    v = str(value).strip().lower()
    if v not in ("baked", "absolute"):
        raise ValueError(f"psg_volume_scaling must be baked or absolute (got '{value}')")
    return v


# settings.yaml `samples:` keys; each was top level before it
SAMPLE_KEYS = ("max_sample_kb", "pt_zero_bytes", "dither", "dc_block", "sustain_loops", "loop_drift_db",
               "treble_shelf_db", "treble_shelf_hz", "resample_taps")


def _samples_section(data: dict, filepath: str) -> dict:
    """settings.yaml `samples:`.  A key still at the top level counts, with a warning; an unknown
    one is an error (a typo would be ignored silently)."""
    section = dict(data.get("samples") or {})
    unknown = sorted(set(section) - set(SAMPLE_KEYS))
    if unknown:
        raise ValueError(f"{filepath}: unknown samples key(s): {', '.join(unknown)}")

    for key in SAMPLE_KEYS:
        if key in data and key not in section:
            warnings.warn(f"{filepath}: {key} moved to samples.{key}", stacklevel=3)
            section[key] = data[key]
    return section


def _max_sample_kb(data: dict, filepath: str) -> int:
    """`samples.max_sample_kb` of settings.yaml, validated (128 default)."""
    kb = data.get("max_sample_kb", 128)
    try:
        sample_limit_bytes(kb)
    except ValueError as e:
        raise ValueError(f"{filepath}: {e}") from e
    return kb


def _sample_flag(data: dict, key: str, default: bool, filepath: str) -> bool:
    """A true / false key of settings.yaml `samples:`."""
    v = data.get(key, default)
    if not isinstance(v, bool):
        raise ValueError(f"{filepath}: {key} must be true or false (got {v!r})")
    return v


SUSTAIN_LOOP_MODES = ("off", "merged", "all")


def _sustain_loops(data: dict, filepath: str) -> str:
    """`samples.sustain_loops` of settings.yaml: off | merged (default) | all."""
    v = _mode_word(data.get("sustain_loops", "merged"))
    if v not in SUSTAIN_LOOP_MODES:
        raise ValueError(f"{filepath}: sustain_loops must be one of {', '.join(SUSTAIN_LOOP_MODES)} (got '{v}')")
    return v


LEGATO_MODES = ("strict", "loose", "retrigger")
TWIN_MODES = ("short", "always")    # merge_twins: when a same-shape twin gives up its slot


def _legato(data: dict, filepath: str) -> str:
    """Top-level `legato` of settings.yaml: retrigger (default) | strict | loose."""
    v = str(data.get("legato", "retrigger")).lower()
    if v not in LEGATO_MODES:
        raise ValueError(f"{filepath}: legato must be one of {', '.join(LEGATO_MODES)} (got '{v}')")
    return v


# The tracker a build is made for (settings.yaml `player`): FT2 clone and ProTracker 2 scale a 4xy
# depth differently, see vibrato_depth
PLAYERS = ("ft2", "pt2")


def _player(data: dict, filepath: str) -> str:
    """Top-level `player` of settings.yaml: ft2 (default) | pt2."""
    v = str(data.get("player", "ft2")).lower()
    if v not in PLAYERS:
        raise ValueError(f"{filepath}: player must be one of {', '.join(PLAYERS)} (got '{v}')")
    return v


# The treble shelf's corner (settings.yaml treble_shelf_hz), at the sample's own pitch
DEFAULT_SHELF_HZ = 2500.0

# PSG tones render at this multiple of the sample's rate (settings.yaml psg_synthesis.oversample)
DEFAULT_PSG_OVERSAMPLE = 8

# PAL Amiga Paula clock (settings.yaml amiga_clock): a MOD note's rate is this / its period
DEFAULT_AMIGA_CLOCK = PAL_AMIGA_CLOCK


def _positive_int(data: dict, key: str, default: int, filepath: str, even: bool = False) -> int:
    """A settings.yaml count in `data`: an integer >= 1 (even when `even`)."""
    try:
        v = int(data.get(key, default))
    except (TypeError, ValueError) as e:
        raise ValueError(f"{filepath}: {key} must be an integer") from e
    if v < 1 or (even and v % 2):
        raise ValueError(f"{filepath}: {key} must be {'an even' if even else 'a'} whole number >= 1 (got {v})")
    return v


def _amiga_clock(data: dict, section: dict) -> int:
    """Top-level `amiga_clock` of settings.yaml (the Paula clock every sample rate follows); a
    synthesis section's own key, where the file still has one, is the fallback."""
    return int(data.get("amiga_clock", section.get("amiga_clock", DEFAULT_AMIGA_CLOCK)))


def _psg_oversample(data: dict, section: dict, filepath: str) -> int:
    """`psg_synthesis.oversample`; the top-level `psg_oversample` it replaced still counts, with a warning."""
    if "oversample" not in section and "psg_oversample" in data:
        warnings.warn(f"{filepath}: psg_oversample moved to psg_synthesis.oversample", stacklevel=3)
        return _positive_int(data, "psg_oversample", DEFAULT_PSG_OVERSAMPLE, filepath)
    return _positive_int(section, "oversample", DEFAULT_PSG_OVERSAMPLE, filepath)


def _treble_shelf(data: dict, filepath: str) -> tuple[float, float]:
    """`samples.treble_shelf_db` / `treble_shelf_hz` of settings.yaml: (gain, corner) of the
    optional high shelf on every synthesised render (core.pcm.high_shelf); 0 dB = off."""
    try:
        return float(data.get("treble_shelf_db", 0.0)), float(data.get("treble_shelf_hz", DEFAULT_SHELF_HZ))
    except (TypeError, ValueError) as e:
        raise ValueError(f"{filepath}: treble_shelf_db / treble_shelf_hz must be numbers") from e


def _loop_drift_db(data: dict, filepath: str) -> float:
    """`samples.loop_drift_db` of settings.yaml (1.0 default): how far a looped sample's level
    may sit above where the instrument's longest note would have decayed to."""
    try:
        v = float(data.get("loop_drift_db", 1.0))
    except (TypeError, ValueError) as e:
        raise ValueError(f"{filepath}: loop_drift_db must be a number of dB") from e
    if v < 0:
        raise ValueError(f"{filepath}: loop_drift_db must not be negative (got {v})")
    return v


@dataclass
class PsgSynthesisSettings:
    enabled: bool = False
    clock_rate: int = 3_579_545      # SN76489 NTSC MD clock (Hz)
    amiga_clock: int = DEFAULT_AMIGA_CLOCK   # settings.yaml amiga_clock (top level)
    sustain_duration: float | str = 1.0
    release_padding: float = 0.2
    # `auto` resolved: {instrument: seconds}, each instrument's own longest ring (the converter's
    # SustainPlanner.resolve); an instrument absent here gets sustain_duration.  Empty when a number is stated.
    sustain_by_instrument: dict = field(default_factory=dict)
    slide_ends: frozenset = frozenset()      # instruments a note of ends in a release slide (merged
                                             # build): only they are heard past their sustain
    exact_sustain: frozenset = frozenset()   # instruments whose auto sustain holds every note that
                                             # plays them: the sample ends where those notes stop
                                             # being heard (the release padding only a cut-short
                                             # note could reach is left off)
    # Envelope tables are not a setting: the driver's own live in core.driver_tables.PSG_ENVELOPES_BY_NAME.
    # PSG level model.  "baked": per instrument, the attenuation most of its notes play at needs no
    # command and is what the sample_list volume stands for; other notes get Cxx on the chip's
    # 2 dB/step law (same scheme as SynthesisSettings.fm_volume_mode).  "absolute": legacy —
    # volume = 64 × 10^(−2·att/20) × sample volume / 64, so a Cxx on nearly every PSG note.
    psg_volume_scaling: str = "baked"
    max_sample_kb: int = 128         # settings.yaml samples.max_sample_kb: 128 = the format's limit, 64 = ProTracker's
    # settings.yaml samples.sustain_loops: which builds cut each settled sample to a loop and
    # end its notes with a release slide (core.loops) - "off", "merged" (--merged only), "all".
    sustain_loops: str = "merged"
    loop_drift_db: float = 1.0       # settings.yaml samples.loop_drift_db: dB a loop may freeze above the
                                     # level the longest note would have decayed to (core.loops)
    treble_shelf_db: float = 0.0     # settings.yaml samples.treble_shelf_db: brightness shelf, 0 = off
    resample_taps: int = DEFAULT_TAPS  # settings.yaml samples.resample_taps: filter width, at the lower rate
    psg_oversample: int = DEFAULT_PSG_OVERSAMPLE   # settings.yaml psg_synthesis.oversample
    dither: str = DEFAULT_DITHER     # settings.yaml samples.dither (core.pcm.DITHER_MODES)
    dc_block: bool = False           # settings.yaml samples.dc_block: each render's DC removed (core.pcm.dc_block)
    treble_shelf_hz: float = DEFAULT_SHELF_HZ   # settings.yaml samples.treble_shelf_hz: its corner

    @property
    def max_sample_bytes(self) -> int:
        """Bytes one synthesised sample may hold (core.pcm.sample_limit_bytes)."""
        return sample_limit_bytes(self.max_sample_kb)

    def loops_for(self, merged: bool) -> bool:
        """True when this build (the merged one or the reference) gets sustain loops."""
        return self.sustain_loops == "all" or (self.sustain_loops == "merged" and merged)

    @classmethod
    def from_yaml(cls, filepath: str) -> 'PsgSynthesisSettings':
        import yaml
        try:
            with open(filepath) as f:
                data = load_yaml(f)
        except yaml.YAMLError as e:
            raise ValueError(f"YAML syntax error in '{filepath}': {e}") from e
        s = data.get("psg_synthesis", {})
        if "psg_envelope_tables" in s:
            warnings.warn(
                f"{filepath}: psg_synthesis.psg_envelope_tables is ignored — the envelopes come from "
                "the driver transcription in core/driver_tables.py (PSG_ENVELOPES_BY_NAME); delete the block",
                stacklevel=2,
            )
        for key in ("normalize_samples", "psg_output_max"):
            if key in s:
                warnings.warn(
                    f"{filepath}: psg_synthesis.{key} is ignored — every sample is peak-normalised "
                    "to its full 8 bits and the sample_list volume carries its level; delete the line",
                    stacklevel=2,
                )
        _psg_sd = s.get("sustain_duration", 1.0)
        smp = _samples_section(data, filepath)
        shelf_db, shelf_hz = _treble_shelf(smp, filepath)
        return cls(
            enabled=s.get("enabled", False),
            clock_rate=s.get("clock_rate", 3_579_545),
            amiga_clock=_amiga_clock(data, s),
            sustain_duration=_psg_sd if _psg_sd == "auto" else float(_psg_sd),
            release_padding=s.get("release_padding", 0.2),
            psg_volume_scaling=_psg_volume_mode(data.get("psg_volume_scaling", "baked")),
            max_sample_kb=_max_sample_kb(smp, filepath),
            sustain_loops=_sustain_loops(smp, filepath),
            loop_drift_db=_loop_drift_db(smp, filepath),
            treble_shelf_db=shelf_db,
            treble_shelf_hz=shelf_hz,
            resample_taps=_positive_int(smp, "resample_taps", DEFAULT_TAPS, filepath, even=True),
            psg_oversample=_psg_oversample(data, s, filepath),
            dc_block=_sample_flag(smp, "dc_block", False, filepath),
            dither=_dither(smp.get("dither", DEFAULT_DITHER), f"{filepath}: samples"),
        )


@dataclass
class SynthesisSettings:
    enabled: bool = False
    mode: str = "ym2612"
    clock_rate: int = 7_670_454       # YM2612 master clock
    amiga_clock: int = DEFAULT_AMIGA_CLOCK   # settings.yaml amiga_clock (top level)
    sustain_duration: float | str = 1.5
    release_padding: float = 0.5
    sustain_by_instrument: dict = field(default_factory=dict)   # as PsgSynthesisSettings
    exact_sustain: frozenset = frozenset()                      # as PsgSynthesisSettings
    slide_ends: frozenset = frozenset()                         # as PsgSynthesisSettings
    threads: int | str = "normal"     # Render threads: "normal" (cores − 1), "max" (all cores), or a count
    detune_variants: bool = True      # an smpsAlterNote note plays a sample rendered at its FNUM offset (core.detune)
    # FM level model — see fm_volume_mode.  "baked" | True ("absolute") | False ("off").
    fm_volume_scaling: bool | str = "baked"
    fm_pan_law_db: float = 3.0        # "baked" mode: a hard-panned note is this many dB below a centred one
    max_sample_kb: int = 128          # settings.yaml samples.max_sample_kb: 128 = the format's limit, 64 = ProTracker's
    sustain_loops: str = "merged"     # settings.yaml samples.sustain_loops, as PsgSynthesisSettings
    loop_drift_db: float = 1.0        # settings.yaml samples.loop_drift_db, as PsgSynthesisSettings
    treble_shelf_db: float = 0.0      # settings.yaml samples.treble_shelf_db / _hz, as PsgSynthesisSettings
    resample_taps: int = DEFAULT_TAPS  # settings.yaml samples.resample_taps, as PsgSynthesisSettings
    treble_shelf_hz: float = DEFAULT_SHELF_HZ
    dither: str = DEFAULT_DITHER      # settings.yaml samples.dither, as PsgSynthesisSettings
    dc_block: bool = False            # settings.yaml samples.dc_block, as PsgSynthesisSettings
    # settings.yaml `legato` (top level): how an smpsNoAttack note is written when its target cannot
    # ride the sounding sample - "strict" (another range: the sounding sample, note moved by the
    # chip-pitch delta; after smpsSetvoice or with nothing sounding: a re-trigger; what FT2 clone and
    # ProTracker need), "loose" (always 3FF on the target's own instrument, as written before) or
    # "retrigger" (every no-attack note a plain note-on, as before 030ca81; the default).
    legato: str = "retrigger"
    # settings.yaml `player` (top level): the tracker the MOD is made for, "ft2" (default) or "pt2"
    player: str = "ft2"
    # settings.yaml samples.pt_zero_bytes: a one-shot sample's first word zeroed, since ProTracker
    # replays it once the sample ends (core.mod.ModFile.zero_idle_words)
    pt_zero_bytes: bool = True

    @property
    def max_sample_bytes(self) -> int:
        """Bytes one synthesised sample may hold (core.pcm.sample_limit_bytes)."""
        return sample_limit_bytes(self.max_sample_kb)

    def loops_for(self, merged: bool) -> bool:
        """True when this build (the merged one or the reference) gets sustain loops."""
        return self.sustain_loops == "all" or (self.sustain_loops == "merged" and merged)

    @property
    def fm_volume_mode(self) -> str:
        """How FM channel levels (smpsHeaderFM volume, smpsAlterVol, smpsPan) reach the MOD.

        "baked"    — per instrument, the level most of its notes play at needs no command (it is
                     what the sample_list volume stands for); any other level gets
                     Cxx = volume × 10^(ΔdB/20), ΔdB from the chip's 0.75 dB/TL step and the pan
                     law.  Cxx only on the minority channel / after smpsAlterVol.  Default.
        "absolute" — (legacy `true`) header TL + log law as an absolute volume: Cxx on every note.
        "off"      — (legacy `false`) header TL ignored, one smpsAlterVol step = one linear MOD
                     volume unit.  Wrong by up to several dB on faded notes; kept for comparison.
        """
        v = self.fm_volume_scaling
        if isinstance(v, str):
            v = v.strip().lower()
            if v in ("baked", "absolute", "off"):
                return v
            raise ValueError(f"fm_volume_scaling must be baked, true or false (got '{self.fm_volume_scaling}')")
        return "absolute" if v else "off"

    def worker_threads(self) -> int:
        """How many FM instruments render at once (see `threads` in settings.yaml).

        "normal" — one thread per CPU core but one, so a conversion leaves a core for
                   whatever else the machine is doing (never below 1).  Default.
        "max"    — one thread per core.
        n        — exactly n threads; 1 renders the instruments one after another.
        The rendered samples do not depend on this: each thread owns its own chip and the
        results are consumed in a fixed order.
        """
        cores = os.cpu_count() or 1
        v = self.threads
        if isinstance(v, str):
            key = v.strip().lower()
            if key == "normal":
                return max(1, cores - 1)
            if key == "max":
                return cores
            if key.isdigit():
                v = int(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ValueError(
                f"fm_synthesis.threads must be 'normal', 'max' or a positive integer (got {self.threads!r})")
        return v

    @classmethod
    def from_yaml(cls, filepath: str) -> "SynthesisSettings":
        import yaml
        try:
            with open(filepath) as f:
                data = load_yaml(f)
        except yaml.YAMLError as e:
            raise ValueError(f"YAML syntax error in '{filepath}': {e}") from e
        s = data.get("fm_synthesis", {})
        for key in ("headroom_db", "carrier_balance"):
            if key in s:
                warnings.warn(
                    f"{filepath}: fm_synthesis.{key} is ignored — carriers render at the voice's TL plus "
                    "the channel's own volume, and the chip's 9-bit channel accumulator clips them "
                    "exactly as the hardware does; delete the line",
                    stacklevel=2,
                )
        if "normalize_samples" in s:
            warnings.warn(
                f"{filepath}: fm_synthesis.normalize_samples is ignored — every sample is peak-normalised "
                "to its full 8 bits and the sample_list volume carries its level; delete the line",
                stacklevel=2,
            )
        _fm_sd = s.get("sustain_duration", 1.5)
        smp = _samples_section(data, filepath)
        shelf_db, shelf_hz = _treble_shelf(smp, filepath)
        return cls(
            enabled=s.get("enabled", False),
            mode=s.get("mode", "ym2612"),
            clock_rate=s.get("clock_rate", 7_670_454),
            amiga_clock=_amiga_clock(data, s),
            sustain_duration=_fm_sd if _fm_sd == "auto" else float(_fm_sd),
            release_padding=s.get("release_padding", 0.5),
            threads=s.get("threads", "normal"),
            detune_variants=bool(s.get("detune_variants", True)),
            max_sample_kb=_max_sample_kb(smp, filepath),
            fm_volume_scaling=data.get("fm_volume_scaling", "baked"),
            fm_pan_law_db=float(data.get("fm_pan_law_db", 3.0)),
            sustain_loops=_sustain_loops(smp, filepath),
            loop_drift_db=_loop_drift_db(smp, filepath),
            legato=_legato(data, filepath),
            player=_player(data, filepath),
            treble_shelf_db=shelf_db,
            treble_shelf_hz=shelf_hz,
            resample_taps=_positive_int(smp, "resample_taps", DEFAULT_TAPS, filepath, even=True),
            pt_zero_bytes=_sample_flag(smp, "pt_zero_bytes", True, filepath),
            dc_block=_sample_flag(smp, "dc_block", False, filepath),
            dither=_dither(smp.get("dither", DEFAULT_DITHER), f"{filepath}: samples"),
        )


def with_song_overrides(settings, config: "ConversionConfig"):
    """`settings` (Synthesis- or PsgSynthesisSettings) with the song's own overrides applied:
    loop_drift_db, treble_shelf_db."""
    import dataclasses
    if config.loop_drift_db is not None:
        settings = dataclasses.replace(settings, loop_drift_db=config.loop_drift_db)
    if config.treble_shelf_db is not None:
        settings = dataclasses.replace(settings, treble_shelf_db=config.treble_shelf_db)
    return settings


def derive_bpm(tempo_divider, tempo_modifier, ticks_per_row, speed, fps=60):
    """Derive MOD BPM from SMPS tempo parameters.

    The SMPS driver runs on VBlank. A tempo counter decrements each frame;
    when it hits 0, TempoWait fires — resetting the counter to `modifier`
    and adding +1 to all tracks' DurationTimeout (cancelling that frame's
    decrement). Effective tick rate = fps * (modifier - 1) / modifier.

    Note durations from assembly are multiplied by `divider`.

    MOD timing: rows_per_second = (BPM / 2.5) / speed

    Setting SMPS rows/sec equal to MOD rows/sec:
        BPM = fps * (modifier - 1) * speed * 2.5 / (modifier * divider * ticks_per_row)

    Args:
        tempo_divider: SMPS header divider (multiplies note durations)
        tempo_modifier: SMPS header modifier (TempoWait fires every N frames)
        ticks_per_row: SMPS ticks per MOD row
        speed: MOD speed (ticks per row in ProTracker)
        fps: Frame rate — 60 for NTSC, 50 for PAL

    Returns:
        Integer BPM clamped to 32–255
    """
    if tempo_modifier <= 1 or tempo_divider < 1:
        return 150  # fallback
    bpm = fps * (tempo_modifier - 1) * speed * 2.5 / (tempo_modifier * tempo_divider * ticks_per_row)
    return max(32, min(255, round(bpm)))


def exact_bpm(tempo_divider, tempo_modifier, ticks_per_row, speed, fps=60) -> float:
    """derive_bpm() before rounding and clamping (nan when the tempo cannot be derived)."""
    if tempo_modifier <= 1 or tempo_divider < 1:
        return float("nan")
    return fps * (tempo_modifier - 1) * speed * 2.5 / (tempo_modifier * tempo_divider * ticks_per_row)


def bpm_rounding_options(tempo_divider, tempo_modifier, ticks_per_row, fps=60,
                         speeds=(2, 3, 4, 5, 6, 7, 8)) -> list[dict]:
    """How far the whole-number MOD BPM is from the driver's tempo, for each candidate speed.

    A MOD BPM is an integer, so a song whose exact BPM is 98.44 (Special Stage: modifier 8,
    divider 2, 2 ticks per row, speed 3) runs 0.44 % slow at 98 - 139 ms behind the hardware
    over its 33 s pass.  `target_speed` only changes how many MOD ticks a row has, not the row
    grid, so it can be chosen to make the BPM (nearly) whole: speed 6 gives 196.875 -> 197,
    0.06 %.  Returns [{speed, exact, bpm, error_pct}] for the speeds whose BPM fits 32-255,
    best first (ties keep the smaller speed).
    """
    out = []
    for speed in speeds:
        exact = exact_bpm(tempo_divider, tempo_modifier, ticks_per_row, speed, fps)
        if exact != exact or not 32 <= exact <= 255:
            continue
        bpm = round(exact)
        out.append({"speed": speed, "exact": exact, "bpm": bpm, "error_pct": (bpm / exact - 1) * 100})
    out.sort(key=lambda o: (abs(o["error_pct"]), o["speed"]))
    return out


@dataclass
class ConversionConfig:
    name: str = "Untitled"
    input_file: str = ""
    output_file: str = "output.mod"
    target_bpm: int = 150
    target_speed: int = 6
    ticks_per_row: float = 6.0
    # MOD channel count.  None (the default) derives it from the channels section: the highest
    # mod_channel + 1, rounded up to a count a format tag exists for (4, 8, 10, 12, 14, 16).
    # Set it only to pad upward, so a spare channel can carry Fxx / Dxx when no note cell has a
    # free effect slot.
    num_mod_channels: int | None = None
    auto_bpm: bool = False        # Derive BPM from SMPS tempo header
    region: str = "ntsc"          # "ntsc" (60 Hz) or "pal" (50 Hz)
    # What voice_map / psg_voice_map low/high (and root's anchor) are compared with:
    #   "source" - the SMPS note byte (default; what every config before Credits uses)
    #   "chip"   - the real pitch the chip plays: byte + pitch_offset + smpsChangeTransposition
    #              (PSG: + 3 octaves, the table's nC0 being C3).  Needed when a song changes key
    #              with $E9 while keeping a voice: the same byte must then reach different notes.
    range_space: str = "source"
    channels: list = field(default_factory=list)       # list of ChannelConfig
    dac_samples: list = field(default_factory=list)    # list of DacSampleConfig

    @property
    def mod_channel_count(self) -> int:
        """The MOD's channel count: `num_mod_channels` when set, else derived from the channels."""
        if self.num_mod_channels is not None:
            return self.num_mod_channels
        highest = max((c.mod_channel for c in self.channels if c.enabled), default=-1)
        return ModFile.round_up_channels(highest + 1)

    def validate_mod_channels(self) -> None:
        """Reject a `num_mod_channels` no format tag exists for, or one the channels overflow."""
        n = self.num_mod_channels
        if n is None:
            return
        valid = list(ModFile.valid_channel_counts())
        if n not in valid:
            raise ValueError(f"num_mod_channels must be one of {valid} (got {n})")
        over = sorted(c.mod_channel for c in self.channels if c.enabled and c.mod_channel >= n)
        if over:
            raise ValueError(
                f"num_mod_channels: {n} leaves no room for mod_channel {over[0]} "
                f"(needs at least {ModFile.round_up_channels(over[-1] + 1)})"
            )
    sample_list: list | None = None                 # [inst_num, filename, volume, finetune]
    samples_dir: str = "./samples/"
    max_patterns: int = 127
    voice_map: dict = field(default_factory=dict)         # {voice_index: list[InstrumentRange]}
    legacy_voice_map: dict = field(default_factory=dict)  # {voice_index: int} — deprecated simple form
    channel_instrument_map: dict = field(default_factory=dict)  # {source_channel: {voice_index: list[InstrumentRange]}}
    psg_map: dict = field(default_factory=dict)           # {form_byte_int: PsgInstrumentEntry}; type auto-inferred from bit 2
    psg_voice_map: dict = field(default_factory=dict)     # {"fTone_01": list[PsgInstrumentEntry], ...}
    mod_pattern_breaks: list = field(default_factory=list)  # [(pattern_slot, row), ...] — insert Bxx + split pattern
    # Channel folding for the reduced (Amiga) build, used with `convert.py --merged` (core/merge/):
    # each group's followers are dropped and their notes rendered into the primary's instruments.
    merge: list = field(default_factory=list)              # list[MergeGroup]; a `merge_patterns:` group
                                                           # carries its patterns, a `merge:` one is song-wide
    merge_drop: list = field(default_factory=list)         # channels left out of the merged build altogether
    # `merge_patterns:` (folds that differ per pattern, tools/fold_csv.py writes it from a fold table):
    # every pattern number the blocks name, and {channel: patterns} its notes are dropped in
    merge_patterns_named: set = field(default_factory=set)
    merge_pattern_drop: dict = field(default_factory=dict)
    # Channels whose notes go to the fill pool: each note is placed on whichever output channel is
    # silent at that moment (core/merge/ pool_notes), or lost where none is
    merge_fill: list = field(default_factory=list)
    # {output channel: ticks}: after that many ticks of one of its notes the channel counts as silent
    # for the fill pool, so a pool note cuts the note's tail (a chime over a kick's decay, over a bass
    # note's second half); a channel absent here is never cut.  Least 1.
    merge_fill_cut_after: dict = field(default_factory=dict)
    # Ticks a follower's note-on may be from the primary's and still fold (Green Hill's FM3 starts
    # its chord one tick after FM4/FM5); a note that short before a legato note is a grace note,
    # folded to the note it bends into.
    merge_tolerance: int = 1
    # Instrument slots the composite fit leaves free for the sample banks of the `bank: true`
    # groups (core/banks.py); the banks take any other slot still free after the fit as well.
    # "auto" (the default): the converter chooses, rebuilding once the banks' sizes are known
    # (SmpsToModConverter.convert); a number pins it.  `merge_bank_slots` is the count in use.
    merge_bank_slots: int = 2
    merge_bank_slots_auto: bool = True
    # When a composite whose shape a surviving one has gives up its slot (core.merge._twins):
    # "short" only while the composites do not all fit; "always" in any case, for the bytes
    merge_twins: str = "short"
    # Song-level override of settings.yaml loop_drift_db (dB a loop may freeze above the settled
    # level): a lofi build lets loops freeze early for shorter samples
    loop_drift_db: float | None = None
    # Song-level override of settings.yaml treble_shelf_db (a brightness shelf; None: the settings')
    treble_shelf_db: float | None = None
    # Merged build only: cap on the semitones a sample is rendered above the pitch its root sounds
    # (resolve_synth_roots; 12 = the usual octave).  0 halves every shifted sample's bytes and rate.
    merge_max_synth_shift: int = 12
    merge_output_file: str | None = None                   # default: output_file stem + "_merged"
    merge_active: bool = False                             # set by core.merge.prepare_merged_config
    merge_plan: Any = field(default=None, repr=False)      # core.merge.MergePlan, set by the converter
    detune_plan: Any = field(default=None, repr=False)     # core.detune.DetunePlan, set by the converter

    @classmethod
    def default_sonic1(cls, song_name="Untitled"):
        """Create sensible defaults for a Sonic 1 song.

        10 channels: DAC→ch0, FM1-5→ch1-5, PSG1-3→ch6-8
        FM channels transposed -48 semitones, PSG -36 semitones
        """
        config = cls(
            name=song_name,
            target_bpm=150,
            target_speed=6,
            ticks_per_row=6.0,
        )

        # DAC channel
        config.channels.append(ChannelConfig(
            source="DAC", mod_channel=0, instrument=1
        ))

        # FM1-FM5
        # SMPS notes span C0-B7 (8 octaves), MOD spans C1-B3 (3 octaves).
        # Most Sonic 1 FM melodies sit in octaves 3-6, so -36 maps them to MOD range.
        for i in range(5):
            config.channels.append(ChannelConfig(
                source=f"FM{i+1}",
                mod_channel=i + 1,
                transpose=-36,
                instrument=i + 2,
            ))

        # PSG1-PSG3
        # PSG notes tend to be in higher octaves, -36 works here too.
        for i in range(3):
            config.channels.append(ChannelConfig(
                source=f"PSG{i+1}",
                mod_channel=i + 6,
                transpose=-36,
                instrument=i + 7,
            ))

        # Default DAC sample mappings
        config.dac_samples = [
            DacSampleConfig(name="dKick", mod_instrument=1, mod_note="C3"),
            DacSampleConfig(name="dSnare", mod_instrument=7, mod_note="C3"),
            DacSampleConfig(name="dTimpani", mod_instrument=8, mod_note="C3"),
            DacSampleConfig(name="dHiTimpani", mod_instrument=9, mod_note="C3"),
            DacSampleConfig(name="dMidTimpani", mod_instrument=10, mod_note="C3"),
            DacSampleConfig(name="dLowTimpani", mod_instrument=11, mod_note="C3"),
            DacSampleConfig(name="dVLowTimpani", mod_instrument=12, mod_note="C3"),
        ]

        return config

    @classmethod
    def from_yaml(cls, filepath):
        """Load configuration from a YAML file."""
        import yaml

        try:
            with open(filepath) as f:
                data = load_yaml(f)
        except yaml.YAMLError as e:
            raise ValueError(f"YAML syntax error in '{filepath}': {e}") from e

        config = cls(
            name=data.get('name', 'Untitled'),
            input_file=data.get('input_file', ''),
            output_file=data.get('output_file', 'output.mod'),
            target_bpm=data.get('target_bpm', 150),
            target_speed=data.get('target_speed', 6),
            ticks_per_row=data.get('ticks_per_row', 6.0),
            num_mod_channels=data.get('num_mod_channels'),
            auto_bpm=data.get('auto_bpm', False),
            region=data.get('region', 'ntsc'),
            range_space=str(data.get('range_space', 'source')),
            samples_dir=data.get('samples_dir', './samples/'),
            max_patterns=data.get('max_patterns', 127),
        )

        # Parse channel configs
        for i, ch_data in enumerate(data.get('channels', [])):
            _ctx = f"channels[{i}]"
            config.channels.append(ChannelConfig(
                source=_require(ch_data, 'source', _ctx),
                mod_channel=_require(ch_data, 'mod_channel', _ctx),
                transpose=ch_data.get('transpose', 0),
                instrument=ch_data.get('instrument', 1),
                volume=ch_data.get('volume', 64),
                enabled=ch_data.get('enabled', True),
            ))

        # Parse DAC sample configs
        for i, dac_data in enumerate(data.get('dac_samples', [])):
            _ctx = f"dac_samples[{i}]"
            config.dac_samples.append(DacSampleConfig(
                name=_require(dac_data, 'name', _ctx),
                mod_instrument=_require(dac_data, 'mod_instrument', _ctx),
                mod_note=dac_data.get('mod_note', 'C3'),
                saturate_db=max(0.0, float(dac_data.get('saturate_db', 0.0))),
                merge_saturate_db=_opt(dac_data, 'merge_saturate_db', lambda v: max(0.0, float(v))),
            ))

        # ---------------------------------------------------------------------------
        # Parse voice_map
        #
        # New format:   voice_map: {0: [{low: G5, high: G6, mod_instrument: 4, root: Fs2}]}
        # Legacy format: voice_map: {0: 4, 1: 5}  (simple int values — deprecated)
        # Old key name:  voice_instrument_map (deprecated — emit warning, parse as new voice_map)
        # ---------------------------------------------------------------------------

        # Step 1 — accept deprecated key voice_instrument_map (old name for new-format data)
        raw_vim_deprecated = data.get('voice_instrument_map')
        if raw_vim_deprecated is not None:
            warnings.warn(
                f"YAML key 'voice_instrument_map' in '{filepath}' is deprecated; "
                "rename it to 'voice_map'.",
                DeprecationWarning,
                stacklevel=2,
            )
            for voice_key, range_list in raw_vim_deprecated.items():
                config.voice_map[int(str(voice_key), 0)] = [
                    _parse_instrument_range(e, f"voice_instrument_map[{voice_key}][{j}]")
                    for j, e in enumerate(range_list)
                ]

        # Step 2 — read voice_map key: detect format by inspecting first value
        raw_vm = data.get('voice_map', {})
        if raw_vm:
            first_val = next(iter(raw_vm.values()))
            if isinstance(first_val, int):
                # Legacy simple format: {0: 4, 1: 5}
                warnings.warn(
                    f"YAML 'voice_map' with integer values in '{filepath}' is deprecated. "
                    "Use the list-of-ranges format (or remove it if voice_map covers all notes).",
                    DeprecationWarning,
                    stacklevel=2,
                )
                for k, v in raw_vm.items():
                    config.legacy_voice_map[int(str(k), 0)] = v
            else:
                # New list-of-ranges format — don't overwrite entries already set from
                # voice_instrument_map (step 1), in case both keys are present.
                for voice_key, range_list in raw_vm.items():
                    vk = int(str(voice_key), 0)
                    if vk not in config.voice_map:
                        config.voice_map[vk] = [
                            _parse_instrument_range(e, f"voice_map[{voice_key}][{j}]")
                            for j, e in enumerate(range_list)
                        ]

        # Parse channel_instrument_map — per-channel overrides for voice_map
        # {source_channel_name: {voice_idx: [InstrumentRange, ...]}}
        raw_cim = data.get('channel_instrument_map', {})
        for ch_name, vim_data in raw_cim.items():
            config.channel_instrument_map[ch_name] = {}
            for voice_key, range_list in vim_data.items():
                config.channel_instrument_map[ch_name][int(str(voice_key), 0)] = [
                    _parse_instrument_range(e, f"channel_instrument_map[{ch_name}][{voice_key}][{j}]")
                    for j, e in enumerate(range_list)
                ]

        # Parse psg_map: dict keyed by smpsPSGform byte (hex or int YAML keys).  The key is the
        # SN76489 noise register byte, $E0 | white << 2 | rate, so the noise type and rate are
        # read from it (a stated `type` / `noise_rate` that disagrees is a config error and warns).
        # The envelope is derived from the song by the converter unless stated.
        raw_psg_map = data.get('psg_map', {})
        for k, psg_entry in raw_psg_map.items():
            form_byte = int(str(k), 0)
            _ctx = f"psg_map[{k}]"
            inferred_type = "white_noise" if (form_byte & 0x04) else "periodic_noise"
            noise_rate = form_byte & 0x03
            for key, derived in (('type', inferred_type), ('noise_rate', noise_rate)):
                if key in psg_entry and psg_entry[key] != derived:
                    warnings.warn(
                        f"{filepath}: {_ctx}.{key}: {psg_entry[key]!r} contradicts the form byte "
                        f"${form_byte:02X} ({derived!r}); the byte wins — delete the key",
                        stacklevel=2,
                    )
            raw_envs = psg_entry.get('envelopes', {}) or {}
            if not isinstance(raw_envs, dict) or not all(isinstance(v, int) for v in raw_envs.values()):
                raise ValueError(f"{_ctx}.envelopes must map smpsPSGvoice labels to MOD instrument numbers")
            config.psg_map[form_byte] = PsgInstrumentEntry(
                mod_instrument=_require(psg_entry, 'mod_instrument', _ctx),
                type=inferred_type,
                root=_mod_note(_require(psg_entry, 'root', _ctx), f"{_ctx}.root"),
                synth_root=_opt(psg_entry, 'synth_root', parse_synth_note),
                low=_opt(psg_entry, 'low', parse_smps_note),
                high=_opt(psg_entry, 'high', parse_smps_note),
                noise_rate=noise_rate,
                tone2_n=_parse_tone2_n(psg_entry, _ctx),
                envelope=psg_entry.get('envelope', None),
                base_volume=psg_entry.get('base_volume', 0),
                vibrato=_opt(psg_entry, 'vibrato', _parse_vibrato),
                envelopes={str(label): inst for label, inst in raw_envs.items()},
                dither=_dither(psg_entry['dither'], _ctx) if 'dither' in psg_entry else None,
            )

        # psg_form_map is deprecated — psg_map now serves this role
        if 'psg_form_map' in data:
            warnings.warn(
                f"YAML key 'psg_form_map' in '{filepath}' is deprecated; "
                "merge entries into 'psg_map' (dict keyed by form byte).",
                DeprecationWarning,
                stacklevel=2,
            )

        # Parse psg_voice_map: {"fTone_01": {mod_instrument, root, ...}} or list of such dicts.
        # Always stored internally as list[PsgInstrumentEntry] to support range-split entries.
        raw_pvm = data.get('psg_voice_map', {})
        for k, v in raw_pvm.items():
            label = str(k)
            if isinstance(v, list):
                config.psg_voice_map[label] = [
                    _parse_psg_voice_entry(e, label, f"psg_voice_map[{label}][{j}]")
                    for j, e in enumerate(v)
                ]
            else:
                config.psg_voice_map[label] = [_parse_psg_voice_entry(v, label, f"psg_voice_map[{label}]")]

        # Parse sample list
        config.sample_list = data.get('sample_list', None)

        # Parse merge groups: [{primary: FM1, followers: [FM5]}, ...]
        for i, g in enumerate(data.get('merge', []) or []):
            config.merge.append(_parse_merge_group(g, f"merge[{i}]"))
        # Per-pattern folds: [{patterns: "1-4", groups: [...], drop: [...]}, ...]
        for i, blk in enumerate(data.get('merge_patterns', []) or []):
            _ctx = f"merge_patterns[{i}]"
            if not isinstance(blk, dict):
                raise ValueError(f"{_ctx}: a block is a mapping with patterns: and groups:")
            pats = parse_patterns(_require(blk, 'patterns', _ctx), _ctx)
            config.merge_patterns_named |= pats
            pdrop = blk.get('drop', []) or []
            for src in ([pdrop] if isinstance(pdrop, str) else pdrop):
                config.merge_pattern_drop.setdefault(str(src), set()).update(pats)
            for j, g in enumerate(blk.get('groups', []) or []):
                config.merge.append(_parse_merge_group(g, f"{_ctx}.groups[{j}]", pats))
        drop = data.get('merge_drop', []) or []
        config.merge_drop = [str(d) for d in ([drop] if isinstance(drop, str) else drop)]
        fill = data.get('merge_fill', []) or []
        config.merge_fill = [str(d) for d in ([fill] if isinstance(fill, str) else fill)]
        cut_after = data.get('merge_fill_cut_after', {}) or {}
        if not isinstance(cut_after, dict):
            raise ValueError("merge_fill_cut_after must be a mapping of channel to ticks")
        config.merge_fill_cut_after = {str(k): max(1, int(v)) for k, v in cut_after.items()}
        config.merge_output_file = data.get('merge_output_file')
        config.merge_max_synth_shift = int(data.get('merge_max_synth_shift', 12))
        config.merge_tolerance = int(data.get('merge_tolerance', 1))
        bank_slots = data.get('merge_bank_slots', 'auto')
        config.merge_bank_slots_auto = str(bank_slots).lower() == 'auto'
        if not config.merge_bank_slots_auto:
            config.merge_bank_slots = max(0, int(bank_slots))
        config.merge_twins = str(data.get('merge_twins', 'short'))
        if config.merge_twins not in TWIN_MODES:
            raise ValueError(f"merge_twins: {config.merge_twins!r} is not one of {', '.join(TWIN_MODES)}")
        if data.get('loop_drift_db') is not None:
            config.loop_drift_db = max(0.0, float(data['loop_drift_db']))
        if data.get('treble_shelf_db') is not None:
            config.treble_shelf_db = float(data['treble_shelf_db'])

        # Parse mod_pattern_breaks: list of {pattern: N, pos: R} dicts
        breaks_raw = data.get('mod_pattern_breaks', [])
        config.mod_pattern_breaks = [
            (int(_require(b, 'pattern', f"mod_pattern_breaks[{i}]")),
             int(_require(b, 'row',     f"mod_pattern_breaks[{i}]")))
            for i, b in enumerate(breaks_raw)
        ]

        config.validate_mod_channels()
        return config
