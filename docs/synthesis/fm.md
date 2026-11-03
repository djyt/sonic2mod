# YM2612 FM rendering

How sonic2mod turns each FM instrument of a song into an 8-bit MOD sample by playing it on an emulated
YM2612 (Nuked-OPN2).  For anyone tuning how FM instruments sound, or changing `core/synth/`.  The PSG
counterpart is [psg.md](psg.md); where a sample's notes land is
[../conversion/notes.md](../conversion/notes.md).

On this page:

- [Why render on a chip](#why-render-on-a-chip)
- [Quick start](#quick-start)
- [The pipeline](#the-pipeline)
- [What is rendered](#what-is-rendered)
- [Pitch](#pitch)
- [Length and loops](#length-and-loops)
- [Level](#level)
- [Composites: several voices at once](#composites-several-voices-at-once)
- [FM drums: a frame-by-frame render](#fm-drums-a-frame-by-frame-render)
- [Resampling, conditioning, quantisation](#resampling-conditioning-quantisation)
- [Threads and the render cache](#threads-and-the-render-cache)
- [Inside the emulator](#inside-the-emulator)
- [Module API](#module-api)
- [Common mistakes](#common-mistakes)

---

## Why render on a chip

The Mega Drive does not store its music as sound.  Its YM2612 *synthesises* every note live: four
sine operators per channel modulate each other, shaped by envelopes, from about 30 bytes of settings
per instrument (a *voice*).  An Amiga cannot do that.  Its Paula chip only plays *samples*: recorded
8-bit waveforms, at a rate set by each note's period.

So the converter records each instrument once, the way the hardware would sound it, and hands the
recording to the MOD:

```
SMPS voice ──► emulated YM2612 ──► PCM at 53 267 Hz ──► resampled ──► 8-bit MOD sample
(registers)    (Nuked-OPN2)                              to the       played at any note
                                                         sample rate  by the tracker
```

**One sample per instrument range, at one pitch.**  A sample is rendered once, at one note.  The
tracker plays it higher or lower by changing its playback rate, which is how every note of the range
gets its pitch.  The cost: a whole FM tone, envelope and all, stretches with the rate, so a note an
octave above the rendered one plays its attack twice as fast.  The renderer picks the pitch most notes
play to keep that error small ([Pitch](#pitch)).  A voice whose notes span too far for one sample is
split by the config into several ranges, each its own sample (`voice_map`,
[../guide/song_configs.md](../guide/song_configs.md)).

---

## Quick start

Shipped `configs/settings.yaml` has `fm_synthesis.enabled: true` (code default `false`: every sample
then comes from the config's `sample_list` files).  The first render compiles
`3rdparty/nuked-opn2/ym3438.c` and `core/chips/ym2612/ym3438_batch.c` into `build/` with gcc or MSVC;
with neither on PATH, Windows loads `prebuilt/ym3438.dll`.

```bash
python tools/chips/validate_ym2612.py       # smoke test, see below
python convert.py configs/sonic_1/01_title_screen.yaml
python convert.py configs/sonic_1/01_title_screen.yaml --verbose   # each sample's rate, peak, loop
```

`validate_ym2612.py` checks, in turn: the device (an A4 test tone → `output/validate_test.raw`);
`program_voice` (Title Screen voice 0, not silent); `fm_render` (voice 1, the FM2 bass, at A3 →
`output/renderer_test.raw`); `generate_fm_samples` (the same voice as an instrument →
`output/sample_gen_test.raw`).  Open them in Audacity: File > Import > Raw Data, signed 16-bit PCM,
little-endian, mono, at the rate the script prints.

Settings (`fm_synthesis:`, `samples:`, `amiga_clock`): [../guide/settings.md](../guide/settings.md).

---

## The pipeline

```
catalogue entry ──► render_layers ──► treble shelf ──► loop or cut ──► int8
 (voice, pitch,     chip at 53 267 Hz,  DC block        (sustain loop,   peak-normalised,
  length, level)    resampled down                       or end where     dithered
                                                         notes stop)
```

| File | Role |
|------|------|
| `core/chips/ym2612/build.py`, `wrapper.py` | The device: compiles the C core (`core.chips.cbuild`); the ctypes `OPN2` class |
| `core/chips/fm.py` | YM2612 facts: clock, register layout, frequency formula, TL law, LFO |
| `core/synth/fm_voice.py` | `SmpsVoice` → YM2612 registers (`program_voice`) |
| `core/synth/fm_render.py` | Voices + pitch → PCM (`render_layers`, `render_frames`) |
| `core/synth/fm_samples.py` | The song's FM catalogue → `{inst: (pcm, rate)}` (`generate_fm_samples`, `generate_fm_drums`) |
| `core/audio/` | Resampler, shelf, DC block, sustain loops, int8 quantisation |
| `core/convert/samples.py` | Calls the generators and installs the samples in the MOD |

---

## What is rendered

The [catalogue](../glossary.md#catalogue) (`core.plan.instruments.fm_catalogue`) lists every FM
instrument and the config entry it is rendered for.  An instrument (a MOD sample slot) is rendered
once, for the **first** entry that names it, in this order:

1. `voice_map` entries with `root`;
2. `channel_instrument_map` entries with `root`;
3. rootless `channel_instrument_map` entries: rendered at C4, played at C1's rate.

A later entry naming the same slot plays that same sample, so its `root` must be written to play it in
tune (Sonic 1's Credits folds its voices onto 31 slots this way).

Two things add entries:

- **[Detune variants](../glossary.md#detune-variant)** (`fm_synthesis.detune_variants`): a copy of an
  instrument rendered at another FNUM offset, in a free slot ([../conversion/detune.md](../conversion/detune.md)).
- **The merged build** drops what it no longer plays and adds its
  [composites](../glossary.md#composite) ([Composites](#composites-several-voices-at-once),
  [../guide/amiga_builds.md](../guide/amiga_builds.md)).

---

## Pitch

### Where the sample is rendered: synth_root

A config entry maps a range of song notes to MOD notes: `root + (key − low)`
([../conversion/notes.md](../conversion/notes.md)).  For every note to be in tune, MOD note `root` must
sound **D**, the pitch the chip plays for `low` (its note plus pitch offset and transposition).

The sample does not have to be *rendered* at D.  [`synth_root`](../glossary.md#synth_root) is the pitch
actually rendered; [`synth_shift`](../glossary.md#synth_shift) = `synth_root − D` in semitones.  The
shift goes into the sample's rate, never into where notes land:

```
target_rate = round(amiga_clock / PERIOD_TABLE[root] × 2^(synth_shift / 12))
```

Example, Title Screen `voice_map` voice 1: `root: A1` (period 508).  At shift 0 the sample's rate is
3 546 895 / 508 = **6 982 Hz**; rendered an octave up (shift 12) it is **13 964 Hz**.  MOD note A1
still sounds D either way; the octave-up sample is twice the bytes.

`core.plan.synth_roots.resolve_synth_roots` derives `synth_root`; a config never needs to state it:

- the chip pitch the instrument's notes play most often (ties to the lower one),
- never below D (the pitch the entry's `root` note must sound), at most an octave above it (in the merged build, at most `merge_max_synth_shift`
  semitones, default 12).

Why the busiest note: envelopes run in real time on the chip, but in a MOD they stretch with the
playback rate.  Rendering at the commonest pitch keeps the most notes at the hardware's attack and
decay speed.  Why the octave cap: each octave of shift doubles the sample's size.

An instrument several entries share is rendered for the first one, its pitch chosen over all their
notes.  A stated `synth_root` names the rendering pitch anywhere in the range (the middle, say), with
the same shift rule.  When one entry's `low` plays at several chip pitches (two pitch offsets, or an
`smpsChangeTransposition`, under one source range), the most-played wins and the
`synth_root_ambiguous` warning names it: split the entry or use `range_space: chip`.

A higher `root` gives every note a higher rate and so more treble
([../conversion/notes.md](../conversion/notes.md)).

### Frequency registers

Note names are real pitches: `synth_root: A4` renders 440 Hz.  `note_to_fnum_block` writes the
registers the song's driver writes for the note, from its FM frequency table (`rules.fm_frequencies`;
MOD index i = table index i + 13; Sonic 1's octave runs fnum 644 for C to 1148 for A♯, with B as 606
in the block above: [../drivers/sonic_1.md § Note tables](../drivers/sonic_1.md#note-tables)).
That matters beyond pitch: rate scaling and DT1 read the key code (block and top fnum bits), so the
same pitch written as another fnum/block pair has another envelope speed and detune.

Off the table, or at another clock, `freq_to_fnum_block` stands in:

```
fnum = f × 144 × 2^(21 − block) / clock          A4 = 440 Hz at block 4 → fnum 1083
```

`freq_to_fnum_block` picks the block that keeps fnum in 512–1023, so for A4 it returns fnum 541, block 5
(the same pitch).  Sonic 1's table writes A4 as 1084 at block 4 (440.5 Hz).

Rendering at the right pitch also matters for timbre: a bass voice rendered octaves too high puts its
modulation sidebands above hearing and comes out thin or near-silent.

### Channel 3 special mode and the LFO

- A voice in **channel 3's special mode** (`SmpsVoice.fnum_offsets`: Streets of Rage's `$F7`) renders
  on channel 3, the only one with the mode: `$27` = `$40`, then each operator's frequency (the note's
  plus its offset) to its own registers (OP1 `$AD`/`$A9`, OP2 `$AE`/`$AA`, OP3 `$AC`/`$A8`, OP4 the
  channel's `$A6`/`$A2`).  In a composite it takes channel 3, the other layers the channels around it.
- A voice under the **hardware LFO** (`SmpsVoice.lfo`: Streets of Rage's `$FC`) writes `$22` (LFO
  rate) and its sensitivities into `$B4`.  Under AMS the LFO runs a quarter cycle before key-on, so the
  level swing starts at its middle; under FMS alone it starts with the note.

---

## Length and loops

How long each FM render is.  How a settled render is cut to a loop, and how looped notes end with
release slides: [../conversion/samples.md](../conversion/samples.md).

**Each render** is the sustain, a key-off, then `fm_synthesis.release_padding` seconds of release
(code 0.5, shipped 0.5), with its silent tail trimmed.

**Sustain.**  With `fm_synthesis.sustain_duration: auto` (shipped; code default `1.5` s) each
instrument is rendered for its own longest note, as the conversion places it
(`SustainPlanner.resolve`, `core/convert/sustain_plan.py`), capped at 10 s.  A number renders every
instrument that long.  Either way the sustain is capped to what fits `samples.max_sample_kb` at the
sample's rate, less the release (`core.mod.limits.max_sustain_secs`).  Where a note still outlasts its
sample, the `sustain_short` warning names the instrument, the seconds needed and the limit.

Worked number: at `max_sample_kb: 64` (65 534 bytes, less a 64-byte margin) and 13 964 Hz a sample
holds 65 470 / 13 964 = 4.7 s; less 0.5 s of release, 4.2 s of sustain.

**Cut where notes stop.**  An instrument whose `auto` sustain holds every note it plays is cut where
those notes stop being heard (`core.audio.loops.heard_padding`): at the sustain where notes end in
`C00`, after the release slide's 48 dB fall where they end in a slide, never past `release_padding`.
Exempt, keeping the whole padding: an instrument playing a channel's last note (it rings out), a
[mix](../glossary.md#mix) source, and any sample a stated sustain or a cap shortened.

**Sustain loops.**  With `samples.sustain_loops` on for the build (code `merged`, shipped `all`) the
renderer also renders a 4 s probe and looks for a [sustain loop](../glossary.md#sustain-loop) in it
([the rules](../conversion/samples.md#sustain-loops-samplessustain_loops)).  The voice's release rate is
measured on the probe's tail (`release_out`) for the notes' release slides.  A loop ending past where
the notes stop being heard is dropped.  A render with a pitch envelope or stepped levels never loops
(each pass differs).

---

## Level

### Rendered at the commonest level

Each instrument is rendered at its [render level](../glossary.md#render-level): the level most of its
notes play at.
`LevelPlanner.fm_render_levels` (`core/convert/level_plan.py`) picks the TL offset and pan most of its
notes carry: the same choice that sets its `sample_list` volume.  `program_voice(tl_offset=)` adds the
TL offset (`smpsHeaderFM` volume + `smpsAlterVol`) to the TLs the driver scales (the carriers), as
the driver's `SetVoice` does (`add.b`; the chip reads 7 bits).  Other notes get `Cxx`
([../conversion/levels.md](../conversion/levels.md)).

### Why the level cannot be applied afterwards

The chip sums a channel's carriers into a 9-bit accumulator (`ym3438.c`: each operator's output
`>> 5`, the sum clamped to −256…255).  One carrier at TL 0 fills it exactly; two or more overflow it,
by an amount that depends on the channel's volume.  Green Hill Zone's voice `$02` on FM1 (+18 TL)
clips no samples at its own level, and 36 % rendered at TL 0.  Rendering at the channel's TL clips
exactly as much as the hardware: flat-topped peaks on an algorithm 4–7 voice are correct where the
VGZ rip shows them too.

### Peak-normalised; balance from the volumes

Every sample is then scaled to its own full 8 bits.  Balance between instruments is the `sample_list`
volume's job, measured against a rip (`tools/vgm/vgm_compare.py --write-volumes`,
[../guide/verifying.md](../guide/verifying.md)).

---

## Composites: several voices at once

`render_layers` keys several voices together, layer i on YM2612 channel `channel + i` (up to six), and
lets the chip sum them.  A layer is `(voice, semitones above the note, FNUM detune, carrier TL
offset[, key-off seconds])`; a plain instrument is one layer at 0.

The merged build's FM [composites](../glossary.md#composite) (two song channels folded into one MOD
channel, [../guide/amiga_builds.md](../guide/amiga_builds.md)) use it: a
[follower](../glossary.md#follower)'s `smpsNoteFill` keys its layer off early; a mix's FM members render
together for a fixed length (`FmInstrument.render_secs`), never looped.

Two values go back to the converter (`peaks_out`):

- the first layer's peak alone, which the composite's volume is scaled from;
- the speaker gain: layers on opposite speakers never meet on the hardware, so each side is rendered
  on its own and the mono render is scaled to their L/R power.

---

## FM drums: a frame-by-frame render

Some drivers play drums as FM programs that change frequency and key state every frame (Golden Axe's,
Space Harrier II's).  `generate_fm_drums` renders each with `render_frames`: the voice, then each
frame's frequency word and key mask held for one frame.  A drum is rendered until the drum track's
next hit at most.  A drum with a PSG part (Space Harrier II's noise) has it rendered beside it and mixed
in at `fm_synthesis.drum_psg_db` (shipped −5.7 dB, measured on the rips).

---

## Resampling, conditioning, quantisation

**Resampling.**  The chip runs at `clock / 144` = 7 670 454 / 144 = 53 267 Hz.  `render_layers`
resamples to the sample's rate with `core.audio.resample`: polyphase Kaiser-windowed sinc, 512 phases,
`samples.resample_taps` taps (default 32) counted at the lower rate, so the filter keeps its shape at
any ratio: flat to 85 % of Nyquist, stopband over 70 dB down.

**Conditioning**, before a loop is searched for:

- treble shelf, `samples.treble_shelf_db` above `treble_shelf_hz` (shipped 0 dB = off, 2500 Hz), plus
  a merge group's own on top (`core.audio.pcm.high_shelf`);
- with `samples.dc_block` (code `false`, shipped `true`), a 5 Hz one-pole high-pass: the console's
  output is AC-coupled, and a lopsided voice otherwise clicks at every note-on and cut.

**Quantisation** (`full_scale_int8`): peak-normalised to the full 8 bits, then dithered by
`samples.dither` (or an entry's or merge group's `dither:`; a composite takes its
[primary](../glossary.md#primary)'s):

| `dither` | What it does | Best for |
|----------|--------------|----------|
| `shaped` (default) | TPDF dither with first-order error feedback: noise pushed toward Nyquist | Bright voices: the hiss hides under the treble; a fading tail hisses instead of stepping |
| `flat` | TPDF, unshaped | Mellow voices, with no treble to hide hiss |
| `off` | Plain rounding | Sounds that stay loud; a quiet tail steps |

The dither is seeded from the sample's length: a render is byte-identical from run to run.  The merged
build's mixer takes the unquantised renders (`raw_out`).

---

## Threads and the render cache

`generate_fm_samples` renders one instrument per thread, `fm_synthesis.threads` at a time (`normal` =
cores − 1, `max`, or a number).  Each thread owns its `OPN2`; ctypes releases the GIL during the batch
call; results are consumed in job order, so the output never depends on the thread count.

With `samples.render_cache` (shipped `output/cache`; code default off), every chip render is kept on
disk ([render cache](../glossary.md#render-cache)) under a hash of its inputs and of the code it ran through (`core/render_cache.py`: the library,
the FM device and renderer, resampler, PCM helpers, driver tables).  A PSG change keeps the FM renders.
Shelf, loops and quantisation still run on a cached render.

---

## Inside the emulator

Nuked-OPN2 (`3rdparty/nuked-opn2/ym3438.c`) is cycle-accurate: `OPN2_Clock` advances one internal
clock.  `OPN2(mode=)` picks the chip, kept across resets; the converter passes `fm_synthesis.mode`:

| Mode | Chip |
|------|------|
| `ym2612` (shipped, default) | Mega Drive 1/2's YM2612: its DAC's sign bias and ×3 output level |
| `ym3438` | The later YM3438 (no DAC bias) |

**One output sample = 24 clocks.**  The chip time-multiplexes its six channels: in YM2612 mode the
outputs carry audio ×3 on six of the 24 clocks (`(cycles & 3) == 3`) and a sign-only ±3 on the other
18.  The batch helpers (`ym3438_batch.c`) sum all 24 values and subtract the mode's DC (72 for
YM2612, 0 for YM3438), so silence is 0.  Taking one clock's value instead captures the bias, not the
audio.  `render_mono` folds L and R to `(L + R) // 2` in C.

**Register writes.**  `write_reg(addr, data, bank)`: address to port `bank*2`, 24 clocks, data to port
`bank*2+1`, 24 clocks: two samples of chip time per write, discarded unless captured
(`begin_capture`: the frame-by-frame drum render and `sonic2wav` keep them,
[../guide/sound_effects.md](../guide/sound_effects.md)).  Bank 0 holds channels 0–2, bank 1 channels
3–5, each at `channel % 3`.

**Voice.**  `program_voice` writes `$B0` (feedback/algorithm), `$B4` = `$C0` (both speakers) plus the
LFO sensitivities when the voice has an LFO (and `$22`, the LFO rate), then per operator DT/MUL, TL,
KS/AR, AM/D1R, D2R, D1L/RR and SSG-EG (0): 30 writes, 31 with the LFO.  SMPS stores operators in the
reverse of register order (`SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)`,
[../drivers/sonic_1.md](../drivers/sonic_1.md)).

**Key-on, `$28`.**  Bits 7–4 the operator mask (OP4 OP3 OP2 OP1), bits 2–0 the channel (0–2 → 0–2,
3–5 → 4–6).  `key_on` sends mask `0xF`, `key_off` mask 0.

---

## Module API

### `core/chips/ym2612/wrapper.py`

```python
OPN2(mode="ym2612")                  # loads (building if needed) the library and resets the chip
opn2.reset(mode=None)                # None keeps the instance's mode
opn2.write_reg(addr, data, bank=0)
opn2.key_on(channel, operators=0xF)
opn2.key_off(channel)
opn2.render_samples(n_samples) -> list[tuple[int, int]]   # (L, R) pairs, DC removed
opn2.render_mono(n_samples) -> array('i')                 # the same, (L + R) // 2
opn2.begin_capture() / end_capture() / take_capture()     # keep the write flushes' audio
OPN2.NATIVE_RATE                     # 53 267;  output_rate(clock_rate) for another clock
```

### `core/synth/fm_voice.py`

```python
program_voice(opn2, voice, channel, tl_offset=0)   # registers only: no frequency, no key-on
```

### `core/synth/fm_render.py`

```python
render_layers(layers, mod_note_index, sustain_secs=1.5, release_secs=0.5, target_rate=None,
              opn2=None, channel=0, clock_rate=7670454, taps=32, *, fm_frequencies,
              envelopes=(), frame_hz=60.0, octave_wrap=None, levels=()) -> (array('i'), rate)
render_note(voice, mod_note_index, sustain_secs=1.5, release_secs=0.5, target_rate=None,
            opn2=None, channel=0, clock_rate=7670454, tl_offset=0, *, fm_frequencies)
            -> (bytes, rate)                          # int8, peak-normalised
render_note_raw(...same as render_note...) -> (array('i'), rate)
render_frames(voice, tl_offset, frames, frame_hz, tail_secs=0.0, target_rate=None, opn2=None,
              clock_rate=7670454, taps=32) -> (array('i'), rate)     # a driver's frame-by-frame track
note_to_fnum_block(mod_note_index, clock_rate=7670454, *, fm_frequencies) -> (fnum, block)
freq_to_fnum_block(freq, clock_rate=7670454) -> (fnum, block)     # fnum in 512–1023 where possible
detuned_fnum_block(fnum, block, fnum_offset, wrap=None) -> (fnum, block)   # smpsAlterNote on the word
note_to_freq(mod_note_index) -> float     # 440 × 2^((idx − 45) / 12); idx 0 = C1
```

`mod_note_index` counts from C1 = 0.  `fm_frequencies` is the song's driver table
(`song.rules.fm_frequencies`).  `envelopes`: a pitch envelope per layer (frequency-word offset a
frame); `levels`: TL changes per frame (a baked volume envelope).  `target_rate=None` keeps the native
rate.  A passed `opn2` is reset (keeping its mode); none creates one.

### `core/synth/fm_samples.py`

```python
generate_fm_samples(song, conv, synth, verbose=False, tl_offsets=None, peaks_out=None,
                    raw_out=None, loops=False, loops_out=None, release_out=None,
                    cache_out=None, extra=(), pitches_out=None) -> dict[int, tuple[bytes, int]]
generate_fm_drums(drums, synth, frame_hz, ring_secs, cache_out=None, psg_clock=3579545)
                  -> dict[int, tuple[bytes, int]]
```

`generate_fm_samples` returns `{instrument: (int8 PCM, rate)}` for every catalogue instrument plus
`extra` (a mix's FM layers, under ids that are no slot).  `conv` is the `ConversionPlan`;
`synth.sustain_duration` must already be resolved (`SustainPlanner.resolve`).  `tl_offsets`:
`{instrument: track volume}` to render at.  The `*_out` dicts receive the peaks and speaker gain, the
unquantised renders, the loops, the release rates, the cache hits and each render's pitch in Hz.

---

## Common mistakes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Silence or near-silence on every FM instrument | No gcc / MSVC on PATH and no `prebuilt/` library for the platform | Install a compiler; `python tools/chips/validate_ym2612.py` builds and tests the library |
| FM instruments come from `.raw` files, not the chip | `fm_synthesis.enabled: false` | Set it `true` in `configs/settings.yaml` |
| One instrument thin or near-silent | A stated `synth_root` octaves away from the chip pitch: rendered there and stretched | Delete it; the derived one is the chip's |
| "Overdriven guitar" distortion | Operator order wrong | `SMPS_OP_TO_REG_OFFSET` must stay `(0x0C, 0x04, 0x08, 0x00)` ([../drivers/sonic_1.md](../drivers/sonic_1.md)) |
| Flat-topped peaks | Two or more carriers overflow the accumulator at the channel's level: the hardware does the same ([Level](#level)) | None needed if the rip shows them; a single-carrier voice cannot clip |
| Notes out of tune | `root` anchors `low` on the wrong MOD note, or one range plays at several chip pitches (`synth_root_ambiguous`) | Fix `root` ([../conversion/notes.md](../conversion/notes.md)); split the entry or use `range_space: chip`.  `synth_root` cannot cause it: its shift is in the rate |
| A note goes silent before its end (`sustain_short`) | The sample is shorter than the note | `sustain_duration: auto`; sustain loops on; at the sample limit, a lower `root` or `samples.max_sample_kb: 128` |
| A range plays another range's sample | Two entries share a `mod_instrument`; the first one rendered wins | Give the range its own slot |
| Instrument levels inconsistent | Every sample is peak-normalised; balance is the `sample_list` volumes' | `python tools/vgm/vgm_compare.py <config> <vgz> --write-volumes`, re-convert, re-run |
