"""SN76489 PSG sample generator.

Reads the song config's psg_map and renders each PsgInstrumentEntry to
8-bit signed mono PCM ready for insertion into a ModSample.

Public API::

    from sn76489.sample_generator import generate_psg_samples

    samples = generate_psg_samples(config, psg_synth)
    # {inst_num: (pcm_bytes, sample_rate_hz), ...}

Usage (smoke test)::

    python sn76489/sample_generator.py
"""

from __future__ import annotations

import functools
import math
import sys
import warnings
from collections.abc import Mapping, Sequence
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from core.audio import (
    SustainLoop,
    apply_loop,
    condition_render,
    find_sustain_loop,
    full_scale_int8,
    int8_to_raw16,
    peak,
    probe_secs,
)
from core.audio import trim_trailing_silence as _trim_trailing_silence
from core.config import ConversionConfig, PsgInstrumentEntry, PsgSynthesisSettings
from core.mod import ModNote, max_sustain_secs
from core.plan import PsgInstrument, psg_catalogue
from core.render_cache import RenderCache, code_salt
from core.smps import SONIC1_ENVELOPES, PsgEnvelope, noise_envelope_frames
from sn76489.build import get_lib_path
from sn76489.renderer import (
    note_to_psg_n,
    render_psg_noise_raw,
    render_psg_tone_raw,
)


@functools.cache
def _render_salt() -> str:
    """What a chip render depends on besides its inputs: the emulator and the Python it runs through
    (this package, the resampler and PCM helpers, the driver's tables)."""
    core = _HERE.parent / "core"
    return code_salt([Path(get_lib_path()), *_HERE.glob("*.py"), core / "audio" / "resample.py",
                      core / "audio" / "pcm.py", core / "smps" / "driver_tables.py"])


def _cached_render(cache: RenderCache, render, **kwargs) -> tuple[list, int]:
    """render(**kwargs) through the render cache (core/render_cache.py); a list, as the renderers return."""
    inputs = (render.__name__, tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in kwargs.items())))
    mono, rate = cache.through(inputs, lambda: render(**kwargs))
    return list(mono), rate


# ---------------------------------------------------------------------------
# Rendering one catalogue entry
# ---------------------------------------------------------------------------

_TONE = "tone"
_WHITE_NOISE, _PERIODIC_NOISE = "white_noise", "periodic_noise"


def _resolve_envelope(entry: PsgInstrumentEntry, tables: Mapping[str, PsgEnvelope],
                      verbose: bool = False) -> PsgEnvelope | None:
    """The entry's envelope, or None for constant volume.

    A name (``fTone_01`` …) is the song's driver's envelope (SmpsSong.psg_envelopes: Sonic 1's
    for an asm song, a ROM's own); an inline list is held at its last step.
    """
    e = entry.envelope
    if e is None:
        return None
    if isinstance(e, str):
        table = tables.get(e)
        if table is None and verbose:
            print(f"  Warning: unknown envelope name '{e}' — rendering at constant volume")
        return table
    return PsgEnvelope(tuple(e))


def _envelope_frames(envelope: PsgEnvelope | None, secs: float, fps: float) -> list[int] | None:
    """The steps a render of `secs` plays (a looping envelope unrolled; a held one as written,
    the renderer holding its last step)."""
    if envelope is None:
        return None
    return envelope.frames(math.ceil(secs * fps) + 1)


def _check_warnings(caught, inst_num, verbose: bool = False):
    for w in caught:
        if verbose and issubclass(w.category, UserWarning) and "silence" in str(w.message):
            print(f"  Warning: instrument {inst_num} rendered silence")


