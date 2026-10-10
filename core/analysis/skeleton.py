"""The YAML skeleton analyze.py suggests for a song: its instruments numbered DAC -> FM voices -> PSG,
each with the ranges, roots and levels the analysis found (core.analysis.song), as text to fill in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..chips import PSG_STEP_DB, TL_STEP_DB
from ..config import PsgSynthesisSettings, SynthesisSettings
from ..mod import PERIOD_TABLE, ModFile, ModNote, period_rate
from ..plan import starting_volume
from ..smps import ChannelType, psg_tone2_divider, synth_note_name, update_hz
from .song import (
    DAC_NATIVE_INFO,
    DAC_SAMPLE_GROUPS,
    ChannelAnalysis,
    PsgToneStats,
    SongAnalysis,
    semitone_to_note_name,
)

ALGORITHM_TOPOLOGY = {
    0: "1→2→3→4",
    1: "(1,2)→3→4",
    2: "(1+(2→3))→4",
    3: "((1→2)+3)→4",
    4: "(1→2)+(3→4)",
    5: "1→(2+3+4)",
    6: "(1,2)→(3+4)",
    7: "1+2+3+4",
}


# YAML-compatible note name table (uses 's' suffix for sharps, e.g. Fs not F#)
_sem_to_yaml = synth_note_name   # YAML config note name, e.g. 'C3', 'Fs2', 'As4'


def _note_in_octave2(semitone: int) -> str:
    """Place the note letter of semitone in octave 2, as a valid ModNote name.

    e.g. C6 → 'C2', C#5 → 'Cs2', F#5 → 'Fs2'
    """
    return ModNote(12 + semitone % 12).name


def _noise_root_for_synth(note_letter: int, synth_freq: float, amiga_clock: int) -> str:
    """Return the lowest MOD octave (≥ 2) where target_rate > 2*synth_freq, as a valid ModNote name.

    Ensures the synthesized LFSR frequency stays below Nyquist so there is no
    aliasing.  For low-frequency noise (e.g. Marble Zone C7 ≈ 2093 Hz) octave 2
    satisfies the constraint; for high-frequency noise (e.g. GHZ C9 ≈ 8383 Hz)
    the function steps up to octave 3.
    """
    for octave in range(2, 4):
        root_idx = (octave - 1) * 12 + note_letter
        period = PERIOD_TABLE[root_idx] if root_idx < len(PERIOD_TABLE) else 0
        if period == 0:
            break
        if period_rate(period, amiga_clock) > 2.0 * synth_freq:
            return ModNote(root_idx).name
    return ModNote(24 + note_letter).name


# sample_list volume of a single-carrier FM voice at TL offset 0, centred.  Fitted to the volumes
# measured against the VGZs in docs/audits/ (13 instruments, Title Screen + GHZ): each implies a
# value between 68 and 92, median 76 — so expect the skeleton's numbers to be within ~2 dB.


def _dac_file(name: str) -> str:
    """A DAC sample's file: Sonic 1's input/sonic_1/samples/ names (dKick -> kick.raw), else its own (dac81.raw)."""
    if len(name) > 1 and name[0] == "d" and name[1].isupper():
        return f"{name[1:].lower()}.raw"
    return f"{name}.raw"


def _channel_has_notes(ch_an: ChannelAnalysis) -> bool:
    if ch_an.channel_type == ChannelType.DAC:
        return bool(ch_an.dac_counts)
    if ch_an.channel_type == ChannelType.FM:
        return any(vs.note_count > 0 for vs in ch_an.voice_stats.values())
    # PSG
    return any(ts.note_count > 0 for ts in ch_an.psg_tone_stats.values())


def _suggest_target_speed(tempo_divider: int, tempo_modifier: int, ticks_per_row: int, fps: float) -> int:
    """The speed (2–8) whose whole-number BPM is closest to the driver's tempo, BPM within 32–255.

    A MOD BPM is an integer; speed changes how many MOD ticks a row has, not the row grid, so it
    is free to choose (core.config.bpm_rounding_options).  Ties go to the smaller speed.  Speed 1
    is the fallback when nothing fits.
    """
    from core.config import bpm_rounding_options
    options = bpm_rounding_options(tempo_divider, tempo_modifier, ticks_per_row, fps)
    return options[0]["speed"] if options else 1


_TOP_NOTE = ModNote.B3.value   # the highest MOD note
_TRANSPOSE_COMMENT = "  # smpsChangeTransposition active — verify root is correct"
_FORM_PREFIX = "form $"        # psg_tone_stats key of an smpsPSGform, e.g. "form $E7"


def _split_point(low: int, high: int) -> int | None:
    """Last semitone of the lower sample where low..high runs past B3 from its octave-2 root; None if one fits."""
    top = low + (_TOP_NOTE - (low % 12 + 12))
    return top if high > top else None


def _upper_root(low: int, high: int) -> str:
    """Root of a split range's upper entry: octave 2, or octave 1 where the range would run past B3."""
    note_class = low % 12
    in_octave2 = 12 + note_class
    return ModNote(note_class if in_octave2 + (high - low) > _TOP_NOTE else in_octave2).name


