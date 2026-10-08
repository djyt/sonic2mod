"""A song config's entries: voice_map ranges, psg_map / psg_voice_map entries, channels, DAC
samples, merge groups — the classes and their parsers."""

import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..mod import MOD_NOTE_MAP, ModNote
from ..smps import parse_smps_note, parse_synth_note, synth_note_name
from .loader import dither_mode

if TYPE_CHECKING:
    from .song import ConversionConfig


@dataclass
class InstrumentRange:
    low: int            # inclusive lower bound — SMPS semitone from C0 (e.g. nA2 = 33)
    high: int           # inclusive upper bound — SMPS semitone from C0
    mod_instrument: int # MOD instrument number (1-31)
    root: ModNote | None = None  # MOD note where `low` plays;
                                    # out_note = root + (source_semitone - low)
                                    # if None: fall back to channel transpose for note
    synth_root: int | None = None  # SMPS semitone the sample is rendered at.  None in a config:
                                      # core.plan.synth_roots.resolve_synth_roots fills in the chip
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
    loop_min_ms: float | None = None     # its sustain loop is at least this long (core.audio.loops' 30 ms
                                      # otherwise): a longer loop keeps a detuned voice's shimmer
                                      # moving where a short one freezes it into a buzz
    name: str | None = None              # the MOD sample's name (22 characters; samples.names: source
                                      # makes one otherwise, core/convert/sample_names.py)
    loop_start_ms: float | None = None   # its sustain loop starts no earlier than this: past an attack a
                                      # beating pair counts as settled through (its swing spans the band)
    loop_decay: str | None = None        # LOOP_DECAY_MODES: "slide" loops while the level still falls
                                      # and the notes' volume slides carry the fall on; None: "freeze"
    dither: str | None = None            # this sample's quantisation (core.audio.pcm.DITHER_MODES); None:


                                      # settings.yaml samples.dither


def _parse_vibrato(v) -> int:
    """Parse a vibrato value (the loader keeps its text as written) → raw byte (high=speed,
    low=depth).  Two hex digits, the 4xy parameter:
      vibrato: 12    → speed=1, depth=2 → 0x12
      vibrato: 1A    → speed=1, depth=10 → 0x1A
      vibrato: 0x12  → 0x12 (a hex literal is the byte itself)
      vibrato: 0     → none
    """
    s = str(v).strip().upper()
    if s.startswith("0X"):
        n = int(s, 16)
        if not 0 <= n <= 0xFF:
            raise ValueError(f"vibrato value '{v}' exceeds 2 hex digits")
        return n
    s = (s.lstrip("0") or "0").zfill(2)
    if len(s) > 2:
        raise ValueError(f"vibrato value '{v}' exceeds 2 hex digits")
    return (int(s[0], 16) << 4) | int(s[1], 16)


# The keys each kind of entry takes: any other is an error, as at the top level (a typo would be ignored)
_SAMPLE_KEYS = frozenset({"loop_drift_db", "loop_min_ms", "loop_start_ms", "loop_decay", "dither", "name"})
_RANGE_KEYS = frozenset({"low", "high", "mod_instrument", "root", "synth_root", "vibrato"}) | _SAMPLE_KEYS
_PSG_KEYS = frozenset({"mod_instrument", "root", "synth_root", "low", "high", "tone2_n", "envelope",
                       "base_volume", "vibrato", "dither", "name", "type", "noise_rate"})
_PSG_MAP_KEYS = _PSG_KEYS | {"envelopes"}
_CHANNEL_KEYS = frozenset({"source", "mod_channel", "transpose", "instrument", "volume", "enabled"})
_DAC_KEYS = frozenset({"name", "mod_instrument", "mod_note", "saturate_db", "merge_saturate_db"})
_GROUP_KEYS = frozenset({"primary", "followers", "cut_primary", "max_composites", "fill_lost", "fill_cut", "bank",
                         "mod_channel", "mix_note", "fill", "cut_after", "mix_at", "loop_mix", "fm_on_chip",
                         "treble_shelf_db", "treble_shelf_hz", "limit_db"}) | _SAMPLE_KEYS
