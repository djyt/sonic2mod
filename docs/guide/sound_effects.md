# Sound effects (`sonic2wav`)

`sonic2wav.py` renders Sonic 1's 49 sound effects (the `SndXX` asm files, or a ROM's) to audio files, as
`configs/sfx.yaml` says: WAV or headerless raw, 8 or 16 bits, its rate and folder.  Shipped: 16-bit stereo WAV;
its `amiga` and `mixer` variants write 8-bit mono samples for an Amiga game engine.  This page is for anyone
producing or tuning those files.

Unlike the MOD path, which flattens events onto a tracker's row grid, this runs a **tick-accurate copy of the
Sonic 1 sound driver** against the YM2612 (Nuked-OPN2) and SN76489 emulators.  Effects are built almost
entirely from per-frame pitch sweeps and volume ramps that a row grid would destroy.  What the driver does:
[sonic_1.md](../drivers/sonic_1.md).

On this page:

1. [Running it](#running-it)
2. [`configs/sfx.yaml`](#configssfxyaml)
3. [The manifest](#the-manifest)
4. [Mix levels](#mix-levels)
5. [8-bit Amiga export](#8-bit-amiga-export)
6. [Known deviations from the hardware](#known-deviations-from-the-hardware)
7. [How `sfx/` copies the driver](#how-sfx-copies-the-driver)

---

## Running it

```bash
python sonic2wav.py --all                         # all 49 → output/sonic_1/sfx/A0_Jump.wav ...
python sonic2wav.py --all --variant amiga         # 8-bit raw + manifest.yaml → output/sonic_1/sfx/amiga/
python sonic2wav.py --all --variant mixer         # 8-bit WAV, levels baked for a software mixer → sfx/mixer/
python sonic2wav.py --rom input/roms/sonic_rev01.bin   # the same 49 read from the ROM, its data fixes on
python sonic2wav.py "reference/smps_drivers/sonic_1/sfx/SndB5 - Ring.asm"
python sonic2wav.py --all --dry-run               # render and report, write nothing
python sonic2wav.py --all --config my_sfx.yaml --out output/tmp   # another settings file, another folder
python sfx/validate.py                            # self-check: tables, resampler, 8-bit chain, ticks, panning
```

| Flag | Effect |
|---|---|
| `FILE ...` | Render these `.asm` files |
| `-a`, `--all` | Every `Snd*.asm` in sfx.yaml's `input` folder |
| `--rom FILE` | Every SFX in a Sonic 1 ROM, with its known data fixes |
| `-c`, `--config PATH` | Settings file (default `configs/sfx.yaml`) |
| `--variant NAME` | Lay the file's `variants: {NAME: ...}` block over the keys beside it |
| `-o`, `--out DIR` | Output folder over `output.directory` |
| `--dry-run` | Render and report, write nothing |

Files are named by sound id and name: `B5_Ring.wav`, `CE_Ring_Left_Speaker.wav`.  The report lists each effect's
channels, ticks, length, rate, peak and size.

---

## `configs/sfx.yaml`

Every key the SFX path reads.  The code's defaults are the shipped base values.  The chip clocks,
`amiga_clock`, `psg_synthesis.oversample` and `samples.resample_taps` come from `configs/settings.yaml`
([settings.md](settings.md)).  A key it does not know is an error.

### `input` and `render:`

| Key | Default | What it does |
|---|---|---|
| `input` | `reference/smps_drivers/sonic_1/sfx` | The folder `--all` renders |
| `render.fps` | `60` | V-int rate, one SFX tick a frame: 60 NTSC, 50 PAL |
| `render.tail_seconds` | `1.0` | Rendered after the last track stops, for the release |
| `render.max_seconds` | `10.0` | Safety cap (the longest, SndBF Get Continue, plays 4.3 s) |
| `render.psg_gain` | `0.31` | PSG against FM, each chip at its own full scale ([Mix levels](#mix-levels)) |
| `render.psg_past_table` | `extend` | A PSG note past the driver's table: `extend` (the twelve-tone sequence continued) or `clamp` (the table's top) |

### `output:`

| Key | Default | What it does |
|---|---|---|
| `directory` | `output/sonic_1/sfx` | Where files go; `--out` overrides it |
| `format` | `wav` | `wav`: RIFF (8-bit unsigned, as WAV stores it).  `raw`: headerless signed PCM (16-bit big-endian, the 68000's order) and a `manifest.yaml` |
| `bits` | `16` | `8` or `16`.  16 bits is rounded; 8 bits takes `dither` |
| `rate` | `44100` | Hz; `native` (the YM2612's 53267 Hz, no resampling); `auto` (per effect, [Rate selection](#rate-selection); needs `paula`) |
| `channels` | `stereo` | `stereo` or `mono` (the ring pair's panning lost) |
| `mono_fold` | `mean` | Mono: a panned effect's level against a centred one.  `mean`: (L+R)/2, 6 dB under (the Mega Drive's mono A/V out).  `power`: 3 dB under (what a stereo listener hears).  `loudest`: as loud as centred.  Sonic 1's panned effects are the rings |
| `level` | `global` | `global`: one gain for the whole set, the composed balance kept.  `normalise`: each effect at full scale; the manifest's `volume` restores the balance |
| `peak_dbfs` | `-0.3` | `global`: the loudest effect's peak (of `reference`'s, when given) |
| `reference` | `[]` | `global`: the sound ids whose loudest sets the gain (`[A0, B5]`), so a rare loud effect does not turn the common ones down.  `[]`, or none of them rendered: every effect |
| `over` | `clip` | `global`: an effect the gain puts past the rails.  `clip`: clipped (counted in `clipped`).  `limit`: turned down to just fit (`limited_db`) |
| `gain_db` | `0` | `global`: dB on each effect's composed level, before `over` |
| `dc_block` | `false` | 30 Hz one-pole high-pass |
| `dither` | `off` | 8-bit rounding: `shaped` (first order), `flat`, `off` (`core.audio.to_int8`, the converter's) |
| `paula` | `false` | Rates on ProTracker's period grid, a silent repeat word appended; the manifest gains `period`, `note`, `finetune` and the repeat.  Needs `bits: 8`, `channels: mono`, and `rate` in Hz or `auto` |
| `max_rate` | `28604` | `auto`'s ceiling (period 124); 16574 for an A500 |
| `trim_lead` | `false` | Start at the first sample that rounds to a step ([Lead and tail](#lead-and-tail)).  Plays a frame earlier than the Mega Drive |
| `tail_db` | `off` | Cut where the effect stays this many dB under its own peak (`-30`), the last frame faded |
| `transpose` | `0` | Semitones on every track's notes, rendered so ([Transpose](#transpose)) |
| `effects` | `{}` | One effect's own `trim_lead` / `tail_db` / `transpose` / `gain_db`, by sound id (`CC`, `$CC`, `0xCC`): `{CC: {tail_db: -24}}` |

`reference`, `over` and `gain_db` need `level: global`; `rate: auto` needs `paula: true`.

### Variants

A `variants:` block lays its keys over the ones beside it (`--variant NAME`); the shipped ones sit inside
`output:`.

| Variant | Keys | For |
|---|---|---|
| `amiga` | `raw`, 8-bit, 8287 Hz (C-2, period 428), mono, `normalise`, `dc_block`, `shaped` dither, `paula`, `trim_lead` → `output/sonic_1/sfx/amiga/` | One effect per Paula channel, its level set by the MOD volume |
| `mixer` | `wav`, 8-bit, 8287 Hz, mono, `mono_fold: power`, `level: global`, `peak_dbfs: 0`, `reference` (11 ids), `over: limit`, `dc_block`, `dither: off`, `paula`, `trim_lead`, `tail_db: -36`, `effects:` → `output/sonic_1/sfx/mixer/` | A software mixer summing voices at one volume (64) |

**Why `mixer` is set so:**

- **Each effect at its composed level**, so voices sum at one volume.  Rounded once at that level: re-scaling
  `amiga`'s samples would round twice.  No dither: at 8287 Hz the whole band is audible and dither at the rings'
  level is heard as hiss.
- **The gain is the common effects'**: `reference` lists the 11 the game plays (Jump, Lamppost, Death, Skid,
  Hit Spikes, Hit Boss, Ring, Break Item, Ring Loss, Spring, Switch); their loudest (Hit Boss, 1.31 full FM
  channels) sits at the rails.  A full FM channel then plays at −2.3 dBFS, the music's own scale (a full
  channel's note in the reference builds plays at −3 to +1 dBFS, median −2), so the Mega Drive's music / SFX
  balance carries over.  Set by the loudest of all 49 (Bumper, three FM channels) it would sit 7.8 dB under.
- **`over: limit`** turns the eight rarer effects past the rails down to fit: Bumper −7.5 dB, Collapse −4.6,
  Get Continue, Wall Smash, Drown Death −1.5, Rumbling, Fireball, Flamethrower −0.2.
- **Break Item (`$C1`) −2.5 dB** (`effects:` `gain_db`): its attack (FM5 + PSG3 noise, three frames near the
  rails) clipped the mix.
- Voices near the rails overflow an 8-bit sum when two overlap: the mixer should saturate.

---

## The manifest

`format: raw` writes `manifest.yaml` beside the files: what a raw file cannot hold.  At the top: `format`,
`clock`, a `note` on what the fields mean, `mask_bits`; then a `samples:` entry per effect:

| Field | What it holds |
|---|---|
| `file`, `rate`, `length` | The file, its rate, its size in bytes |
| `period`, `note`, `finetune` | `paula`: the ProTracker period playing `rate`, its note, 0 |
| `volume` | `normalise`: the composed level (1–64).  `global`: 64 |
| `repeat_offset`, `repeat_length` | `paula`: the trailing silent word |
| `channels` | The channels' types (`FM`, `PSG`) |
| `slots`, `mask` | The SFX track slots it takes (FM3 FM4 FM5 PSG1 PSG2 PSG3; PSG3 tone and noise are one), and as a 6-bit mask, bit 0 FM3.  An effect on a slot cuts the older one's track there (`sfx/slots.py`) |
| `peak` | The written peak in steps (127 / 32767 full scale) |
| `clipped` | Samples the gain pushed past the rails (`over: clip`) |
| `limited_db` | `over: limit`: how far the effect was turned down to fit (0 when it fits) |
| `transpose` | Semitones the effect was rendered from its own pitch; absent at 0 |
| `gain_db` | How far the effect was moved from its composed level; absent at 0 |

---

## Mix levels

One channel at full volume measures **YM2612 ±789** (the same for 1 and 4 carriers: the channel accumulator
clamps), **SN76489 tone ±4096**, noise ±2048.  Each chip is divided by its own full scale, then the PSG is
multiplied by `psg_gain` (eight effects use both chips).

`psg_gain: 0.31` (−10.2 dB) is VGMPlay's balance, the reference the converter's volumes are measured against:
one PSG tone at attenuation 0 against one FM carrier at TL 0, rendered by VGMPlay through the project's
`VGMPlay.ini`, peaks 10.2 dB under with the Nuked core (12.0 dB with GPGX and GENS).  The driver cannot say:
its levels are attenuations within each chip; the mix is the board's.  At 1.0, Jump (PSG1 at attenuation 0)
comes out 10 dB louder against the Ring (FM5) than VGMPlay plays the same register writes; at 0.31 they are
about equal, as on a Mega Drive's sound test.

The same balance holds beside the converted music: the music's PSG volumes were measured against VGMPlay too
(Green Hill: a full PSG tone about 11.5 dB under a full FM channel).

With `level: global` the gain is one factor across every file, not per-file normalising: the ring stays well
below the death jingle.  `reference` picks which effects' loudest sets it; `over: limit` turns down only the
ones past the rails.

---

## 8-bit Amiga export

Headerless **signed 8-bit mono** `.raw` files and a `manifest.yaml` with the rate, the note to trigger each at,
the MOD volume and the repeat points.  Eight bits wants nearly the opposite of the WAV path:

- **Per-sample peak normalising, the level restored by the MOD volume.**  Paula scales after its DAC, so the
  quantisation noise falls with the volume; storing a quiet effect quietly only discards bits.
- **DC removal first.**  The PSG-noise effects carry real offset (SndB8: a quarter of its peak), which wastes
  headroom and clicks on trigger.  A one-pole 30 Hz blocker, not mean subtraction: many effects open with a
  rest, which a mean would push off zero.
- **Volumes measured after DC removal and resampling**, on the audio that plays.
- **Shaped TPDF dither**: the long `smpsAlterVol` ramps turn granular when truncated.  Its random generator is
  seeded from the length, so runs are byte-identical.
- **One resampling stage**, chip rate straight to the target.

### Rate selection

With `rate: auto`, rates come from ProTracker's period table (`856 / 2^(n/12)`, C-1 to B-3), so each effect
plays at true pitch with finetune 0.  The lowest rate whose Nyquist holds 99 % of the effect's energy wins.  It
saves less than expected: Paula tops out near period 124 (≈ 28.6 kHz) and two thirds of the effects (square
waves, noise, bright FM) want that ceiling.  So filter quality matters more than rate: the resampler is a sharp
polyphase sinc.

The target machine matters more than the material.  An A500's fixed ≈ 4.4 kHz filter makes rates above
≈ 11 kHz largely academic (`max_rate: 16574`, about half the bytes); an A1200 hears them, and hears Paula's
zero-order-hold imaging too, so low rates sound grittier there.  Per-effect rates are a variant of their own,
or a copy of `sfx.yaml` given with `--config`.

### MOD mechanics

Lengths are padded even (a MOD stores words) and a silent word is appended, the manifest's `repeat_offset` /
`repeat_length` pointing at it: Paula loops the repeat region once a sample ends, and silence there stops a
one-shot buzzing.  Samples are mono: the ring pair's panning becomes a channel choice (`B5_Ring` right,
`CE_Ring_Left_Speaker` left).  A MOD holds 31 instruments, so the 49 never fit one module.

### Lead and tail

**The leading rest is the data's.**  19 of the 49 effects open on a rest, most `nRst, $01` (Spring, Roll,
Bumper, Shield, Giant Ring ...; Collapse `$02`, Signpost `$04`), most likely keying the channel off for a
frame while the voice uploads (an effect takes FM4 / FM5 from a music note that may still sound).  A sample
starts from silence anyway: `trim_lead` drops that frame (138 bytes at 8287 Hz; 1.6 KB over the 49), and the
effect sounds a frame earlier.

**Tails are the voices' own fades.**  44 of the 45 effect voices release every operator at `$0F`, so the long
tails are written: Spring's `smpsAlterVol $02` every two frames, 25 times, falls 37.5 dB over 50 frames.
`tail_db` cuts where the effect stays that far under its own peak and fades the last frame.  Over the mixer's
49 (8-bit, volume 32): −36 dB saves 7 KB, −30 dB 26 KB, −24 dB 63 KB of 416 KB.  The mixer variant cuts at
−36 dB, and its `effects:` entries cut some effects sooner (−12 to −28 dB, chosen by ear).

### Transpose

**Some effects live above a low rate's band.**  Roll (`$BE`) is algorithm 4 with feedback 7 on OP1 (a hiss)
into OP2 at ×4: its carrier sits at 4.4 kHz on `nCs6` and 6.3 kHz on `nG6`, the hiss up to 12 kHz.  Only 8 %
of its energy is under 8287 Hz's 4.14 kHz Nyquist, so the resampler rightly removes the rest and what is left
peaks at 50 of 127.  `transpose` adds semitones to every track's pitch byte before the driver plays it: the
voice, vibrato, fade and timing stay the data's and the notes move into the band (an octave down: 42 % of the
energy kept, peak 97).  The mixer variant plays Roll at −9 (by ear: peak 92 of 127).  A re-voicing for the
target, not the Mega Drive's pitch: the manifest's `transpose` says so.

---

## Known deviations from the hardware

Per the project rule, a driver bug is not reproduced: the render plays what was meant.

1. **The `SendVoiceTL` bug.**  Stock Sonic 1 reads the effect's voice pointer from `(a6)` instead of `(a5)`,
   so every `smpsAlterVol` in an effect (12 of the 49) uploads TLs from a wrong pointer.  `sonic2wav` does the
   disassembly's `FixBugs` version: those ramps are cleaner than the hardware's.
2. **PSG table overrun.**  SndB6 Spikes Move (`nG6`, index 79) and the unused SndA2 (`nCs6`, index 73) read
   past the 70-entry table into code; the twelve-tone sequence is continued from the last musical entry
   (`psg_past_table: clamp`: the table's top).  Index 69 (`nMaxPSG`) keeps its divider 1.
3. **FM table overrun.**  SndA8 SS Goal reaches index 96, one past the table, after 37 `smpsAlterPitch $02`
   steps; the block field saturates at 7 anyway, so whole octaves are dropped until the note fits, keeping the
   pitch class.
4. **PSG3's noise stops at `smpsStop`** (the `FixBugs` behaviour); stock leaves it ringing.

---

## How `sfx/` copies the driver

| Module | Role |
|---|---|
| `sfx/tables.py` | Re-exports Sonic 1's tables (`core/drivers/reference.py`: frequency tables, PSG envelopes) and the SMPS register and channel maps (`core/smps/driver_tables.py`) |
| `sfx/track.py` | `SfxTrack`: the `SMPS_Track` RAM struct, field for field |
| `sfx/chips.py` | Register writes mirroring `SetVoice`, `SendVoiceTL`, `FMUpdateFreq`, `PSGUpdateFreq` ... |
| `sfx/driver.py` | `SfxDriver`: the per-frame state machine |
| `sfx/render.py` | Frame loop, FM + PSG mix at 53267 Hz, tail detection |
| `sfx/resample.py` | Re-exports `core/audio/resample.py` (polyphase windowed sinc) |
| `sfx/config.py` | `configs/sfx.yaml` → `SfxConfig` (`RenderSettings`, `OutputSettings`) |
| `sfx/batch.py` | Discovery, naming (`sound_id`), rendering at the chip's rate |
| `sfx/export.py` | Renders → files: channels, DC, rate, level, 8 / 16-bit, `.wav` / `.raw`, the manifest |
| `sfx/amiga.py` | Paula's side: period grid, rate selection, the silent repeat word |
| `sfx/slots.py` | The SFX track slots an effect takes: the manifest's `slots` / `mask` |
| `sfx/wav.py` | WAV writer (stdlib `wave`), 8 or 16-bit |
| `sfx/validate.py` | Self-check |

### Easy to get wrong

- **The driver's tables, not equal temperament.**  The FM word is already the `$A4` / `$A0` pair: write
  `word >> 8`, then `word & 0xFF`, no fnum / block search.  Transposition wraps modulo 128; SndBC Teleport's
  "invalid" `$90` transpose lands on the same index as `$10`.
- **One tick per V-int.**  `TempoWait` holds only the music tracks, so the music's `fps × (m−1)/m` does not
  apply.  Every effect has `smpsHeaderTempoSFX $01`: 1 tick = 1/60 s.
- **Modulation halves its step count on arm only**; 22 of the 49 effects use `smpsModSet`.  The frequency is
  rewritten only when modulation steps: the driver skips the caller's write (`addq.w #4,sp`), so
  `_do_modulation()` returns a bool.
- **A duration byte with no note keeps the frequency**: SndA8 SS Goal re-keys after each `smpsAlterPitch` at the
  old pitch.  The parser marks these `SmpsNote.is_retrigger`.
- **`smpsNoAttack` skips the key-off and the re-arm**, so a tied note carries its PSG envelope and modulation
  sweep on.
- **PSG envelopes hold their last value** at `$80`.  `core/synth/psg_render.py` ramps to silence unless told to
  `hold`: right for a one-shot MOD sample, wrong here.
- **`SetVoice` and `SendVoiceTL` differ.**  `core/synth/fm_voice.py::program_voice` does `SetVoice`'s sum only
  and writes `$B4` centred, so `sfx/chips.py` mirrors both routines instead.
- **`OPN2.write_reg` costs two samples of chip time** and normally discards their audio; on a continuous
  timeline that pulls the FM out of step with the PSG.  `OPN2.begin_capture()` keeps them for the frame loop
  (off by default: the MOD path is unaffected).
- **One timeline.**  The SN76489's output rate is fixed at init, so it runs at `psg_synthesis.oversample` (8) ×
  the OPN2's 53267 Hz and its stream is resampled down to the FM's once, at the end.  At 1× the core's
  anti-aliasing is a box average and a square wave's aliases fold back (Jump at 8287 Hz: −24 dB, heard as
  crackle; −41 dB at 8×).  An effect with no PSG track skips the chip.