def _range_entry(low: int, high: int, inst: int, root: str) -> list[str]:
    """One `- low: … root:` entry of a voice_map / psg_voice_map range list."""
    return [
        f"    - low:  {_sem_to_yaml(low)}",
        f"      high: {_sem_to_yaml(high)}",
        f"      mod_instrument: {inst}",
        f"      root: {root}",
    ]


@dataclass
class _FmVoice:
    """An FM voice's instrument(s): `inst`, and `inst + 1` above `split` when its range needs two."""
    index: int
    inst: int
    alg: str                 # "alg 4: (1→2)+(3→4), fb 3"
    used_by: str             # "FM1, FM5"
    min_sem: int | None      # None: switched to, never played
    max_sem: int | None
    has_trans: bool          # a channel using it runs smpsChangeTransposition
    split: int | None
    volume: int = 32
    volume_note: str = ""    # "TL FM1 $0A → 41, …"


@dataclass
class _PsgNoise:
    form_byte: int
    label: str               # "form $E7"
    inst: int
    stats: PsgToneStats
    channel: ChannelAnalysis


@dataclass
class _PsgTone:
    label: str
    inst: int
    stats: PsgToneStats
    split: int | None        # as _FmVoice.split


class _Skeleton:
    """The suggested config for a song.  Instruments are numbered DAC → FM voices → PSG."""

    def __init__(self, analysis: SongAnalysis, region: str, synth: SynthesisSettings,
                 psg_synth: PsgSynthesisSettings):
        self._analysis = analysis
        self._region = region
        self._active = [ch for ch in analysis.channels if _channel_has_notes(ch)]
        self._fm = [ch for ch in self._active if ch.channel_type == ChannelType.FM]
        self._psg = [ch for ch in self._active if ch.channel_type == ChannelType.PSG]
        self._synth, self._psg_synth = synth, psg_synth
        self._pan_law_db = self._synth.fm_pan_law_db   # a hard-panned note vs a centred one
        self._next_inst = 1

        self._dac_items: list[tuple[str, int, int]] = []   # (name, inst, count)
        self._dac_bases: list[tuple[str, int]] = []        # (base_name, inst) — one per instrument slot
        self._fm_voices: list[_FmVoice] = []
        self._noise: list[_PsgNoise] = []
        self._tones: list[_PsgTone] = []
        self._assign_dac()
        self._assign_fm()
        self._assign_psg()

    def text(self) -> str:
        lines = self._header()
        lines += self._sample_list()
        lines += self._dac_samples()
        lines += self._voice_map()
        lines += self._psg_map()
        lines += self._psg_voice_map()
        return "\n".join(lines)

    def _take(self, count: int) -> int:
        """The next free instrument slot, `count` slots reserved from it."""
        inst = self._next_inst
        self._next_inst += count
        return inst

    # --- instrument assignment ---

    def _assign_dac(self):
        """One slot per DAC sample, most played first; timpani variants share their base's slot."""
        seen: set[str] = set()
        inst_by_name: dict[str, int] = {}
        for ch_an in self._active:
            if ch_an.channel_type != ChannelType.DAC:
                continue
            for name, count in sorted(ch_an.dac_counts.items(), key=lambda x: -x[1]):
                if name in seen:
                    continue
                seen.add(name)

                base = DAC_SAMPLE_GROUPS.get(name, name)
                if base not in inst_by_name:
                    inst_by_name[base] = self._take(1)
                    self._dac_bases.append((base, inst_by_name[base]))
                inst_by_name[name] = inst_by_name[base]
                self._dac_items.append((name, inst_by_name[name], count))

    def _assign_fm(self):
        used_by: dict[int, list[str]] = {}
        for ch_an in self._fm:
            for vi in ch_an.voice_stats:
                used_by.setdefault(vi, []).append(ch_an.name)

        for vi in sorted(used_by):
            playing = [ch_an for ch_an in self._fm
                       if vi in ch_an.voice_stats and ch_an.voice_stats[vi].note_count > 0]
            stats = [ch_an.voice_stats[vi] for ch_an in playing]
            min_sem = min(vs.min_semitone for vs in stats) if stats else None
            max_sem = max(vs.max_semitone for vs in stats) if stats else None
            split = _split_point(min_sem, max_sem) if min_sem is not None and max_sem is not None else None
            voice = _FmVoice(
                index=vi,
                inst=self._take(2 if split is not None else 1),
                alg=self._algorithm(vi),
                used_by=", ".join(used_by[vi]),
                min_sem=min_sem,
                max_sem=max_sem,
                has_trans=any(ch_an.has_transpose_change for ch_an in self._fm if vi in ch_an.voice_stats),
                split=split,
            )
            if playing:
                self._set_fm_volume(voice, playing)
            self._fm_voices.append(voice)

    def _algorithm(self, vi: int) -> str:
        voice_def = next((v for v in self._analysis.song.voices if v.index == vi), None)
        if not voice_def:
            return "unknown"
        topo = ALGORITHM_TOPOLOGY.get(voice_def.algorithm, "?")
        return f"alg {voice_def.algorithm}: {topo}, fb {voice_def.feedback}"

    def _set_fm_volume(self, voice: _FmVoice, playing: list[ChannelAnalysis]):
        """The voice's level on each channel it plays on — TL offset and pan.  The dominant channel's
        level becomes the sample_list volume (what fm_volume_scaling: baked expects; it puts Cxx on
        the rest)."""
        vi = voice.index
        levels = {ch_an.name: (ch_an.voice_stats[vi].modal_volume, ch_an.voice_stats[vi].modal_hard_pan)
                  for ch_an in playing}
        dominant = max(playing, key=lambda c: c.voice_stats[vi].note_count)
        voice.volume = self._fm_volume(levels[dominant.name])

        per_ch = [f"{name} ${lv[0] & 0xFF:02X}{' panned' if lv[1] else ''} → {self._fm_volume(lv)}"
                  for name, lv in levels.items()]
        voice.volume_note = "TL " + ", ".join(per_ch)
        if len({self._fm_volume(lv) for lv in levels.values()}) > 1:
            voice.volume_note += (f"  (volume is {dominant.name}'s; fm_volume_scaling: baked "
                                  "puts Cxx on the others)")

    def _fm_volume(self, lv: tuple[int, bool]) -> int:
        return starting_volume("FM", lv[0], lv[1], self._pan_law_db)

    def _assign_psg(self):
        """One slot per smpsPSGform byte and per tone label (two for a split range).  A label every
        channel plays only as a noise envelope gets none."""
        seen: set[str] = set()
        for ch_an in self._psg:
            for label, ts in ch_an.psg_tone_stats.items():
                if label in seen:
                    continue
                seen.add(label)

                if label.startswith(_FORM_PREFIX):
                    form_byte = int(label[len(_FORM_PREFIX):], 16)
                    self._noise.append(_PsgNoise(form_byte, label, self._take(1), ts, ch_an))
                    continue
                if self._is_noise_envelope(label):
                    continue

                split = _split_point(ts.min_semitone, ts.max_semitone) if ts.note_count else None
                self._tones.append(_PsgTone(label, self._take(2 if split is not None else 1), ts, split))

    def _is_noise_envelope(self, label: str) -> bool:
        """Every channel playing the label is a noise channel (one with an smpsPSGform)."""
        users = [ch for ch in self._psg if label in ch.psg_tone_stats]
        return bool(users) and all(_has_noise(ch) for ch in users)

    def _psg_attenuation(self, label: str) -> int:
        """The label's attenuation on the channel that plays it most (psg_volume_scaling: baked)."""
        users = [ch.psg_tone_stats[label] for ch in self._psg
                 if label in ch.psg_tone_stats and ch.psg_tone_stats[label].note_count > 0]
        return max(users, key=lambda t: t.note_count).modal_volume if users else 0

    # --- sections ---

    def _header(self) -> list[str]:
        song = self._analysis.song
        fname = os.path.basename(self._analysis.file_path)
        stem = os.path.splitext(fname)[0]
        speed = _suggest_target_speed(song.header.tempo_divider, song.header.tempo_modifier, ticks_per_row=1,
                                      fps=update_hz(song.rules, self._region))
        lines = [
            "# Generated by analyze.py — fill in root values (synth_root is derived from the song)",
            f"# Song: {fname}",
            "",
            f"name: {stem}",
            f"input_file: {self._analysis.file_path}",
            f"output_file: output/{stem.replace(' ', '_')}.mod",
            "samples_dir: \"input/sonic_1/samples/\"",
            "",
            "auto_bpm: true",
            f"target_speed: {speed}",
            "ticks_per_row: 1",
            f"# {ModFile.round_up_channels(len(self._active))} MOD channels, derived from the channels below;"
            " set num_mod_channels: only to pad for a spare Fxx/Dxx channel",
            f"region: {self._region}",
            "",
            "channels:",
        ]
        for mod_ch, ch_an in enumerate(self._active):
            lines.append(f"  - source: {'DAC' if ch_an.channel_type == 'DAC' else ch_an.name}")
            lines.append(f"    mod_channel: {mod_ch}")
        return lines

    def _sample_list(self) -> list[str]:
        lines = ["", "sample_list:"]
        if self._dac_items:
            lines.append("  # --- percussion ---")
            for base_name, inst in self._dac_bases:
                lines.append(f"  - [{inst}, \"{_dac_file(base_name)}\", {starting_volume('DAC')}, 0]")

        if self._fm_voices:
            lines.append(f"  # FM volumes bake in each channel's level: TL offset (smpsHeaderFM volume + smpsAlterVol, "
                         f"{TL_STEP_DB} dB/step)")
            lines.append(f"  # and −{self._pan_law_db:g} dB when hard-panned (core.plan.starting_volume; samples are peak-normalised).  "
                         "Starting points (~2 dB) —")
            lines.append("  # measure with tools/vgm_compare.py; every channel sharing an instrument should read the same error.")
        for v in self._fm_voices:
            lines.append(f"  # --- voice ${v.index:02X}: {v.alg} — {v.used_by} ---")
            if v.volume_note:
                lines.append(f"  #     {v.volume_note}")
            if v.split is None:
                lines.append(f"  - [{v.inst}, \"fm_v{v.index:02x}.raw\", {v.volume}, 0]")
                continue
            lines.append(f"  - [{v.inst}, \"fm_v{v.index:02x}_lo.raw\", {v.volume}, 0]")
            lines.append(f"  - [{v.inst + 1}, \"fm_v{v.index:02x}_hi.raw\", {v.volume}, 0]")

        if self._noise or self._tones:
            lines.append(f"  # PSG volumes bake in the track attenuation ({PSG_STEP_DB:g} dB/step): "
                         f"tone / noise {starting_volume('tone')} at attenuation 0.")
        for n in self._noise:
            lines.append(f"  # --- PSG noise ({n.label}) ---")
            lines.append(self._psg_sample(n.inst, "psg_noise.raw", "noise", n.label))
        for t in self._tones:
            lines.append(f"  # --- PSG tone {t.label} ---")
            if t.split is None:
                lines.append(self._psg_sample(t.inst, f"psg_{t.label}.raw", "tone", t.label))
                continue
            lines.append(self._psg_sample(t.inst, f"psg_{t.label}_lo.raw", "tone", t.label))
            lines.append(self._psg_sample(t.inst + 1, f"psg_{t.label}_hi.raw", "tone", t.label))
        return lines

    def _psg_sample(self, inst: int, fname: str, kind: str, label: str) -> str:
        att = self._psg_attenuation(label)
        return (f"  - [{inst}, \"{fname}\", {starting_volume(kind, att)}, 0]"
                + (f"   # attenuation {att} (−{att * PSG_STEP_DB:g} dB)" if att else ""))

    def _dac_samples(self) -> list[str]:
        if not self._dac_items:
            return []

        lines = ["", "dac_samples:"]
        for name, inst, count in self._dac_items:
            native_note, native_rate = DAC_NATIVE_INFO.get(name, ("C3", 8000))
            lines.append(f"  # {name}: ×{count} — native ~{native_rate} Hz → note {native_note}")
            lines.append(f"  - name: {name}")
            lines.append(f"    mod_instrument: {inst}")
            lines.append(f"    mod_note: {native_note}")
        return lines

    def _voice_map(self) -> list[str]:
        if not self._fm_voices:
            return []

        lines = ["", "voice_map:"]
        for v in self._fm_voices:
            lines.append(f"  # ${v.index:02X} — {v.alg} — used by {v.used_by}")
            lines.append(f"  {v.index}:")
            lines += self._fm_ranges(v)
        return lines

    def _fm_ranges(self, v: _FmVoice) -> list[str]:
        """No synth_root: the converter renders at the pitch the chip plays for `low`."""
        if v.min_sem is None or v.max_sem is None:
            return ["    # (no notes played — voice switched to but never triggered)"]

        comment = _TRANSPOSE_COMMENT if v.has_trans else ""
        if v.split is None:
            return _range_entry(v.min_sem, v.max_sem, v.inst, _note_in_octave2(v.min_sem) + comment)
        return (_range_entry(v.min_sem, v.split, v.inst, _note_in_octave2(v.min_sem) + comment)
                + _range_entry(v.split + 1, v.max_sem, v.inst + 1, _upper_root(v.split + 1, v.max_sem) + comment))

    def _psg_map(self) -> list[str]:
        if not self._noise:
            return []

        lines = ["", "psg_map:"]
        for n in self._noise:
            lines += self._noise_entry(n)
        return lines

    def _noise_entry(self, n: _PsgNoise) -> list[str]:
        ts = n.stats
        noise_rate = n.form_byte & 0x03
        min_sem = ts.min_semitone if ts.note_count > 0 else 45
        transpose = n.channel.initial_transpose

        tone2_n: int | None = None
        shift_hz = 0.0
        root_name = _note_in_octave2(min_sem)
        if noise_rate == 3 and ts.note_count > 0:
            # The LFSR is clocked by tone channel 2, whose divider the driver looks up in
            # PSGFrequencies from the channel's own note — not a chromatic extrapolation:
            # nMaxPSG (index 69) is divider 0 → N=1, not the ~7 kHz an "A8" would give.
            tone2_n = psg_tone2_divider(0x81 + min_sem, transpose, self._analysis.song.rules.psg_read)
            shift_hz = self._psg_synth.clock_rate / (32.0 * tone2_n)
            # root must satisfy Nyquist for the LFSR shift rate where that is achievable;
            # above it the highest-rate root is the best a MOD sample can do.
            root_name = _noise_root_for_synth(min_sem % 12, shift_hz, self._synth.amiga_clock)

        noise_kind = "white" if n.form_byte & 0x04 else "periodic"
        lines = [
            f"  0x{n.form_byte:02X}:                    # {n.label}: {noise_kind} noise, rate {noise_rate} "
            "(both read from the byte)",
            f"    mod_instrument: {n.inst}",
            f"    root: {root_name}",
        ]
        if tone2_n is not None:
            # Not emitted as a key: the converter derives the divider from the song itself
            # (derive_rate3_dividers); an explicit tone2_n here would only shadow that.
            note_desc = semitone_to_note_name(min_sem)
            lines.append(f"    # LFSR divider is derived from the song: {tone2_n} for n{note_desc} "
                         f"{transpose:+d} → {shift_hz:,.0f} Hz")
            if ts.max_semitone != ts.min_semitone:
                # Pitched noise: anchor the sample at the lowest note so MOD playback speed
                # follows the melody (one static LFSR rate per sample is the approximation).
                lines.append(f"    low: {_sem_to_yaml(min_sem)}                # n{note_desc} plays at root; "
                             f"notes up to n{semitone_to_note_name(ts.max_semitone)} shift the playback rate")

        # The envelope is not a key: the converter derives it from the song (the header
        # voice or the last smpsPSGvoice); `envelopes: {label: inst}` gives a label its own sample.
        first = _first_envelope(n.channel)
        lines.append(f"    # envelope derived from the song: {first or 'header voice'}")
        # A label the noise also plays under keeps the derived envelope's sample (the converter
        # warns noise_envelopes); Scrap Brain's fTone_08 hats got their own this way
        for label, label_stats in n.channel.psg_tone_stats.items():
            if label.startswith(_FORM_PREFIX) or label == first or not label_stats.note_count:
                continue
            notes = f"{label_stats.note_count} note" + ("s" if label_stats.note_count != 1 else "")
            lines.append(f"    # also plays under {label} ({notes}): give it its own "
                         f"sample with envelopes: {{{label}: <instrument>}}")
        return lines

    def _psg_voice_map(self) -> list[str]:
        if not self._tones:
            return []

        lines = ["", "psg_voice_map:"]
        for t in self._tones:
            lines.append(f"  {t.label}:")
            lines += self._tone_ranges(t)
        return lines

    def _tone_ranges(self, t: _PsgTone) -> list[str]:
        ts = t.stats
        if ts.note_count == 0:
            return ["    # (no notes played — label switched to but never triggered)"]

        if t.split is not None:
            return (_range_entry(ts.min_semitone, t.split, t.inst, _note_in_octave2(ts.min_semitone))
                    + _range_entry(t.split + 1, ts.max_semitone, t.inst + 1,
                                   _upper_root(t.split + 1, ts.max_semitone)))
        return [
            f"    low:  {_sem_to_yaml(ts.min_semitone)}",
            f"    high: {_sem_to_yaml(ts.max_semitone)}",
            f"    mod_instrument: {t.inst}",
            f"    root: {_note_in_octave2(ts.min_semitone)}",
            f"    synth_root: {_sem_to_yaml(ts.min_semitone)}",
        ]


def _has_noise(ch_an: ChannelAnalysis) -> bool:
    """The channel runs an smpsPSGform: a noise channel."""
    return any(label.startswith(_FORM_PREFIX) for label in ch_an.psg_tone_stats)


def _first_envelope(ch_an: ChannelAnalysis) -> str:
    """A noise channel's first voice label — the envelope its notes start under; "" if none."""
    if not _has_noise(ch_an):
        return ""
    return next((label for label in ch_an.psg_tone_stats if not label.startswith(_FORM_PREFIX)), "")


def yaml_skeleton(analysis: SongAnalysis, region: str, synth: SynthesisSettings,
                  psg_synth: PsgSynthesisSettings) -> str:
    """The suggested config, its levels and rates modelled on the settings the converter reads."""
    return _Skeleton(analysis, region, synth, psg_synth).text()
