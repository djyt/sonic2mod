# SN76489 PSG Synthesis

How sonic2mod renders each PSG instrument as an 8-bit MOD sample on the VGMPlay SN76489 core.

Related: `docs/fm_synthesis.md` (the YM2612 counterpart; shared rules are not repeated here),
`docs/smps_driver.md` § PSG Channels (what the driver does), `docs/yaml_config.md` § 4 (`psg_map` / `psg_voice_map` keys)
and § 8 (settings.yaml).

---

## Overview

```
catalogue entry → render_psg_tone_raw / render_psg_noise_raw → shelf, DC block → loop (tones) → int8 (peak-normalised, dithered)
```

| Type | SN76489 mode | Where it comes from |
|------|--------------|---------------------|
| `tone` | Square wave, tone channels 0–2 | `psg_voice_map` (always tone) |
| `white_noise` | White-noise LFSR | `psg_map` key byte, bit 2 = 1 |
| `periodic_noise` | Periodic-noise LFSR | `psg_map` key byte, bit 2 = 0 |

| File | Role |
|------|------|
| `sn76489/build.py`, `sn76489/wrapper.py` | Compile `reference/SN76489/sn76489.c` + `panning.c` (`core.cbuild`); ctypes `SN76489` class |
| `sn76489/renderer.py` | Note or noise form → PCM (`render_psg_tone_raw`, `render_psg_noise_raw`) |
| `sn76489/sample_generator.py` | The config's PSG catalogue → `{inst: (pcm, rate)}` (`generate_psg_samples`) |

---

## Quick Start

`psg_synthesis.enabled` is `true` in the shipped `configs/settings.yaml` (code default `false`).
The first render compiles the DLL, so gcc or MSVC must be on PATH.

```bash
python sn76489/validate.py           # C3 tone + white noise → output/psg_{tone,noise}_test.raw
python sn76489/renderer.py           # the same through the renderer
python sn76489/sample_generator.py   # periodic noise + white noise with fTone_04 → output/psg_sample_gen_test_*.raw
python convert.py configs/sonic_1/01_title_screen.yaml
```

Audacity: File > Import > Raw Data, signed 16-bit PCM, little-endian, mono, at the printed rate.

---

## How a note becomes a sample

### The catalogue: what is rendered

`core.plan.instruments.psg_catalogue` lists every PSG instrument, rendered once for the
**first** entry that names its slot: each `psg_map` entry, then its `envelopes:` variants
(each a slot of its own), then the `psg_voice_map` entries.  An entry without `root` renders
nothing.  The merged build drops what it no longer plays; an instrument that is only a mix
source is rendered for the mixer and gives up its slot.

A `psg_map` key is the SN76489 noise byte `$E0 | white << 2 | rate`, so the type and rate are
read from it, never configured.  The noise envelope is read from the song
(`core.plan.noise_derive.derive_noise_envelopes`): the label most of the instrument's notes
play under (the header voice or the last `smpsPSGvoice`; ties to the first heard).  A
`psg_voice_map` entry plays its own label's envelope.  A stated `envelope:` overrides either.

### Tone pitch: synth_root, synth_shift, target_rate

As for FM (`docs/fm_synthesis.md` § Pitch): `resolve_synth_roots` derives `synth_root` from
the chip pitch the entry's notes play, through the driver's table, at most an octave above the
pitch `root` sounds, and the shift goes into the rate:

```
target_rate = round(amiga_clock / PERIOD_TABLE[root] × 2^(synth_shift / 12))
```

The tone is rendered at `synth_root` (at `root` when there is none).  An entry with `low`
places notes at `root + (key − low)`; a rooted tone without `low` takes the channel's
transpose path (`docs/pipeline.md` § `voice_map` routing).

### Tone divider

`note_to_psg_n` finds the divider the song's driver writes for the note in its table
(`rules.psg_frequencies`; index 0 = `nC0` = C3, so MOD index i is table index i − 24), and the
renderer takes that divider.  The table is what the hardware plays; Sonic 1's differs from equal
temperament by up to 85 cents at the top.  Off the table, or at another clock:

