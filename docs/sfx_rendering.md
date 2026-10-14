# SFX → WAV rendering (`sonic2wav`)

Renders the 49 Sonic 1 sound effects in `reference/smps_drivers/sonic_1/sfx/*.asm` (or a ROM's) as
`configs/sfx.yaml` says: WAV or raw, 8 or 16 bits, its rate and folder.  Shipped: 16-bit stereo WAV;
its `amiga` variant, Paula's 8-bit raw samples.

Unlike the MOD path, which flattens events onto a tracker row grid, this runs a **tick-accurate
software copy of the Sonic 1 sound driver** against the YM2612 (Nuked-OPN2) and SN76489
emulators: SFX are built almost entirely from per-frame vibrato sweeps and volume ramps that a row
grid destroys.  What the driver does is `docs/smps_driver.md`; this page is how `sfx/` copies it.

```bash
python sonic2wav.py --all                       # all 49 → output/sonic_1/sfx/
python sonic2wav.py --all --variant amiga       # 8-bit raw + manifest.yaml → output/sonic_1/sfx/amiga/
python sonic2wav.py --all --variant mixer       # the same, levels baked for a software mixer → sfx/mixer/
python sonic2wav.py --rom input/roms/sonic_rev01.bin   # the same 49 read from the ROM
python sonic2wav.py "reference/smps_drivers/sonic_1/sfx/SndB5 - Ring.asm"
python sonic2wav.py --all --dry-run             # parse + render, report, write nothing
python sonic2wav.py --all --config my_sfx.yaml --out output/tmp   # another settings file, folder
python sfx/validate.py                          # self-check
```

## `configs/sfx.yaml`

Every key the SFX path reads; the code's defaults are the shipped base values.  The chip clocks,
`amiga_clock` and `samples.resample_taps` are the converter's, from `settings.yaml`.  A
`variants:` block lays its keys over the ones beside it (`--variant NAME`); an unknown key is an
error.

| Key | Shipped | Meaning |
|---|---|---|
| `input` | `reference/smps_drivers/sonic_1/sfx` | The `SndXX` .asm folder `--all` renders |
| `render.fps` | `60` | V-int rate, one SFX tick a frame: 60 NTSC, 50 PAL |
| `render.tail_seconds` | `1.0` | Rendered after the last track stops |
| `render.max_seconds` | `10.0` | Safety cap |
| `render.psg_gain` | `0.31` | PSG against FM (§ Mix levels) |
| `render.psg_past_table` | `extend` | A PSG note past the table: `extend` or `clamp` (§ Known deviations) |
| `output.directory` | `output/sonic_1/sfx` | `--out` overrides it |
| `output.format` | `wav` | `wav`: RIFF (8-bit unsigned, as WAV stores it).  `raw`: headerless signed PCM, 16-bit big-endian (the 68000's order), and a `manifest.yaml` |
| `output.bits` | `16` | `8` or `16`.  16 bits is rounded; 8 bits takes `dither` |
| `output.rate` | `44100` | Hz; `native` (the YM2612's 53267 Hz, no resampling); `auto` (§ Rate selection; `paula` only) |
| `output.channels` | `stereo` | `stereo` or `mono` (the ring pair's panning lost) |
| `output.mono_fold` | `mean` | Mono: a panned effect against a centred one.  `mean`: (L+R)/2, a one-side effect 6 dB under (the Mega Drive's mono A/V out).  `power`: the stereo power kept, 3 dB under (a stereo listener).  `loudest`: as loud as centred.  A gain per effect; Sonic 1's panned ones are the rings |
| `output.level` | `global` | `global`: one gain for the set, the composed balance kept (`peak_dbfs` the loudest's peak).  `normalise`: each effect at full scale; the manifest's `volume` restores the balance |
| `output.peak_dbfs` | `-0.3` | `global`: the loudest effect's peak |
| `output.boost` | `1` | `global`: played at volume 64 / boost to the nearest whole one, baked × exactly 64 / that volume (1.45 → volume 44, × 1.4545).  Quiet effects gain log2(boost) bits; loud peaks clip, counted in the manifest's `clipped` |
| `output.dc_block` | `false` | 30 Hz one-pole high-pass |
| `output.dither` | `off` | 8-bit rounding: `shaped` (first order), `flat`, `off` (`core.audio.to_int8`, the converter's) |
| `output.paula` | `false` | Rates on ProTracker's period grid, a silent repeat word appended; the manifest gains `period`, `note`, `finetune`, the repeat.  Needs `bits: 8`, `channels: mono` |
| `output.max_rate` | `28604` | `auto`'s ceiling (period 124); 16574 for an A500 |

Variants:

- **`amiga`**: `raw`, 8-bit, 8287 Hz (C-2, period 428), mono, `normalise`, DC blocked, `shaped`
  dither, `paula` → `output/sonic_1/sfx/amiga/`.  For playing one effect per Paula channel.
- **`mixer`**: as `amiga` but `level: global`, `peak_dbfs: 0`, `mono_fold: power`, `boost: 2`
  (volume 32), `dither: off`, and `format: wav` for listening (`raw` for Aonic) →
  `output/sonic_1/sfx/mixer/`.  Dither at the rings' level (±46 steps) is heard as hiss: at 8287 Hz
  the whole band is audible.  Volume 32 is the most two rings and Jump (one
  channel, every pair of block-aligned start offsets) take without passing ±124: 122 at worst.
  Each effect is stored at its composed level (the loudest at ±127), so voices sum in software and
  play at volume 64 / boost.  Rounded once, dithered, at that level: re-scaling `amiga`'s samples
  would add an undithered second rounding.

### The manifest (`format: raw`)

| Field | Meaning |
|---|---|
| `file`, `rate`, `length` | The file, its rate, its size in bytes |
| `period`, `note`, `finetune` | `paula`: the ProTracker period playing `rate`, its note, 0 |
| `volume` | `normalise`: the composed level (1-64).  `global`: 64 / boost |
| `repeat_offset`, `repeat_length` | `paula`: the trailing silent word |
| `channels` | The channels' types (`FM`, `PSG`) |
| `slots`, `mask` | The SFX track slots it takes (FM3 FM4 FM5 PSG1 PSG2 PSG3; PSG3 tone and noise one), and as a 6-bit mask, bit 0 FM3 (`mask_bits` at the top).  An effect on a slot cuts the older one's track there (`sfx/slots.py`) |
| `peak` | The written peak in steps (127 / 32767 full scale) |
| `clipped` | Samples a boost pushed past the rails |

## Module map

| Module | Role |
|---|---|
| `sfx/tables.py` | Re-exports Sonic 1's tables (`core/drivers/reference.py`: frequency tables, PSG envelopes) and the SMPS register and channel maps (`core/smps/driver_tables.py`); they live in `core/` so the converter can reach them without importing the SFX driver |
| `sfx/track.py` | `SfxTrack` — mirrors the `SMPS_Track` RAM struct field for field |
| `sfx/chips.py` | Register writes mirroring `SetVoice`, `SendVoiceTL`, `FMUpdateFreq`, `PSGUpdateFreq` … |
| `sfx/driver.py` | `SfxDriver` — the per-frame state machine |
| `sfx/render.py` | Frame loop, FM + PSG mix at 53267 Hz, tail detection |
| `sfx/resample.py` | Re-exports `core/audio/resample.py` (polyphase windowed-sinc) |
| `sfx/config.py` | `configs/sfx.yaml` → `SfxConfig` (`RenderSettings`, `OutputSettings`) |
| `sfx/batch.py` | Discovery, naming, rendering at the chip's rate |
| `sfx/export.py` | Renders → files: channels, DC, rate, level, 8 / 16-bit, `.wav` / `.raw`, the manifest |
| `sfx/amiga.py` | Paula's side: period grid, FFT rate selection, the silent repeat word |
| `sfx/slots.py` | The SFX track slots an effect takes, the manifest's `slots` / `mask` |
| `sfx/wav.py` | WAV writer (stdlib `wave`), 8 or 16-bit |

## Things that are easy to get wrong

**The driver's tables, not equal temperament.**  The FM word is already the $A4/$A0 pair: write
`word >> 8`, then `word & 0xFF`, no fnum/block search.  Transposition wraps modulo 128 (driver
§ Note tables); SndBC Teleport's "invalid" `$90` transpose lands on the same index as the fixed
`$10`.

**One tick per V-int.**  `TempoWait` only holds the music tracks, so the music's
`fps × (m−1)/m` must not be applied.  Every SFX has `smpsHeaderTempoSFX $01`: 1 tick = 1/60 s.

**Modulation halves its step count on arm only** (driver § smpsModSet); 22 of the 49 SFX use
`smpsModSet`.  The frequency is rewritten only when modulation steps: the driver skips the
caller's write with `addq.w #4,sp`, `_do_modulation()` returns a bool.

**A duration byte with no note keeps the frequency** (driver § Note reads): SndA8 SS Goal re-keys
after each `smpsAlterPitch` at the old pitch.  The parser marks these `SmpsNote.is_retrigger`.

**`smpsNoAttack` skips the key-off and the re-arm**, so a tied note carries its PSG envelope and
modulation sweep on (driver § smpsNoAttack).

**PSG envelopes hold their last value** at `$80`.  `core/synth/psg_render.py` ramps to silence unless
told to `hold` — right for a one-shot MOD sample, wrong here.

**`SetVoice` and `SendVoiceTL` differ** (driver § Carriers by algorithm).  `core/synth/fm_voice.py::program_voice`
does `SetVoice`'s sum only and writes $B4 centred, so `sfx/chips.py` mirrors both routines instead.

**`OPN2.write_reg` costs two samples of chip time** and normally discards their audio; on a
continuous timeline that pulls the FM out of step with the PSG.  `OPN2.begin_capture()` keeps them
for the frame loop to prepend (off by default: the MOD path is unaffected).

**One timeline.**  The SN76489's output rate is fixed at `SN76489_Init`, so it is built at
`psg_synthesis.oversample` (8) × the OPN2's native 53267 Hz and its stream resampled down to the
FM's once, at the end.  At 1× the core's anti-aliasing is a box average and a square wave's
aliases fold back: Jump at 8287 Hz carried them at −24 dB of the signal (heard as crackle), −41
dB at 8×.  An effect with no PSG track skips the chip.

## Mix levels

One channel at full volume measures **YM2612 ±789** (the same for 1 and 4 carriers: the channel
accumulator clamps), **SN76489 tone ±4096**, noise ±2048.  Each chip is divided by its own full
scale, then the PSG × `psg_gain` (eight SFX mix both chips).

`psg_gain: 0.31` (−10.2 dB) is VGMPlay's balance, the reference the converter's volumes are measured
against: one PSG tone at attenuation 0 against one FM carrier at TL 0, rendered by VGMPlay through
the project's `VGMPlay.ini`, peaks 10.2 dB under with the Nuked core (12.0 dB with GPGX and GENS).
The driver cannot say: its levels are attenuations within each chip, the mix is the board's.  At
1.0, Sonic 1's Jump (PSG1 at attenuation 0) came out 10 dB louder against the Ring (FM5, carriers at
TL 0, channel volume $05) than VGMPlay plays our own register writes for the two; there they are
about equal, as on a Mega Drive's sound test (2026-10-14).

With `level: global` gain is one factor across all 49 files (`peak_dbfs`), not per-file
normalisation: the composed balance stays (the ring sits well below the death jingle).

## 8-bit Amiga export (`--variant amiga`)

```bash
python sonic2wav.py --all --variant amiga             # -> output/sonic_1/sfx/amiga/ + manifest.yaml
```

Per effect rates (`rate: auto`, `max_rate: 16574` for an A500, about half the size) are a variant
of their own, or a copy of `sfx.yaml` given with `--config`.

Headerless **signed 8-bit mono** `.raw` files and a `manifest.yaml` with what a raw file cannot
hold: rate, the note to trigger it at, the MOD volume, the repeat points.

Eight bits wants nearly the opposite of the WAV path:

- **Per-sample peak normalisation, the level restored by the MOD volume.**  Paula scales after the
  DAC, so the quantisation noise falls with the volume; storing a quiet effect quietly only
  discards bits.
- **DC removal first.**  The PSG-noise effects carry real offset (B8: a quarter of its peak), which
  wastes headroom and clicks on trigger.  A one-pole 30 Hz blocker, not mean subtraction: many SFX
  open with a rest, which a mean would push off zero.
- **Volumes measured after DC removal and resampling**, on the audio that plays.
- **Shaped TPDF dither** (`dither: shaped`, first order): the long `smpsAlterVol` ramps turn
  granular when truncated.  The converter's quantiser (`core.audio.to_int8`); its RNG is seeded
  from the length: runs are byte-identical.
- **One resampling stage**, chip rate straight to the target.

### Rate selection

Rates come from ProTracker's period table (`856 / 2^(n/12)`, C-1 to B-3), so each plays at true
pitch with finetune 0.  The lowest rate whose Nyquist holds 99 % of the effect's energy wins.  It
saves less than expected: Paula tops out near period 124 (~28.6 kHz) and two thirds of the effects —
square waves, LFSR noise, bright FM — want that ceiling.  Filter quality therefore matters more
than rate, hence the sharp polyphase sinc.

The target machine matters more than the material: an A500's fixed ~4.4 kHz filter makes rates
above ~11 kHz largely academic (`max_rate: 16574`); an A1200 hears them, and also hears Paula's
zero-order-hold imaging, so low rates sound grittier there.

### MOD mechanics

Lengths are padded even (MOD stores words) and a silent word is appended, the manifest's
`repeat_offset` / `repeat_length` pointing at it: Paula loops the repeat region once a sample ends,
and silence there stops a one-shot buzzing.  Samples are mono: the ring pair's panning is a channel
choice (`B5_Ring` right, `CE_Ring_Left_Speaker` left).  A MOD holds 31 instruments, so the 49 never
fit one module.

No tail trimming beyond the WAV path's: 44 of the 45 SFX voices release every operator at `$0F`.

## Known deviations from stock hardware

1. **The `SendVoiceTL` bug is not reproduced.**  Stock Sonic 1 reads the SFX voice pointer from
   `(a6)` instead of `(a5)`, so every `smpsAlterVol` in an SFX (12 of the 49) uploads TLs from a
   garbage pointer.  sonic2wav does the `FixBugs` version: those ramps are cleaner than hardware.
2. **PSG table overrun is extrapolated.**  SndB6 Spikes Move (`nG6`, index 79) and the unused SndA2
   (`nCs6`, index 73) read past the 70-entry table into code; the twelve-tone sequence is continued
   from the last musical entry instead.  `psg_past_table: clamp` clamps to the table's top.  Index 69 (`nMaxPSG`)
   keeps its divider 1.
3. **FM table overrun caps the octave.**  SndA8 SS Goal reaches index 96 — one past the table —
   after 37 `smpsAlterPitch $02` steps; the block field saturates at 7 anyway, so whole octaves are
   dropped until the note fits, keeping the pitch class.
4. **PSG3's noise stops at `smpsStop`** (the `FixBugs` behaviour); stock leaves it ringing, which
   would poison the rest of the render.
