# YM2612 FM Synthesis

How sonic2mod renders each FM instrument as an 8-bit MOD sample on Nuked-OPN2.

Related: `docs/pipeline.md` (how notes are placed and played), `docs/smps_driver.md` (voice
format), `docs/yaml_config.md` (every config and settings key), `docs/psg_synthesis.md` (the
SN76489 counterpart).

---

## Overview

```
catalogue entry → render_layers (chip, then resampled) → shelf, DC block → loop or cut → int8 (peak-normalised, dithered)
```

| File | Role |
|------|------|
| `ym2612/build.py`, `ym2612/wrapper.py` | Compile `ym3438.c` + `ym3438_batch.c` (`core.cbuild`); ctypes `OPN2` class |
| `ym2612/voice.py` | `SmpsVoice` → YM2612 registers (`program_voice`) |
| `ym2612/renderer.py` | Voices + pitch → PCM (`render_layers`, `render_note`) |
| `ym2612/sample_generator.py` | The song's instrument catalogue → `{inst: (pcm, rate)}` (`generate_fm_samples`) |
| `core/convert/smps2mod.py` | Calls the generator (handed in by `convert.py`) and installs the samples |

---

## Quick Start

`fm_synthesis.enabled` is `true` in the shipped `configs/settings.yaml` (the code's default is
`false`: samples then come from `sample_list` files).  The first render compiles the DLL, so
gcc or MSVC must be on PATH.

```bash
python ym2612/validate.py           # A4 test tone → output/validate_test.raw; checks the C helpers
python ym2612/voice.py              # Title Screen voice 0, 100 ms → prints the peak
python ym2612/renderer.py           # voice 1 at A3 → output/renderer_test.raw
python ym2612/sample_generator.py   # voice 1, root A3, through the generator → output/sample_gen_test.raw
python convert.py configs/01_title_screen.yaml
```

Audacity: File > Import > Raw Data, signed 16-bit PCM, little-endian, mono, at the rate each
script prints.

Settings (`fm_synthesis:`, `samples:`, `amiga_clock`): `docs/yaml_config.md` § settings.yaml.

---

## How a note becomes a sample

### The catalogue: what is rendered

`core.plan.instruments.fm_catalogue` lists every FM instrument and the entry it is rendered
for; the generator and the sustain planner both read it.  An instrument is rendered once, for
the **first** entry that names its slot, in this order:

1. `voice_map` entries with `root` (voices the song defines);
2. `channel_instrument_map` entries with `root`;
3. rootless `channel_instrument_map` entries: rendered at C4 and played at C1's rate.

A later entry naming the same slot plays that sample (Credits folds its voices onto 31 slots),
so its `root` must be written to play it in tune.  Detune variants
(`fm_synthesis.detune_variants`, `docs/pipeline.md` § Detune variants) add a copy of an
instrument in a free slot at another FNUM offset; the merged build drops what it no longer
plays and adds its chip composites (§ Layers).

### Pitch: synth_root, synth_shift, target_rate

A note's MOD placement is `root + (key − low)` (`docs/pipeline.md` § `voice_map` routing).  The
sample must sound the chip's pitch at every one of those notes:

- **D** — the pitch the chip plays for the entry's `low` (its byte plus the pitch offset and
  every `smpsChangeTransposition`).  A sample rendered at D and played at `root`'s rate puts
  every note in tune.
- **`synth_root`** — the pitch actually rendered.  `core.plan.synth_roots.resolve_synth_roots`
  derives it: the chip pitch the instrument's notes play most often (ties to the lower), at
  most an octave above D (`merge_max_synth_shift` lowers the cap in the merged build).
  Envelopes run in real time on the chip but stretch with playback rate in a MOD, so rendering
  at the busiest note keeps the most notes at the hardware's attack and decay speed.
- **`synth_shift`** = `synth_root − D`.  It goes into the sample's rate, never the placement:

```
target_rate = round(amiga_clock / PERIOD_TABLE[root] × 2^(synth_shift / 12))
```

