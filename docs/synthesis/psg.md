# SN76489 PSG rendering

How sonic2mod turns each PSG instrument (square-wave tones and noise) into an 8-bit MOD sample by
playing it on an emulated SN76489 (VGMPlay's core).  For anyone tuning PSG instruments or changing
`core/synth/psg_*.py`.  The FM counterpart, which explains the shared ideas in full, is [fm.md](fm.md);
what the driver does with the PSG is [../drivers/sonic_1.md](../drivers/sonic_1.md).

On this page:

- [Why render on a chip](#why-render-on-a-chip)
- [Quick start](#quick-start)
- [The pipeline](#the-pipeline)
- [What is rendered](#what-is-rendered)
- [Tone pitch](#tone-pitch)
- [Noise pitch](#noise-pitch)
- [Envelopes](#envelopes)
- [Length and loops](#length-and-loops)
- [Oversampling, conditioning, quantisation](#oversampling-conditioning-quantisation)
- [Inside the emulator](#inside-the-emulator)
- [Module API](#module-api)
- [Common mistakes](#common-mistakes)

---

## Why render on a chip

The Mega Drive's SN76489 PSG makes sound from a few registers: three square-wave tone channels and one
noise channel, each with a 4-bit attenuation the driver steps once a frame (a volume *envelope*).  An
Amiga only plays recorded samples.  So, as for FM ([fm.md § Why render on a chip](fm.md#why-render-on-a-chip)),
each PSG instrument is played once on an emulated chip and recorded:

```
divider / noise byte  ──► emulated SN76489 ──► PCM ──► 8-bit MOD sample
+ envelope, per frame     (VGMPlay core)               played at any note by the tracker
```

**One sample per instrument range, at one pitch,** resampled by the tracker for every other note.  A
square wave stretches well; what stretches with it is the envelope, which runs per frame on the
hardware but faster or slower in the MOD as the note moves from the rendered pitch.

---

## Quick start

Shipped `configs/settings.yaml` has `psg_synthesis.enabled: true` (code default `false`).  The first
render compiles `3rdparty/sn76489/sn76489.c` + `panning.c` into `build/` with gcc or MSVC; with neither
on PATH, Windows loads `prebuilt/sn76489.dll`.

```bash
python tools/chips/validate_sn76489.py      # smoke test, see below
python convert.py configs/sonic_1/01_title_screen.yaml
```

`validate_sn76489.py` writes, in turn: the device's C3 tone and white noise
(`output/psg_{tone,noise}_test.raw`); `psg_render`'s (`output/psg_render_{tone,noise}_test.raw`);
`generate_psg_samples`' tone and noise entries (`output/psg_sample_gen_test_*.raw`).  Open them in
Audacity: File > Import > Raw Data, signed 16-bit PCM, little-endian, mono, at the printed rate.

Settings (`psg_synthesis:`, `samples:`): [../guide/settings.md](../guide/settings.md).

---

## The pipeline

```
catalogue entry ──► render_psg_tone_raw  (tone: at 8× the rate, resampled down)
                └─► render_psg_noise_raw (noise: at the sample's rate)
                         │
                         ▼
               treble shelf, DC block ──► loop (tones) ──► int8 (peak-normalised, dithered)
```

| Type | Chip mode | Comes from |
|------|-----------|------------|
| `tone` | Square wave, tone channels 0–2 | `psg_voice_map` (always tone) |
| `white_noise` | White-noise LFSR | `psg_map` key byte, bit 2 = 1 |
| `periodic_noise` | Periodic-noise LFSR (a buzzy tone) | `psg_map` key byte, bit 2 = 0 |

| File | Role |
|------|------|
| `core/chips/sn76489/build.py`, `wrapper.py` | The device: compiles the C core (`core.chips.cbuild`); the ctypes `SN76489` class |
| `core/chips/psg.py` | SN76489 facts: clock, divider formula, 2 dB attenuation step, noise byte |
| `core/synth/psg_render.py` | Divider or noise form → PCM (`render_psg_tone_raw`, `render_psg_noise_raw`) |
| `core/synth/psg_samples.py` | The config's PSG catalogue → `{inst: (pcm, rate)}` (`generate_psg_samples`) |

---

## What is rendered

The [catalogue](../glossary.md#catalogue) (`core.plan.instruments.psg_catalogue`) lists every PSG
instrument, rendered once for the **first** entry
that names its slot: each `psg_map` entry, then its `envelopes:` variants (each a slot of its own),
then the `psg_voice_map` entries.  An entry without `root` renders nothing.  The merged build drops
what it no longer plays; an instrument only used as a [mix](../glossary.md#mix) source is rendered for
the mixer and gives up its slot.

A `psg_map` key is the SN76489 noise byte `$E0 | white << 2 | rate`, so type and rate are read from it,
never configured.  Example, Scrap Brain Zone:

```yaml
psg_map:
  0xE7:                    # white noise, rate 3 (LFSR clocked by tone channel 2)
    mod_instrument: 17     # plays under the envelope the song has in force: fTone_04
    root: A3
    envelopes:
      fTone_08: 18         # the loop's fTone_08 hi-hat gets its own sample
```

The noise envelope is read from the song (`core.plan.noise_derive.derive_noise_envelopes`): the label
most of the instrument's notes play under (the header voice or the last `smpsPSGvoice`; ties to the
first heard).  A `psg_voice_map` entry plays its own label's envelope.  A stated `envelope:` overrides
either.

---

## Tone pitch

### synth_root, synth_shift, target_rate

As for FM ([fm.md § Pitch](fm.md#pitch)): `resolve_synth_roots` derives
[`synth_root`](../glossary.md#synth_root) from the chip pitch the entry's notes play most, at most an
octave above the pitch `root` sounds, and the [`synth_shift`](../glossary.md#synth_shift) goes into the
rate:

```
target_rate = round(amiga_clock / PERIOD_TABLE[root] × 2^(synth_shift / 12))
```

The tone is rendered at `synth_root` (at `root` when there is none).  An entry with `low` places notes
at `root + (key − low)`; a rooted tone without `low` takes the channel's transpose path
([../conversion/notes.md](../conversion/notes.md)).

### The tone divider

The SN76489 sets a tone by a 10-bit divider N: `f = clock / (32 × N)`.  `note_to_psg_n` finds the
divider the song's driver writes for the note in its table (`rules.psg_frequencies`; Sonic 1's starts
at C3, so MOD index i is table index i − 24).  The table is what the hardware plays: Sonic 1's differs
from rounded equal temperament on 29 of its 70 entries (`nMaxPSG` included), by up to 83 cents.  Off the table,
or at another clock:

```
N = round(clock / (32 × freq)),  clamped 1–1023        C3 (130.8 Hz) at 3 579 545 Hz → N = 855
```

A detune variant renders its tone the detune's dividers off the table's
([../conversion/detune.md](../conversion/detune.md)).

---

## Noise pitch

A noise entry's `root` sets its rate and its MOD note.  With `low`, notes are placed at
`root + (key − low)` and the MOD playback speed follows the melody; without it, every note plays at
`root`.  Example: Title Screen's noise at `root: A3` plays at 27 928 Hz, whose 14 kHz bandwidth keeps
most of the hiss.

The noise register's rate bits pick what clocks the LFSR (the shift register making the noise):

| Rate | LFSR clock |
|------|-----------|
| 0, 1, 2 | Fixed: clock / 512, / 1024, / 2048 |
| 3 | Tone channel 2's divider (PSG3: the chip counts its channels from 0) |

### Rate 3: the tone-2 divider

Rate 3 (every Sonic 1 song's `$E7`) clocks the LFSR from tone channel 2, PSG3's, and the driver keeps
writing PSG3's own note there even in noise mode.  So this
[tone-2 divider](../glossary.md#tone-2-divider) is in the song:
`core.plan.noise_derive.derive_rate3_dividers` looks it up as the driver does,
`PSGFrequencies[note − $81 + transpose]`:

- at the entry's `low` note when it has one (the sample plays at `root` for that note).  Marble Zone:
  `low: A3`, transpose `$0B` → index 56 → **N = 34** (3 290 Hz);
- otherwise at the note the instrument plays most.  Every Sonic 1 hi-hat is `nMaxPSG` → **N = 1**.

`nMaxPSG` is not a pitch: it indexes the table's last entry, divider 0, which the Sega PSG clocks as 1:
an LFSR at clock / 32 ≈ 112 kHz, near-white hiss.  A chromatic `A8` there would give N ≈ 16, a dull
7 kHz rattle; `rate3_synth_root_issues` warns for a rate-3 `synth_root` outside the table's C3–G#8.
`convert.py --verbose` prints the divider used.

Precedence: a stated `tone2_n`, then a stated `synth_root` (its note's divider), then the derivation.
Before the audible render the LFSR is spun at N = 1 for 4096 discarded samples, past the shift
register's start-up run of zero bits.

The sample holds one LFSR rate; the hardware retunes it every note.  A pitched noise channel is
approximated by MOD playback speed: fine for Marble Zone's short `fTone_09` bursts over nine semitones.

---

## Envelopes

An envelope name resolves through the song's own driver tables (`rules.psg_envelopes`; Sonic 1's
listed in [../drivers/sonic_1.md](../drivers/sonic_1.md)).  One step, an attenuation added to the entry's
`base_volume`, is written per driver frame: 60 Hz NTSC, 50 Hz with `region: pal`, or the driver's
Timer A rate.  After the last step:

| | What the renderer does | Why |
|-|------------------------|-----|
| Tone | Holds the last step | The driver's `$80` terminator holds it: a held tone settles and can loop |
| Noise | Ramps one step per frame to silence | Gives a hi-hat its tail and the sample an end |

An inline list (`envelope: [0, 0, 2, 4]`) works the same.  A looping envelope (another driver's) is
unrolled over the render.

---

## Length and loops

Sample-length rules shared with FM, sustain loops and release slides:
[../conversion/samples.md](../conversion/samples.md).  The PSG specifics:

- **Tones** follow the FM rules ([fm.md § Length and loops](fm.md#length-and-loops)):
  `psg_synthesis.sustain_duration: auto` (shipped; code default 1.0 s) renders each instrument for its
  own longest note, capped at 10 s and at what fits `samples.max_sample_kb` at its rate.
- **Noise** is rendered for its envelope plus the ramp to silence (`noise_envelope_frames`), or at
  most 0.5 s without an envelope (or with a looping one), and no longer than its notes where its
  `auto` sustain holds them all.
- **No release on the chip.**  `psg_synthesis.release_padding` (code and shipped 0.2 s) renders
  silence, which is trimmed off but still counts against the sample limit.
- **Sustain loops.**  With `samples.sustain_loops` on for the build, a tone whose envelope holds is
  probed and cut to a loop as FM is; a loop ending past the notes' sustain is dropped.  Noise never
  loops.
- **Note ends.**  PSG notes end in cuts.  An envelope with a release phase (Streets of Rage 2's ADSR)
  reports its fall rate (`release_out`) for the converter's release slides.

---

## Oversampling, conditioning, quantisation

**Tones** render at `psg_synthesis.oversample` × the sample's rate (code and shipped 8) and are
resampled down (`core.audio.resample`, `samples.resample_taps`).  Why: at the sample's own rate the
core's anti-aliasing (`IntermediatePos`) is a box average: −1.9 dB at 70 % of Nyquist, −3.9 dB at
Nyquist, aliases folding back.  4× is within 0.2 dB of 8× at half the time; 1 uses the box filter.

**Noise** renders at the sample's rate: it is white either way, and band-limiting it would raise its
crest factor and cost 7 dB of level.

**Conditioning and quantisation** as FM ([fm.md § Resampling, conditioning,
quantisation](fm.md#resampling-conditioning-quantisation)): treble shelf, DC block, trailing silence
trimmed, peak-normalised to the full 8 bits with the entry's `dither:` or `samples.dither`.  The
tone:noise balance is the `sample_list` volumes'.  With `samples.render_cache` each render is cached
under a hash of its inputs and the PSG code.

---

## Inside the emulator

`3rdparty/sn76489/sn76489.c` (VGMPlay), configured for the Mega Drive: `FB_SEGAVDP = 0x0009` (16-bit
LFSR feedback), `SRW_SEGAVDP = 16`, `boost_noise = 1`.  `PSGVolumeValues[16]`: 2 dB per attenuation
step, 4096 at 0, silence at 15.  `SN76489_Update` writes stereo int32, about ±4096 per channel.

> **Pitfall:** `SN76489_Reset` must run after `SN76489_Init`.  The C source has it commented out of
> `Init`, leaving `Registers[]`, `ToneFreqVals[]` and `IntermediatePos[]` as malloc garbage
> (out-of-bounds `PSGVolumeValues` reads).  `SN76489.reset()` does it, and the constructor calls it.

| Write | Bytes |
|-------|-------|
| Tone divider N (channels 0–2) | `1 CC 0 NNNN` (low 4 bits), then `0 0 NNNNNN` (N >> 4) |
| Attenuation (channels 0–3) | `1 CC 1 VVVV` (0 = loudest, 15 = silent) |
| Noise (channel 3) | `$E0 \| white << 2 \| rate` (rate 0/1/2 fixed; 3 = tone channel 2) |

---

## Module API

### `core/chips/sn76489/wrapper.py`

```python
SN76489(clock_rate=3579545, sample_rate=44100)   # the output rate is fixed at construction
sn.reset()
sn.write(data)                       # a raw command byte
sn.write_tone_freq(ch, n)            # ch 0–2, N clamped 1–1023
sn.write_volume(ch, vol)             # ch 0–3, 0 = loudest, 15 = silent
sn.write_noise(white, rate)          # rate 0–3
sn.render_samples(n) -> list[tuple[int, int]]
sn.render_channels(n) -> (array('i'), array('i'))   # L, R: no tuple a sample
sn.shutdown()
```

### `core/synth/psg_render.py`

```python
note_to_psg_n(mod_note_index, rules, clock_rate=3579545, offset=0) -> int   # idx 0 = C1, 24 = C3
render_psg_tone_raw(divider, sustain_secs=1.0, release_secs=0.2, clock_rate=3579545,
                    target_rate=None, envelope=None, base_volume=0, fps=60.0,
                    oversample=8, taps=32) -> (list, rate)
render_psg_noise_raw(white, noise_rate, sustain_secs=0.4, release_secs=0.1, clock_rate=3579545,
                     target_rate=None, envelope=None, base_volume=0, fps=60.0,
                     tone2_n=None) -> (list, rate)
render_psg_tone(divider, sustain_secs=1.0, release_secs=0.2, clock_rate=3579545,
                target_rate=None) -> (bytes, rate)                      # int8, peak-normalised
render_psg_noise(white, noise_rate, sustain_secs=0.4, release_secs=0.1, clock_rate=3579545,
                 target_rate=None, tone2_n=None) -> (bytes, rate)       # int8, peak-normalised
render_psg_frames(frames, frame_hz, tail_secs=0.0, target_rate=44100,
                  clock_rate=3579545) -> (list, rate)                   # an FM drum's PSG part
```

`target_rate=None` renders at 44 100 Hz.  `envelope` is the per-frame step list.  `rules` is the
song's `PlaybackRules` (its PSG table).

### `core/synth/psg_samples.py`

```python
generate_psg_samples(conv, psg_synth, rules, verbose=False, rate3_dividers=None,
                     noise_envelopes=None, loops=False, loops_out=None, raw_out=None,
                     cache_out=None, release_out=None, pitches_out=None)
                     -> dict[int, tuple[bytes, int]]
```

Returns `{instrument: (int8 PCM, rate)}` for every catalogue instrument.  `conv` is the
`ConversionPlan`; the converter passes the song's `rules` (its PSG table and envelopes) and the derived
`rate3_dividers` and `noise_envelopes` (`{instrument: ...}`); `psg_synth.sustain_duration` must
already be resolved.

---

## Common mistakes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Hi-hat a dull rattle instead of hiss | A stated rate-3 `synth_root` or `tone2_n` replaces the divider the hardware uses (`A8` → N ≈ 16) | Delete it; the derivation reads the song's (`rate3_synth_root_issues` warns) |
| Tone sample stretched far from its notes | A stated tone `synth_root` far from where the notes play (they stay in tune; the envelope stretches) | Delete it; the derived one is the busiest chip pitch |
| Noise too loud or too quiet against the tones | Every sample is peak-normalised; balance is the `sample_list` volumes' | `python tools/vgm/vgm_compare.py <config> <vgz> --write-volumes` |
| `noise_envelopes` warning: one noise sample for several envelopes | The instrument plays under several envelope labels | Give a label its own slot in the entry's `envelopes:` (see the Scrap Brain example above) |
| PSG instruments come from files, or are silent | `psg_synthesis.enabled: false`, or no `psg_map` / `psg_voice_map` in the config | Enable it; add the maps ([../guide/song_configs.md](../guide/song_configs.md)) |
| A tone note goes silent before its end (`sustain_short`) | The sample is shorter than the note | As FM: `auto`, sustain loops, a lower `root` or `samples.max_sample_kb: 128` |