class _PsgRenderer:
    """A catalogue entry's chip render (through the render cache), shelved and centred; a tone
    whose envelope holds cut at a sustain loop (with loops), put in `loops_out`.  Noise never loops."""

    def __init__(self, synth: PsgSynthesisSettings, fps: float, cache: RenderCache, verbose: bool,
                 rate3_dividers: dict | None, loops: bool, loops_out: dict | None,
                 envelopes: Mapping[str, PsgEnvelope]):
        self._synth = synth
        self._envelopes = envelopes
        self._fps = fps
        self._cache = cache
        self._verbose = verbose
        self._rate3_dividers = rate3_dividers
        self._loops = loops
        self._loops_out = loops_out

    def render(self, spec: PsgInstrument) -> tuple[Sequence[float], int] | None:
        """(mono, rate) of the catalogue entry, its silent tail trimmed; None where nothing is heard."""
        entry = spec.entry
        inst_num = entry.mod_instrument

        # target_rate: exact Hz the MOD will play back at (period = amiga_clock / rate).
        # Noise and tone both use this. For noise, this is the only pitch-relevant parameter.
        # A tone rendered synth_shift semitones above the pitch `root` sounds (resolve_synth_roots)
        # gets a rate raised by the same ratio, so MOD note root still sounds that pitch.
        target_rate = spec.target_rate(self._synth.amiga_clock)
        envelope = _resolve_envelope(entry, self._envelopes, verbose=self._verbose)
        env_info = f" envelope={entry.envelope}({len(envelope.steps)}fr)" if envelope else ""

        entry_type = entry.type.lower()
        if entry_type == _TONE:
            mono, rate = self._tone(entry, spec.synth_idx, target_rate, envelope, env_info)
        elif entry_type in (_WHITE_NOISE, _PERIODIC_NOISE):
            mono, rate = self._noise(entry, spec.synth_idx, entry_type == _WHITE_NOISE, target_rate, envelope, env_info)
        else:
            if self._verbose:
                print(f"  Warning: unknown psg entry type '{entry.type}' for inst {inst_num} — skipping")
            return None

        if not mono:
            if self._verbose:
                print(f"  Warning: instrument {inst_num} (PSG) rendered empty — skipping")
            return None

        mono = _trim_trailing_silence(mono)
        if not mono:
            if self._verbose:
                print(f"  Warning: instrument {inst_num} (PSG) rendered all silence — skipping")
            return None

        if self._verbose:
            print(f"  Instrument {inst_num:2d}: {len(mono)} samples @ {rate} Hz  peak={peak(mono):.0f}")
        return mono, rate

    def _tone(self, entry: PsgInstrumentEntry, note: int, target_rate: int, envelope: PsgEnvelope | None,
              env_info: str) -> tuple[Sequence[float], int]:
        synth, inst_num = self._synth, entry.mod_instrument
        n_val = note_to_psg_n(note, synth.clock_rate)
        if self._verbose:
            freq_hz = 440.0 * (2.0 ** ((note - 45) / 12.0))
            print(f"  [psg synth] inst={inst_num} tone  "
                  f"synth_note={note} freq={freq_hz:.1f}Hz N={n_val}  "
                  f"root={entry.root.name} rate={target_rate}Hz{env_info}")

        # A MOD sample holds at most max_sample_kb (settings.yaml), so at this rate the
        # sustain can only be so long (the converter warns where a note needs more).
        # This instrument's own longest ring when `auto` resolved one, else the setting
        want = synth.sustain_by_instrument.get(inst_num, synth.sustain_duration)
        fits = max_sustain_secs(target_rate, synth.release_padding, synth.max_sample_bytes)
        sustain = min(want, fits)
        if self._verbose and sustain < want:
            print(f"  [psg synth] inst={inst_num} sustain capped at {sustain:.2f}s "
                  f"({synth.max_sample_kb} KiB sample limit at {target_rate}Hz)")
        if not self._loops:
            return self._tone_render(entry, note, target_rate, envelope, sustain)

        # Rendered long enough to see the envelope settle; again for its own sustain where no loop is
        probe = probe_secs(sustain, fits)
        mono, rate = self._tone_render(entry, note, target_rate, envelope, probe)
        loop = self._loop(inst_num, mono, rate, n_val, sustain, probe)
        if loop is not None:
            if self._loops_out is not None:
                self._loops_out[inst_num] = loop
            return apply_loop(mono, loop), rate
        if probe > sustain:
            return self._tone_render(entry, note, target_rate, envelope, sustain)
        return mono, rate

    def _tone_render(self, entry: PsgInstrumentEntry, note: int, target_rate: int, envelope: PsgEnvelope | None,
                     secs: float) -> tuple[Sequence[float], int]:
        synth = self._synth
        return self._render(
            render_psg_tone_raw, entry.mod_instrument,
            mod_note_index=note,
            sustain_secs=secs,
            release_secs=synth.release_padding,
            clock_rate=synth.clock_rate,
            target_rate=target_rate,
            envelope=_envelope_frames(envelope, secs, self._fps),
            base_volume=entry.base_volume,
            fps=self._fps,
            oversample=synth.psg_oversample,
            taps=synth.resample_taps,
        )

    def _loop(self, inst_num: int, mono: Sequence[float], rate: int, n_val: int, sustain: float,
              probe: float) -> SustainLoop | None:
        """The tone's sustain loop, where one ends before the plain render would."""
        synth = self._synth
        period = rate * 32.0 * n_val / synth.clock_rate
        plain_n = int(rate * (sustain + synth.release_padding))
        ref = min(synth.loop_ref_by_instrument.get(inst_num, sustain), probe)
        loop = find_sustain_loop(mono, rate, period, int(rate * probe), ref_n=int(rate * ref),
                                 max_end=min(plain_n, int(rate * probe)), flat_db=synth.loop_drift_db,
                                 timbre=synth.loop_timbre)

        # A PSG note is cut at its end: where the sustain holds every note, a loop ending
        # past it is longer than the plain render, and less faithful
        if loop is not None and inst_num in synth.exact_sustain and loop.end > int(rate * sustain):
            return None
        return loop

    def _noise(self, entry: PsgInstrumentEntry, note: int, white: bool, target_rate: int, envelope: PsgEnvelope | None,
               env_info: str) -> tuple[Sequence[float], int]:
        # target_rate = amiga_clock / PERIOD_TABLE[root] is both the synthesis rate and the
        # MOD playback rate when triggered at root. When triggered at other notes (via low/high
        # range anchoring), the MOD plays back faster/slower, approximating the LFSR frequency
        # change per note. root choice affects synthesis quality and the pitch anchor.
        synth, inst_num = self._synth, entry.mod_instrument
        tone2_n = self._tone2_n(entry, note) if entry.noise_rate == 3 else None

        # Cap sustain to envelope length so the sample ends at the natural decay tail
        # rather than holding noise output for the full song-longest-note duration.
        # Include ramp-to-silence frames so _render_with_envelope can fade to attenuation 15.
        env_frames = noise_envelope_frames(envelope, entry.base_volume)
        sustain = env_frames / self._fps if env_frames is not None else min(synth.sustain_duration, 0.5)
        if inst_num in synth.exact_sustain:       # the notes are cut sooner than it decays
            sustain = min(sustain, synth.sustain_by_instrument[inst_num])
        if self._verbose:
            noise_label = "white" if white else "periodic"
            print(f"  [psg synth] inst={inst_num} {noise_label}_noise  "
                  f"rate={entry.noise_rate}  tone2_n={tone2_n}  root={entry.root.name}  "
                  f"target_rate={target_rate}Hz  sustain={sustain:.3f}s{env_info}")
        return self._render(
            render_psg_noise_raw, inst_num,
            white=white,
            noise_rate=entry.noise_rate,
            sustain_secs=sustain,
            release_secs=synth.release_padding,
            clock_rate=synth.clock_rate,
            target_rate=target_rate,
            envelope=_envelope_frames(envelope, float(sustain), self._fps),
            base_volume=entry.base_volume,
            fps=self._fps,
            tone2_n=tone2_n,
        )

    def _tone2_n(self, entry: PsgInstrumentEntry, note: int) -> int:
        """Rate 3 follows tone ch2: the divider that clocks the LFSR at the hardware's frequency
        (~220 Hz for A3, not the emulator's reset N=1, near sample_rate/2: wrong timbre)."""
        # Explicit divider from the config (e.g. tone2_n: 1 for nMaxPSG, which the
        # driver writes as N=0 and the Sega VDP PSG clocks as N=1).
        if entry.tone2_n is not None:
            return entry.tone2_n

        # Neither given: the divider the driver writes for this instrument's notes,
        # worked out from the song by derive_rate3_dividers.
        dividers = self._rate3_dividers
        if entry.synth_root is None and dividers and entry.mod_instrument in dividers:
            return dividers[entry.mod_instrument]
        return note_to_psg_n(note, self._synth.clock_rate)

    def _render(self, render, inst_num: int, **kwargs) -> tuple[Sequence[float], int]:
        """render(**kwargs) through the render cache, shelved and centred (before any loop is found in it)."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            mono, rate = _cached_render(self._cache, render, **kwargs)
        _check_warnings(caught, inst_num, verbose=self._verbose)
        synth = self._synth
        return condition_render(mono, rate, [(synth.treble_shelf_hz, synth.treble_shelf_db)], synth.dc_block), rate


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_psg_samples(
    config: ConversionConfig,
    psg_synth: PsgSynthesisSettings,
    verbose: bool = False,
    rate3_dividers: dict | None = None,
    noise_envelopes: dict | None = None,
    psg_envelopes: Mapping[str, PsgEnvelope] | None = None,
    loops: bool = False,
    loops_out: dict[int, SustainLoop] | None = None,
    raw_out: dict[int, tuple] | None = None,
    cache_out: dict[str, int] | None = None,
) -> dict:
    """Render a PSG sample for every instrument in the config's catalogue.

    Args:
        config:    ConversionConfig — provides psg_map and region.
        psg_synth: PsgSynthesisSettings — clock/amiga_clock/sustain/release.
        rate3_dividers:  {instrument: tone-2 divider} the converter derived for rate-3 noise.
        noise_envelopes: {instrument: envelope label} the converter derived for the noise
                         instruments (derive_noise_envelopes) — a psg_map
                         entry's own instrument and each of its `envelopes:` variants.
        psg_envelopes: the song's envelopes by name (SmpsSong.psg_envelopes); None: Sonic 1's.
        loops:     cut each tone whose envelope holds at a sustain loop (core.audio.loops), reported
                   in `loops_out` ({instrument: SustainLoop}); noise is never looped.
        cache_out: filled with {"hits": n, "misses": n} of the render cache
                   (settings.yaml samples.render_cache; nothing when it is off).

    Returns:
        {instrument_number: (pcm_bytes, sample_rate_hz)} — 8-bit signed mono PCM.
    """
    if not config.psg_map and not config.psg_voice_map:
        return {}

    fps = 50.0 if config.region.lower() == 'pal' else 60.0

    # One render per catalogue instrument: each psg_map entry's own instrument, then its
    # envelope variants, then the psg_voice_map tone entries (core.plan.instruments.psg_catalogue).
    catalogue = psg_catalogue(config, noise_envelopes or {})
    cache = RenderCache(psg_synth.render_cache, "sn76489", _render_salt() if psg_synth.render_cache else "")
    renderer = _PsgRenderer(psg_synth, fps, cache, verbose, rate3_dividers, loops, loops_out,
                            psg_envelopes if psg_envelopes is not None else SONIC1_ENVELOPES)
    raw_data: dict[int, tuple[Sequence[float], int]] = {}   # inst_num -> (mono, rate)
    for spec in catalogue.values():
        rendered = renderer.render(spec)
        if rendered is not None:
            raw_data[spec.entry.mod_instrument] = rendered
    if cache_out is not None and cache.enabled:
        cache_out.update(hits=cache.hits, misses=cache.misses)
    if raw_out is not None:                  # the unquantised renders, for the composite mixer
        raw_out.update(raw_data)

    # --- Quantise, each instrument to its own full 8 bits ---
    # The level is the sample_list volume's job (measured against the VGZ); the noise channel
    # used to sit at half scale (the emulator halves it), which only cost it a bit.
    return {inst: (full_scale_int8(mono, catalogue[inst].entry.dither or psg_synth.dither), rate)
            for inst, (mono, rate) in raw_data.items()}


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Render one tone and one noise entry from a minimal fake config."""

    import dataclasses

    from core.config import PsgInstrumentEntry, find_settings, load_settings

    psg_synth = dataclasses.replace(load_settings(find_settings())[1],   # settings.yaml, a short fixed hold
                                    enabled=True, sustain_duration=0.5, release_padding=0.1)

    fake_config = ConversionConfig()
    # psg_map is a dict keyed by form byte; type is auto-inferred in production,
    # but can be set explicitly when constructing entries directly.
    fake_config.psg_map = {
        0xE0: PsgInstrumentEntry(
            mod_instrument=14,
            type="periodic_noise",
            root=ModNote.C3,
        ),
        0xE7: PsgInstrumentEntry(
            mod_instrument=15,
            type="white_noise",
            noise_rate=0,
            root=ModNote.C2,
            envelope="fTone_04",
            base_volume=0,
        ),
    }

    print("Smoke test — generate_psg_samples(tone@C3, white_noise@C2 w/ PSG4 envelope)...")
    print(f"  clock_rate    = {psg_synth.clock_rate}")
    print(f"  amiga_clock   = {psg_synth.amiga_clock}")
    print(f"  sustain       = {psg_synth.sustain_duration}s")
    print()

    samples = generate_psg_samples(fake_config, psg_synth, verbose=True)

    if not samples:
        print("  ERROR: no samples generated")
        sys.exit(1)

    print(f"\n  Generated {len(samples)} instrument(s)")
    for inst_num, (pcm, rate) in samples.items():
        print(f"    instrument {inst_num}: {len(pcm)} bytes @ {rate} Hz")

    out_dir = Path(__file__).parent.parent / "output"
    out_dir.mkdir(exist_ok=True)

    for inst_num, (pcm, _) in samples.items():
        out_path = out_dir / f"psg_sample_gen_test_{inst_num}.raw"
        n = int8_to_raw16(out_path, pcm)
        print(f"  Written: {out_path}  ({n} bytes, 16-bit for Audacity)")

    print()
    print("SUCCESS")


if __name__ == "__main__":
    _smoke_test()