```
N = round(clock / (32 × freq)),  clamped 1–1023
```

### Noise pitch

A noise entry's `root` sets its rate and its MOD note.  With `low`, notes are placed at
`root + (key − low)` and MOD playback speed follows the melody; without it, every note plays at
`root`.  Rates 0–2 clock the LFSR at a fixed N/512, N/1024, N/2048; rate 3 follows tone channel 2.

### Rate-3 noise: the tone-2 divider

Rate 3 (every Sonic 1 song's `$E7`) clocks the LFSR from tone channel 2's divider, and the
driver keeps writing PSG3's own note there even in noise mode.  So the divider is in the song:
`core.plan.noise_derive.derive_rate3_dividers` looks it up as the driver does,
`PSGFrequencies[note − $81 + transpose]`:

- at the entry's `low` note when it has one (the sample plays at `root` for that note) —
  Marble Zone: `low: A3`, transpose `$0B` → index 56 → **N = 34** (3290 Hz);
- otherwise at the note the instrument plays most — every hi-hat is `nMaxPSG` → **N = 1**.

`nMaxPSG` is not a pitch: it indexes the table's last entry, divider 0, which the Sega PSG
clocks as 1 — an LFSR at clock/32 ≈ 112 kHz, near-white hiss.  A chromatic
`A8` there gives N ≈ 16, a dull 7 kHz rattle; `rate3_synth_root_issues` warns for a rate-3
`synth_root` outside the table's C3–Gs8.  `convert.py --verbose` prints the divider used.

Precedence: `tone2_n`, then `synth_root` (its note's divider), then the derivation.  Before the
audible render the LFSR is spun at N = 1 for 4096 discarded samples, past the shift register's
start-up run of zero bits.

The sample holds one LFSR rate; the hardware retunes it every note.  A pitched noise channel is
approximated by MOD playback speed — acceptable for Marble Zone's short `fTone_09` bursts over
nine semitones.

### Envelopes

An envelope name resolves through the song's own tables (`rules.psg_envelopes`; Sonic 1's are
`core.drivers.reference.SONIC1_ENVELOPES`, the driver data in `docs/smps_driver.md` § PSG volume
envelopes).  One step, an attenuation added to `base_volume`, is written per frame
(60 Hz, 50 with `region: pal`).  After the last step a **tone holds** it, as the driver's `$80`
terminator does — so a held tone settles and can loop; **noise** ramps one step per frame to
silence (a hat's tail).  An inline list (`envelope: [0, 0, 2, 4]`) works the same; a looping
envelope (another driver's) is unrolled.

### Length

Tones follow the FM rules (`docs/fm_synthesis.md` § Length: `sustain_duration: auto`): each
instrument its own longest ring at its playback rate, capped at 10 s and at what fits
`samples.max_sample_kb`.  Noise is rendered for its envelope plus the ramp to silence
(`noise_envelope_frames`), or at most 0.5 s without an envelope, and no longer than its notes
where its auto sustain holds them all.  The chip has no release: `release_padding` renders
silence, trimmed off, though it still counts against the sample limit.

### Sustain loops

With `samples.sustain_loops` on for the build, a tone whose envelope holds is probed and cut to
a loop as FM is (`docs/fm_synthesis.md` § Sustain loops); a loop ending past the notes' sustain
is dropped.  Noise never loops, and PSG notes end in cuts, not release slides.

### Oversampling

A tone renders at `psg_synthesis.oversample` × the sample's rate (8) and is resampled down
(`core.audio.resample`, `samples.resample_taps`).  At the sample's own rate the core's
anti-aliasing (`IntermediatePos`) is a box average: −1.9 dB at 70 % of Nyquist, −3.9 dB at
Nyquist, aliases folding back; 4× is within 0.2 dB of 8× at half the time.  Noise renders at
the sample's rate: it is white either way, and band-limiting raised its crest factor and cost
7 dB of level.

### Conditioning and quantisation

As FM (`docs/fm_synthesis.md` § Conditioning and quantisation): shelf, DC block, trailing
silence trimmed, peak-normalised to the full 8 bits with the entry's or `samples.dither`.  The
tone:noise balance is the `sample_list` volumes'.

---

## SN76489 Emulator Internals

`reference/SN76489/sn76489.c` (VGMPlay), configured for the Mega Drive: `FB_SEGAVDP = 0x0009`
(16-bit LFSR feedback), `SRW_SEGAVDP = 16`, `boost_noise = 1`.  `PSGVolumeValues[16]`: 2 dB per
attenuation step, 4096 at 0, silence at 15.  `SN76489_Update` writes stereo int32, about ±4096
per channel.

**`SN76489_Reset` must run after `SN76489_Init`** — the C source has it commented out of
`Init`, leaving `Registers[]`, `ToneFreqVals[]` and `IntermediatePos[]` as malloc garbage
(out-of-bounds `PSGVolumeValues` reads).  `SN76489.reset()` does it.

| Write | Bytes |
|-------|-------|
| Tone divider N (channels 0–2) | `1 CC 0 NNNN` (low 4 bits), then `0 0 NNNNNN` (N >> 4) |
| Volume (channels 0–3) | `1 CC 1 VVVV` (0 = loudest, 15 = silent) |
| Noise (channel 3) | `0xE0 \| white << 2 \| rate` (rate 0/1/2 = N/512, N/1024, N/2048; 3 = tone channel 2) |

---

## Module API

### `sn76489/wrapper.py`

```python
SN76489(clock_rate=3579545, sample_rate=44100)   # the output rate is fixed at construction
sn.reset()
sn.write(byte)
sn.write_tone_freq(ch, n)        # ch 0–2, N clamped 1–1023
sn.write_volume(ch, vol)         # ch 0–3, 0 = loudest, 15 = silent
sn.write_noise(white, rate)      # rate 0–3
sn.render_samples(n) -> list[tuple[int, int]]
sn.shutdown()
```

### `sn76489/renderer.py`

```python
note_to_psg_n(mod_note_index, clock_rate=3579545) -> int            # idx 0 = C1, 24 = C3
render_psg_tone_raw(mod_note_index, sustain_secs=1.0, release_secs=0.2, clock_rate=3579545,
                    target_rate=None, envelope=None, base_volume=0, fps=60.0,
                    oversample=8, taps=32) -> (list, rate)
render_psg_noise_raw(white, noise_rate, sustain_secs=0.4, release_secs=0.1, clock_rate=3579545,
                     target_rate=None, envelope=None, base_volume=0, fps=60.0,
                     tone2_n=None) -> (list, rate)
render_psg_tone(...) / render_psg_noise(...) -> (bytes, rate)       # int8, peak-normalised
```

`target_rate=None` renders at 44 100 Hz.  `envelope` is the per-frame step list.

### `sn76489/sample_generator.py`

```python
generate_psg_samples(config, psg_synth, rules, verbose=False, rate3_dividers=None, noise_envelopes=None,
                     loops=False, loops_out=None, raw_out=None,
                     cache_out=None) -> dict[int, tuple[bytes, int]]
```

Returns `{instrument: (int8 PCM, rate)}` for every catalogue instrument.  The converter passes
the song's `rules` (its PSG table and envelopes) and the derived `rate3_dividers` and
`noise_envelopes` (`{instrument: ...}`); `psg_synth.sustain_duration` must already be resolved.

---

## Common Mistakes

### A stated synth_root or tone2_n

Both are derived from the song; delete stated values rather than correct them.  A stated tone
`synth_root` only moves the rendering pitch (the notes stay in tune, the sample stretches); a
stated rate-3 `synth_root` or `tone2_n` replaces the divider the hardware uses.

### Noise too loud or too quiet against the tones

Every sample is peak-normalised, so the balance is the `sample_list` volumes': measure them with
`tools/vgm_compare.py --write-volumes`.

### One noise sample for several envelopes

`noise_envelopes` warns when an instrument plays under several labels: give a label its own
slot in the entry's `envelopes:` (Credits' PSG3 has no free slot and keeps the warning).