_BLOCK_KEYS = frozenset({"patterns", "groups", "drop"})
_BREAK_KEYS = frozenset({"pattern", "row"})


def _check_keys(d, known: frozenset, context: str) -> None:
    """`d` is a mapping holding only `known` keys."""
    if not isinstance(d, dict):
        raise ValueError(f"Config error: {context} must be a mapping (got {d!r})")
    unknown = sorted(str(k) for k in set(d) - known)
    if unknown:
        raise ValueError(f"Config error: unknown key(s) in {context}: {', '.join(unknown)}")


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
    """Parse a single InstrumentRange dict from YAML."""
    _check_keys(entry, _RANGE_KEYS, context)
    low  = parse_smps_note(_require(entry, 'low',  context))
    high = parse_smps_note(_require(entry, 'high', context))

    if 'mod_instrument' not in entry:
        raise ValueError(f"Config error: 'mod_instrument' is required in {context}")
    inst = entry['mod_instrument']

    root       = _opt(entry, 'root',       lambda v: _mod_note(v, f"{context}.root"))
    synth_root = _opt(entry, 'synth_root', parse_synth_note)
    vibrato    = _opt(entry, 'vibrato',    _parse_vibrato)
    drift      = _opt(entry, 'loop_drift_db', lambda v: _drift_db(v, context))
    min_ms     = _opt(entry, 'loop_min_ms', lambda v: _loop_min_ms(v, context))
    start_ms   = _opt(entry, 'loop_start_ms', lambda v: _loop_start_ms(v, context))
    dither     = _opt(entry, 'dither', lambda v: dither_mode(v, context))
    decay      = _opt(entry, 'loop_decay', lambda v: _loop_decay(v, context))
    name       = _opt(entry, 'name', _sample_name)

    return InstrumentRange(
        low=low, high=high, mod_instrument=inst, root=root, synth_root=synth_root,
        vibrato=vibrato, loop_drift_db=drift, loop_min_ms=min_ms, dither=dither, loop_decay=decay,
        loop_start_ms=start_ms, name=name,
    )


def _sample_name(v) -> str:
    """A `name:` override: the MOD sample's name, at most the 22 characters the format holds."""
    return str(v)[:22]


# loop_decay: what a sustain loop does with a level that is still falling.  "freeze" (the default)
# loops only where the level has settled to within loop_drift_db; "slide" loops where it falls
# in a straight line (in dB) with the timbre holding, plays the loop at that level, and writes
# the rest of the fall into the notes as volume slides (core.audio.loops, ChannelWriter)
LOOP_DECAY_MODES = ("freeze", "slide")


def _loop_decay(v, context: str) -> str:
    """A `loop_decay` value: one of LOOP_DECAY_MODES."""
    mode = str(v).lower()
    if mode not in LOOP_DECAY_MODES:
        raise ValueError(f"{context}: loop_decay must be one of {', '.join(LOOP_DECAY_MODES)} (got {v!r})")
    return mode