MOD note `root` still sounds D, and every note keeps its place, playback rate and bandwidth;
the cost is 2^(synth_shift/12) times the bytes, hence the octave cap.  (Placing notes lower
instead halved the Chaos Emerald lead's rate to 4 kHz and lost its treble.)  A higher `root`
gives every note a higher rate and more bandwidth (`docs/pipeline.md` § `root`
placement).

An instrument several entries share is rendered for the first; its pitch is chosen over all
their notes.  A config never needs to state `synth_root`; one that does names the rendering
pitch anywhere in the range (the middle, say, so the sample stretches at most half the range
each way), and the same shift rule applies.  When an entry's `low` plays at several chip
pitches (two pitch offsets, or an `smpsChangeTransposition`, under one source range), the
most-played wins and `synth_root_ambiguous` warns: split the entry or use `range_space: chip`.

### Frequency registers

Pitch names are real pitches: `synth_root: A4` renders 440 Hz, and an SMPS FM label at pitch
offset 0 is the chip's note (the driver's table puts `nC0` at 16.35 Hz).  `note_to_fnum_block`
writes the registers the song's driver writes for the note (its `rules.fm_frequencies`, MOD index
i = table index i + 13; Sonic 1's: fnum 644–1216, the block from the octave).  Rate scaling and
DT1 read the key code (block and the fnum's top bits), so the same pitch written as another
fnum/block pair has another envelope and detune.  Off the table, or at another clock,
`freq_to_fnum_block` stands in: `fnum = f × 144 × 2^(21 − block) / clock` (A4 → 1083, block 4).

Rendering at the right pitch matters for timbre: a bass voice rendered octaves too high puts
its modulation sidebands out of the audible range and comes out thin or near-silent.

A voice in channel 3's special mode (`SmpsVoice.fnum_offsets`: Streets of Rage's `$F7`) renders on
channel 3, the only one with the mode: `$27` = `$40`, then each operator's word (the note's plus its
offset) to its own registers (OP1 `$AD`/`$A9`, OP2 `$AE`/`$AA`, OP3 `$AC`/`$A8`, OP4 the channel's
`$A6`/`$A2`).  In a composite it takes channel 3 and the other layers the channels around it.

A voice under the hardware LFO (`SmpsVoice.lfo`: Streets of Rage's `$FC`) writes `$22` (the chip's
LFO frequency) and its sensitivities into B4.  Under AMS the LFO runs a quarter cycle before
key-on (its level swing at the middle); under FMS alone it starts with the note.

### Length: `sustain_duration: auto`

A sample without a sustain loop that a note outlasts goes silent.  With `auto`,
`SustainPlanner.resolve` (`core/convert/sustain_plan.py`) renders each instrument for its own
longest **ring**, measured by `_needs` with the same `DriverState` walk as the conversion:

- A ring is a note plus the `smpsNoAttack` rests after it (no `C00` is written for those).  A
  plain rest or the next note-on ends it.  An `smpsNoAttack` *note* continues it only where
  `legato` writes it as `3FF`; under `retrigger` (the default) it starts a ring of its own.
  In the merged build a ring also ends at the next note-on on its column, a pooled or spliced
  note included, and at a note of the channel that is folded elsewhere.
- Its ends are where the row grid can put them (`_mod_span`): exact on a row, up to half a row
  and a driver tick out between rows, the next row for a ring that ends inside its own.
- Its length is MOD time, summed over the tempo segments, after `smpsSetTempoDiv` re-timing.
- It is scaled to the sample's own rate: root period / note period (the first entry's root),
  divided by 2^(synth_shift/12), times 2^(finetune/96) for a positive `sample_list` finetune.

Each instrument's need is capped at 10 s (`sustain_by_instrument`; the largest becomes
`sustain_duration` for anything unmeasured).  A stated number renders every instrument that
long.  The generator also caps each sustain to what fits `samples.max_sample_kb` at the
sample's rate, less the release (`core.mod.limits.max_sustain_secs`).  Where a note still
outlasts its sample, `sustain_short` names the instrument, the seconds needed and the limit
(the setting, the 10 s cap, or the sample limit — a lower `root` halves the bytes per second
per octave); a looped instrument never warns.