def _loop_min_ms(v, context: str) -> float:
    """A `loop_min_ms` override: milliseconds, positive."""
    try:
        ms = float(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{context}: loop_min_ms must be a number of milliseconds (got {v!r})") from e
    if ms <= 0:
        raise ValueError(f"{context}: loop_min_ms must be positive (got {ms})")
    return ms


def _loop_start_ms(v, context: str) -> float:
    """A `loop_start_ms` override: milliseconds, not negative."""
    try:
        ms = float(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{context}: loop_start_ms must be a number of milliseconds (got {v!r})") from e
    if ms < 0:
        raise ValueError(f"{context}: loop_start_ms must not be negative (got {ms})")
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
                                # each sound chosen with 9xx (core/merge/banks.py), which takes the note's
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
    loop_start_ms: float | None = None  # the earliest sustain loop start of this group's chip composites
                                # (and of its looped mixes)
    name: str | None = None         # its composites' MOD sample name (the note each plays at is added)
    loop_decay: str | None = None   # LOOP_DECAY_MODES for this group's chip composites (the primary's
                                # entry's otherwise)
    treble_shelf_db: float | None = None   # a brightness shelf on this group's composites (the mixed
                                # sum, drums off disk included; a chip composite's render), on top of
                                # any song shelf; treble_shelf_hz its corner (None: the settings')
    treble_shelf_hz: float | None = None
    limit_db: float | None = None   # a mix of this group whose sum is past full scale has its peaks
                                # limited, by up to this many dB, instead of the whole sound turned
                                # down (core.audio.pcm.limit_peaks); denser, a little transient distortion
    dither: str | None = None   # this group's composites' quantisation (core.audio.pcm.DITHER_MODES);
                                # None: settings.yaml samples.dither
    loop_mix: bool = False      # a long mix of this group loops where its sum settles, found in the
                                # finished mix as a single voice's loop is (lossy: the chord's slow
                                # movement freezes there); for a pitched primary
    fm_on_chip: bool = True     # a mix of this group renders its FM primary and FM followers together on
                                # the chip, unlooped for the composite's longest note, and mixes only the
                                # rest (a PSG) on top: the voices' detune and phase run on as the
                                # hardware's do instead of two looped samples summed; false: the
                                # samples summed (each looped where its voice settles)
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
    _check_keys(g, _GROUP_KEYS, ctx)
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
                      loop_decay=_opt(g, 'loop_decay', lambda v: _loop_decay(v, ctx)),
                      loop_start_ms=_opt(g, 'loop_start_ms', lambda v: _loop_start_ms(v, ctx)),
                      name=_opt(g, 'name', _sample_name),
                      loop_mix=bool(g.get('loop_mix', False)),
                      fm_on_chip=bool(g.get('fm_on_chip', True)),
                      treble_shelf_db=_opt(g, 'treble_shelf_db', float),
                      treble_shelf_hz=_opt(g, 'treble_shelf_hz', float),
                      limit_db=_opt(g, 'limit_db', lambda v: max(0.0, float(v))),
                      dither=_opt(g, 'dither', lambda v: dither_mode(v, ctx)))


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
                               # peak (core.audio.pcm.saturate): presence, some added harmonics
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
                                             # clock; overrides the divider derived from the song's notes
                                             # (core.plan.derive_rate3_dividers).
    envelope: str | list[int] | None = None  # Named table str ("fTone_04") or inline list[int].  A psg_map
                                             # (noise) entry leaves it None: the converter derives it from
                                             # the song (derive_noise_envelopes); stating it is an override
    base_volume: int = 0                     # SN76489 base attenuation (0=max, 15=silent)
    vibrato: int | None = None           # per-entry 4xy override; same semantics as InstrumentRange.vibrato
    envelopes: dict[str, int] = field(default_factory=dict)   # psg_map only: {smpsPSGvoice label: MOD
                                             # instrument} — a noise-mode envelope that gets its own sample
                                             # (Scrap Brain's fTone_08); other labels play mod_instrument
    dither: str | None = None                # as InstrumentRange.dither
    name: str | None = None                  # as InstrumentRange.name


def _parse_psg_voice_entry(v: dict, default_envelope: str, context: str = "psg_voice_map entry") -> 'PsgInstrumentEntry':
    """Parse a single psg_voice_map entry dict into a PsgInstrumentEntry (always a tone).

    A noise channel never consults psg_voice_map: once smpsPSGform ran, smpsPSGvoice only
    changes the envelope, and an envelope that needs its own sample is named under
    psg_map[<form>].envelopes.  A noise type here is therefore a config error.
    """
    _check_keys(v, _PSG_KEYS, context)
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
        dither=_opt(v, 'dither', lambda d: dither_mode(d, context)),
        name=_opt(v, 'name', _sample_name),
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


TWIN_MODES = ("short", "always")    # merge_twins: when a same-shape twin gives up its slot


# --- the song config's sections, as ConversionConfig.from_yaml reads them -------------------------


def parse_channels(data: dict) -> list[ChannelConfig]:
    """`channels:` — which SMPS channel plays on which MOD channel."""
    out = []
    for i, ch_data in enumerate(data.get('channels', [])):
        ctx = f"channels[{i}]"
        _check_keys(ch_data, _CHANNEL_KEYS, ctx)
        out.append(ChannelConfig(
            source=_require(ch_data, 'source', ctx),
            mod_channel=_require(ch_data, 'mod_channel', ctx),
            transpose=ch_data.get('transpose', 0),
            instrument=ch_data.get('instrument', 1),
            volume=ch_data.get('volume', 64),
            enabled=ch_data.get('enabled', True),
        ))
    return out


def parse_dac_samples(data: dict) -> list[DacSampleConfig]:
    """`dac_samples:` — each drum's MOD instrument and note."""
    out = []
    for i, dac_data in enumerate(data.get('dac_samples', [])):
        ctx = f"dac_samples[{i}]"
        _check_keys(dac_data, _DAC_KEYS, ctx)
        mod_note = str(dac_data.get('mod_note', 'C3'))
        if mod_note not in MOD_NOTE_MAP:
            raise ValueError(f"{ctx}: mod_note {mod_note!r} is not a MOD note (C1 .. B3, like F2, Fs2 or F#2)")
        out.append(DacSampleConfig(
            name=_require(dac_data, 'name', ctx),
            mod_instrument=_require(dac_data, 'mod_instrument', ctx),
            mod_note=mod_note,
            saturate_db=max(0.0, float(dac_data.get('saturate_db', 0.0))),
            merge_saturate_db=_opt(dac_data, 'merge_saturate_db', lambda v: max(0.0, float(v))),
        ))
    return out


def parse_voice_maps(data: dict) -> dict:
    """`voice_map:` as {voice: [InstrumentRange]}: {0: [{low: G5, high: G6, mod_instrument: 4, root: Fs2}]}.
    A voice with nothing under it (analyze.py's skeleton, for a voice no note plays) has no ranges."""
    voice_map: dict = {}
    for voice_key, range_list in (data.get('voice_map') or {}).items():
        if range_list is None:
            continue
        if not isinstance(range_list, list):
            raise ValueError(f"voice_map[{voice_key}] must be a list of ranges (low, high, mod_instrument, ...)")
        voice_map[int(str(voice_key), 0)] = [_parse_instrument_range(e, f"voice_map[{voice_key}][{j}]")
                                            for j, e in enumerate(range_list)]
    return voice_map


def parse_channel_instrument_map(data: dict) -> dict:
    """`channel_instrument_map:` — per-channel overrides for voice_map,
    {source_channel_name: {voice_idx: [InstrumentRange, ...]}}."""
    out: dict = {}
    for ch_name, vim_data in data.get('channel_instrument_map', {}).items():
        out[ch_name] = {}
        for voice_key, range_list in vim_data.items():
            out[ch_name][int(str(voice_key), 0)] = [
                _parse_instrument_range(e, f"channel_instrument_map[{ch_name}][{voice_key}][{j}]")
                for j, e in enumerate(range_list)
            ]
    return out


def parse_psg_map(data: dict, filepath) -> dict:
    """`psg_map:` keyed by smpsPSGform byte (hex or int YAML keys).  The key is the SN76489 noise
    register byte, $E0 | white << 2 | rate, so the noise type and rate are read from it (a stated
    `type` / `noise_rate` that disagrees is a config error and warns).  The envelope is derived
    from the song by the converter unless stated."""
    out: dict = {}
    for k, psg_entry in data.get('psg_map', {}).items():
        form_byte = int(str(k), 0)
        ctx = f"psg_map[{k}]"
        _check_keys(psg_entry, _PSG_MAP_KEYS, ctx)
        inferred_type = "white_noise" if (form_byte & 0x04) else "periodic_noise"
        noise_rate = form_byte & 0x03
        for key, derived in (('type', inferred_type), ('noise_rate', noise_rate)):
            if key in psg_entry and psg_entry[key] != derived:
                warnings.warn(
                    f"{filepath}: {ctx}.{key}: {psg_entry[key]!r} contradicts the form byte "
                    f"${form_byte:02X} ({derived!r}); the byte wins — delete the key",
                    stacklevel=3,
                )
        raw_envs = psg_entry.get('envelopes', {}) or {}
        if not isinstance(raw_envs, dict) or not all(isinstance(v, int) for v in raw_envs.values()):
            raise ValueError(f"{ctx}.envelopes must map smpsPSGvoice labels to MOD instrument numbers")
        out[form_byte] = PsgInstrumentEntry(
            mod_instrument=_require(psg_entry, 'mod_instrument', ctx),
            type=inferred_type,
            root=_mod_note(_require(psg_entry, 'root', ctx), f"{ctx}.root"),
            synth_root=_opt(psg_entry, 'synth_root', parse_synth_note),
            low=_opt(psg_entry, 'low', parse_smps_note),
            high=_opt(psg_entry, 'high', parse_smps_note),
            noise_rate=noise_rate,
            tone2_n=_parse_tone2_n(psg_entry, ctx),
            envelope=psg_entry.get('envelope', None),
            base_volume=psg_entry.get('base_volume', 0),
            vibrato=_opt(psg_entry, 'vibrato', _parse_vibrato),
            envelopes={str(label): inst for label, inst in raw_envs.items()},
            dither=dither_mode(psg_entry['dither'], ctx) if 'dither' in psg_entry else None,
            name=_opt(psg_entry, 'name', _sample_name),
        )
    return out


def parse_psg_voice_map(data: dict) -> dict:
    """`psg_voice_map:` {"fTone_01": {mod_instrument, root, ...}} or a list of such dicts, always
    stored as list[PsgInstrumentEntry] to support range-split entries.  A label with nothing under
    it (analyze.py's skeleton, for a label no note plays) has no entries."""
    out: dict = {}
    for k, v in (data.get('psg_voice_map') or {}).items():
        label = str(k)
        if v is None:
            continue
        if isinstance(v, list):
            out[label] = [_parse_psg_voice_entry(e, label, f"psg_voice_map[{label}][{j}]") for j, e in enumerate(v)]
        else:
            out[label] = [_parse_psg_voice_entry(v, label, f"psg_voice_map[{label}]")]
    return out


def parse_merge_groups(data: dict) -> tuple[list[MergeGroup], set, dict]:
    """(groups, every pattern merge_patterns names, {channel: patterns its notes are dropped in}):
    `merge:` [{primary: FM1, followers: [FM5]}, ...] and `merge_patterns:` [{patterns: "1-4",
    groups: [...], drop: [...]}, ...]."""
    groups = [_parse_merge_group(g, f"merge[{i}]") for i, g in enumerate(data.get('merge', []) or [])]
    named: set = set()
    pattern_drop: dict = {}
    for i, blk in enumerate(data.get('merge_patterns', []) or []):
        ctx = f"merge_patterns[{i}]"
        if not isinstance(blk, dict):
            raise ValueError(f"{ctx}: a block is a mapping with patterns: and groups:")
        _check_keys(blk, _BLOCK_KEYS, ctx)
        pats = parse_patterns(_require(blk, 'patterns', ctx), ctx)
        named |= pats
        pdrop = blk.get('drop', []) or []
        for src in ([pdrop] if isinstance(pdrop, str) else pdrop):
            pattern_drop.setdefault(str(src), set()).update(pats)
        groups.extend(_parse_merge_group(g, f"{ctx}.groups[{j}]", pats)
                      for j, g in enumerate(blk.get('groups', []) or []))
    return groups, named, pattern_drop


def parse_pattern_breaks(data: dict) -> list[tuple[int, int]]:
    """`mod_pattern_breaks:` [{pattern: N, row: R}, ...] as (pattern, row)."""
    for i, b in enumerate(data.get('mod_pattern_breaks', [])):
        _check_keys(b, _BREAK_KEYS, f"mod_pattern_breaks[{i}]")
    return [(int(_require(b, 'pattern', f"mod_pattern_breaks[{i}]")),
             int(_require(b, 'row', f"mod_pattern_breaks[{i}]")))
            for i, b in enumerate(data.get('mod_pattern_breaks', []))]