### Where a sample ends

Every render is the sustain, a key-off, then `release_padding` seconds of release, with its
silent tail trimmed.  An instrument whose `auto` sustain holds every note it plays
(`exact_sustain`) is cut where its notes stop being heard (`core.audio.loops.heard_padding`):
at the sustain where notes end in `C00`, or after the release slide's 48 dB fall where they
end in a slide (`slide_ends`), never past `release_padding`.  Exempt, and keeping the whole
padding: an instrument playing a channel's last note (it rings out), a mix source (the other
chip's pass measures its rings), and any sample a stated sustain or a cap shortened.

### Sustain loops

With `samples.sustain_loops` on for the build (code default `merged`, shipped `all`), the
renderer probes 4 s (`PROBE_SECS`, within the sample limit); a voice whose envelope settles is
cut where it settles plus one crossfaded loop (at most 1.2 s), and its notes end in release
slides at the rate measured on the probe's tail (`release_out`) instead of `C00`.  A loop that
would end past the plain render, or past where the notes stop being heard, is dropped.  A voice
under the hardware LFO loops on whole LFO cycles, its envelope measured over a cycle.  Rules,
`loop_drift_db` and `loop_decay`: `docs/pipeline.md` § Sample length, sustain loops and release slides.

### Level: render level and the channel accumulator

Each instrument is rendered at the level most of its notes play at.
`LevelPlanner.fm_render_levels` picks the (TL offset, pan) most of its notes carry — the same
choice that sets its baked `sample_list` volume — and `program_voice(tl_offset=)` adds the TL
offset (`smpsHeaderFM` volume + `smpsAlterVol`) to the carriers as `SetVoice` does (`add.b`,
the chip reads 7 bits).  Modulators are untouched; other notes get `Cxx` (`docs/pipeline.md`
§ Levels).

The level cannot be applied afterwards.  The chip sums a channel's carriers into a 9-bit
accumulator (`OPN2_ChGenerate`: each operator's output `>> 5`, the sum clamped to −256…255).
One carrier at TL 0 fills it exactly; two or more overflow it, by an amount that depends on the
channel's volume.  GHZ voice $02 on FM1 (+18 TL) clips no samples on the hardware and 36 %
rendered at TL 0.  Rendering at the channel's own TL clips exactly as much as the hardware, so
flat-topped peaks on an algorithm 4–7 voice are correct where the VGZ shows them too.

### Layers: chip composites

`render_layers` keys several voices together, layer i on YM2612 channel `channel + i` (up to
six), and lets the chip sum them.  A layer is `(voice, semitones above the note, FNUM detune,
carrier TL offset[, key-off seconds])`; a plain instrument is one layer at 0.  The merged
build's FM composites (`docs/pipeline.md` § The merged build) use it: a follower's
`smpsNoteFill` keys its layer off early, and a mix's FM members render together
(`FmInstrument.render_secs`: a fixed length, never looped).  Two values go back to the
converter (`peaks_out`): the first layer's peak alone, which a composite's volume is scaled
from, and the speaker gain — layers on opposite speakers never meet on the hardware, so each
side is rendered on its own and the mono render is scaled to their L/R power.

### Resampling

The chip runs at `clock / 144` (53 267 Hz).  `render_layers` resamples to the sample's rate
with `core.audio.resample`: polyphase Kaiser-windowed sinc, 512 phases, `samples.resample_taps`
taps (default 32) counted at the lower rate so the filter keeps its shape at any ratio, flat to
85 % of Nyquist with the stopband over 70 dB down.

### Conditioning and quantisation

Before a loop is searched for, each render gets the treble shelf (`samples.treble_shelf_db`,
a merge group's on top; `core.audio.pcm.high_shelf`, 0 dB = off) and, with `samples.dc_block`,
a 5 Hz one-pole high-pass (`dc_block`): the console's output is AC-coupled, and a lopsided
voice otherwise clicks at every note-on and cut.

Every sample is then peak-normalised to its own full 8 bits (`full_scale_int8` → `to_int8`):
balance between instruments is the `sample_list` volume's job, measured against the VGZ
(`tools/vgm_compare.py --write-volumes`).  `dither` (`samples.dither`, or an entry's or merge
group's `dither:`; a composite takes its primary's):

- `shaped` — TPDF dither with first-order error feedback: the noise rises toward Nyquist,
  under a bright voice's treble; a fading tail turns to hiss instead of stepping.
- `flat` — TPDF, unshaped: less hiss on a mellow voice, which has no treble to hide it.
- `off` — plain rounding: least noise on a sound that stays loud; a quiet tail steps.

The dither sequence is seeded from the sample's length, so a render is byte-identical from
run to run.  The merged build's mixer takes the unquantised renders (`raw_out`).

### Threads and the render cache

`generate_fm_samples` renders one instrument per thread, `fm_synthesis.threads` at a time
(`SynthesisSettings.worker_threads`: `normal` = cores − 1, `max`, or a number).  Each thread
owns its `OPN2`; ctypes releases the GIL for the batch call; results are consumed in job
order, so the output never depends on the thread count.  With `samples.render_cache`, every
chip render is kept on disk under a hash of its inputs, in a directory per hash of the DLL and
the Python it runs through (`core/render_cache.py`); shelf, loops and quantising still run.

---

## OPN2 Emulator Internals

Nuked-OPN2 (`reference/Nuked-OPN2/ym3438.c`) is cycle-accurate; `OPN2_Clock` advances one
internal clock.  `OPN2(mode=)` selects the chip type, kept across resets: `ym2612` (MD1/MD2
DAC: sign bias, ×3 level) or `ym3438`.  `convert.py` synthesises FM only in `ym2612` mode.

**One sample = 24 clocks.**  The chip time-multiplexes its six channels; in YM2612 mode
`mol`/`mor` carry audio × 3 at the six output clocks (`(cycles & 3) == 3`) and a sign-only ±3 at
the other 18.  The batch helpers (`ym3438_batch.c`) sum all 24 values and subtract the mode's
DC (72 in YM2612 mode, 0 in YM3438), so silence is 0.  Taking one clock's value instead
captures the bias, not the audio.  `render_mono` folds L and R to `(L + R) // 2` in C.

**Register writes.**  `write_reg(addr, data, bank)`: address to port `bank*2`, 24 clocks, data
to port `bank*2+1`, 24 clocks — two samples of chip time per write, discarded (`sfx/render.py`
keeps them with `begin_capture`).  Bank 0 holds channels 0–2, bank 1 channels 3–5,
each at `channel % 3`.

**Voice.**  `program_voice` writes 30 registers: `0xB0` (feedback/algorithm), `0xB4` = `0xC0`
(both speakers, AMS = PMS = 0), and per operator DT/MUL, TL, KS/AR, AM/D1R, D2R, D1L/RR and
SSG-EG (0).  The SMPS operator order is reversed against the registers
(`SMPS_OP_TO_REG_OFFSET`, `docs/smps_driver.md` § YM2612 register mapping).

**Key-on, `0x28`.**  Bits 7–4 the operator mask (OP4 OP3 OP2 OP1), bits 2–0 the channel
(0–2 → 0–2, 3–5 → 4–6).  `key_on` sends mask `0xF`, `key_off` mask `0`.

---

## Module API

### `ym2612/wrapper.py`

```python
OPN2(mode="ym2612")                 # loads (building if needed) the DLL and resets the chip
opn2.reset(mode=None)               # None keeps the instance's mode
opn2.write_reg(addr, data, bank=0)
opn2.key_on(channel, operators=0xF)
opn2.key_off(channel)
opn2.render_samples(n) -> list[tuple[int, int]]   # n (L, R) pairs
opn2.render_mono(n) -> array('i')                 # the same, (L + R) // 2
opn2.begin_capture() / end_capture() / take_capture()   # keep the write flushes' audio
OPN2.NATIVE_RATE                    # 53 267; output_rate(clock_rate) for another clock
```

### `ym2612/voice.py`

```python
program_voice(opn2, voice, channel, tl_offset=0)   # registers only: no frequency, no key-on
```

### `ym2612/renderer.py`

```python
render_layers(layers, mod_note_index, sustain_secs=1.5, release_secs=0.5, target_rate=None,
              opn2=None, channel=0, clock_rate=7670454, taps=32) -> (array('i'), rate)
render_note(voice, mod_note_index, sustain_secs=1.5, release_secs=0.5, target_rate=None,
            opn2=None, channel=0, clock_rate=7670454, tl_offset=0) -> (bytes, rate)  # int8, peak-normalised
render_note_raw(...same...) -> (array('i'), rate)
note_to_fnum_block(mod_note_index, clock_rate=7670454) -> (fnum, block)   # the driver's table
freq_to_fnum_block(freq, clock_rate=7670454) -> (fnum, block)             # fnum in 512–1023 where possible
detuned_fnum_block(fnum, block, fnum_offset) -> (fnum, block)             # smpsAlterNote on the whole word
note_to_freq(mod_note_index) -> float     # 440 × 2^((idx − 45) / 12); idx 0 = C1
```

`mod_note_index` counts from C1 = 0.  `target_rate=None` keeps the native rate.  A passed
`opn2` is reset (keeping its mode); none creates one.

### `ym2612/sample_generator.py`

```python
generate_fm_samples(song, config, synth, verbose=False, tl_offsets=None, peaks_out=None,
                    raw_out=None, loops=False, loops_out=None, release_out=None,
                    cache_out=None, extra=()) -> dict[int, tuple[bytes, int]]
```

Returns `{instrument: (int8 PCM, rate)}` for every catalogue instrument plus `extra` (a mix's
FM layers, under ids that are no slot).  `synth.sustain_duration` must already be resolved
(`SustainPlanner.resolve`).  `tl_offsets`: `{instrument: track volume}` to render at.  The
`*_out` dicts receive the peaks and speaker gain, the unquantised renders, the loops, the
release rates and the cache hits.

---

## Common Mistakes

### Silence or near-silence

- No gcc / MSVC on PATH, so `ym2612/ym3438.dll` cannot be built (`python ym2612/validate.py`
  builds and tests it).
- `fm_synthesis.enabled: false`, or `mode: ym3438` — either loads `sample_list` files instead.
- A stated `synth_root` octaves away from the chip pitch: the sample is rendered there and
  stretched.  Delete it; the derived one is the chip's.

### "Overdriven guitar" distortion

The operator order is wrong: `SMPS_OP_TO_REG_OFFSET` must stay `(0x0C, 0x04, 0x08, 0x00)`
(`docs/smps_driver.md` § YM2612 register mapping).

### Flat-topped peaks

Two or more carriers overflowing the accumulator at the channel's level — the hardware does
the same (§ Level).  A single-carrier voice cannot clip.

### Wrong pitch

`root` anchors `low` on the wrong MOD note (`docs/pipeline.md` § `voice_map` routing), or a
range is played at several chip pitches (`synth_root_ambiguous`: split it or use
`range_space: chip`).  `synth_root` cannot cause it: its shift is carried by the rate.

### A note goes silent before its end

`sustain_short`: the sample is shorter than the note.  Use `auto`, turn on sustain loops, or,
at the sample limit, lower `root` or raise `samples.max_sample_kb` to 128.

### A range plays another range's sample

Two entries share a `mod_instrument`; the first one rendered wins.  Give a range that needs its
own sample its own slot.

### Instrument levels inconsistent

Every sample is peak-normalised, so the balance is the `sample_list` volumes'.  Measure them:
`python tools/vgm_compare.py <config> <vgz> --write-volumes`, re-convert, re-run.
