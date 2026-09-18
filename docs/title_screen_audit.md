# Title Screen conversion audit (2026-09-17)

Accuracy audit of `configs/01_title_screen.yaml` → `output/01_title_screen.mod` against the
hardware recording `reference/vgz/01 - Title Theme.vgz`.  Method, numbers, what was changed
and what is still open.  Reproduce with:

```bash
python tools/vgm_analyze.py "reference/vgz/01 - Title Theme.vgz" --chip all --max-rows 0
python tools/vgm_compare.py configs/01_title_screen.yaml "reference/vgz/01 - Title Theme.vgz" \
       --vgmplay C:\coding\amiga\music\vgmplay
```

`vgm_compare.py` renders the VGZ one chip channel at a time (VGMPlay, Nuked OPN2 core, mute
masks) and the MOD one channel at a time (ffmpeg + libopenmpt on channel-isolated copies), aligns
them by envelope cross-correlation (MOD lags the VGZ by 250 ms: the recording starts at the first
DAC hit, SMPS tick 12) and compares every key-on.

---

## Verdict

| Aspect | Result |
|--------|--------|
| Notes | All 87 FM key-ons at the right pitch, within −10…+9 cents (ProTracker period-table rounding). Same note counts per channel as the SMPS data. |
| Timing | Exact. `smpsHeaderTempo $01,$05` = 48 ticks/s; BPM 120 / speed 3 / ticks_per_row 3 gives one MOD tick per SMPS tick. Onset medians 0 ms after alignment; the driver's tempo-skip jitter (±8 ms) is the only difference. One 2-tick DAC snare hit (tick 224) is lost because it falls between rows. |
| FM balance (before) | FM1/FM3/FM4/FM5 were 1.8 / 2.2 / 1.8 / 3.2 dB too loud relative to FM2 — the `smpsHeaderFM` TL offsets ($0C/$09/$0D/$0C/$0E) were not represented. |
| FM balance (after) | Within ±0.5 dB of the recording on every channel. |
| Noise timbre (before) | Dull: energy below 4 kHz. `synth_root: A8` clocked the LFSR at 7 kHz, but the driver writes tone-2 divider **N=0** for `nMaxPSG` (the VGZ confirms it), which the VDP PSG clocks as N=1 → 112 kHz shift rate, near-white hiss. |
| Noise timbre (after) | `tone2_n: 1`: flat to 8 kHz like the recording; the 8–13 kHz band is still ~6 dB down (emulator decimation to the 27.9 kHz sample rate). |
| Noise level | Was +11.6 dB relative to FM2 compared with the recording; now +1.5 dB (sample volume 48 → 16). |
| Noise envelope | fTone_04 decay shape matches within 1–2 dB out to 133 ms. |
| Note fill | Fill 3 cuts at 42 ms in the MOD vs 50 ms on hardware; fill 12 cuts at 250 ms vs **200 ms**. The driver counts fill in frames (60 Hz), the converter counts ticks (48/s). |
| Vibrato (FM4 closing A2) | Hardware: 5.75 Hz, ±19 cents. MOD (`485`): 3.85 Hz, ±33 cents. Formula in `smps2mod.py` is off (see below). |
| Envelope decay | Sustained FM2 notes track the recording within ~2 dB over 2 s. Short notes played far above the sample's synth pitch decay proportionally faster (sample-rate scaling), e.g. G3 on voice 1 (synthesised at A2) loses 5.7 dB in 0.3 s vs 3.5 dB on hardware. |
| DAC | Kick fundamental 55.2 Hz vs 53.8 Hz (C2 = 8287 Hz vs the original 8250 Hz); snare band profile matches; the recording's DPCM hiss is absent in the MOD (cleaner, not wrong). DAC level relative to FM2 matches to 0.1 dB. |
| Detune | FM5's `smpsAlterNote $03` is +5…8 cents on hardware; finetune +1 gives +12.5 cents (closest available). FM3's ending note now also gets the detuned variant so the A2 unison beats instead of summing in phase. |

Per-channel level after the fix (whole-song RMS relative to FM2, dB):

| Channel | VGZ | MOD | diff |
|---------|-----|-----|------|
| DAC | −2.3 | −2.3 | 0.0 |
| FM1 | −7.1 | −7.5 | −0.3 |
| FM3 | −4.2 | −3.7 | +0.5 |
| FM4 | −3.8 | −3.7 | +0.1 |
| FM5 | −8.5 | −9.0 | −0.5 |
| NOISE | −23.8 | −22.2 | +1.5 |

(The PSG/FM ratio of VGMPlay is itself an approximation of hardware; treat the noise figure as ±3 dB.)

---

## Config changes made

1. `sample_list` FM volumes now bake in the header TL offsets relative to FM2: inst 3 → 25,
   inst 4 → 21, inst 6 → 24 (`32 × 10^(−steps×0.75/20)`). FM3/FM4 end on voice $01 with
   `smpsAlterVol −4/−3`, which lands exactly on FM2's $09, so instrument 5 serves all three at 32.
2. `psg_map[0xE7]`: `synth_root: A8` replaced by the new `tone2_n: 1`; volume 48 → 16.
3. `channel_instrument_map.FM3` voice 1 → instrument 8 (`fm_voice1.raw`, finetune +1).

`tests/baselines/title_screen_baseline.mod` now differs on purpose (channel 6: `C13` → `C06`;
channel 3: instrument 5 → 8 on the closing note). Regenerate with
`python tools/regression_test.py --generate-baselines` once accepted.

---

## Converter work this audit points at

### 1. Note fill and modulation are frame-based, not tick-based

`TempoWait` only increments each track's `DurationTimeout`; `NoteTimeoutUpdate` and
`DoModulation` still run every V-int.  So `smpsNoteFill`, `ModulationWait` and `ModulationSpeed`
count frames (60 Hz NTSC / 50 Hz PAL) while durations count ticks (`fps × (mod−1)/mod`).

Fix: `fill_ticks = fill_frames × ticks_per_sec / fps` before `_tick_to_pattern_row(tick + fill)`;
same scaling for `vibrato_wait`.  For the Title Screen (mod 5) that is ×0.8.

### 2. Vibrato mapping

Driver behaviour (`DoModulation`): every `speed` frames add `delta` to the accumulator; after
`steps` adds the delta is negated and one frame is spent reloading. The first half-cycle uses
`steps/2` (the byte is halved on `smpsModSet`), every later half-cycle uses the full `steps` byte.

| Quantity | Correct | `smps2mod.py` today |
|----------|---------|---------------------|
| steady cycle | `2·speed·(steps+1)` frames | `2·speed·(steps/2+1)` ticks |
| amplitude | `delta·steps/2` FNUM units | `delta` FNUM units |
| ProTracker cycle | `64/x` processing ticks, `speed−1` per row | `16/x` rows |
| ProTracker amplitude | ≈`2·y` period units | `y` period units |
| FNUM reference | the note's own FNUM (644…1216) | fixed 644 |

For `smpsModSet $00,$01,$06,$04` at BPM 120 / speed 3 the right effect is `4C3`, not `485`:
`x = 64 × (speed/(speed−1)) × (2.5/BPM) / cycle_s = 64 × 1.5 × 0.02083 / 0.1667 = 12`,
`y = period × (2^(cents/1200) − 1) / 2 = 508 × 0.0111 / 2 ≈ 3`.

### 3. Sub-row onsets

`_tick_to_pattern_row` truncates. Any note that starts between rows (the 2-tick DAC roll at ticks
224/226/228) is either shifted or, when it collides with a rest on the same row, dropped.
ProTracker's `EDx` (note delay, x ticks) places a note at `row + x/speed` exactly; emitting it
whenever `tick % ticks_per_row != 0` and the effect slot is free makes placement tick-exact.

### 4. FM channel balance without per-note `Cxx`

`fm_volume_scaling: true` reproduces the header TL but emits a `Cxx` on *every* note (MOD resets
volume on trigger), which is why it is off by default. A cleaner scheme: bake the channel's TL at
first use into the instrument's default volume (per `(instrument, channel)` pair, creating a
variant when two channels share an instrument at different TLs) and emit `Cxx` only when a
`smpsAlterVol` moves a channel away from that baked level. That gives the balance measured above
with zero pattern clutter, and the YAML volumes stop being hand-tuned.

### 5. PSG vs FM level calibration is per-song guesswork

`psg_noise.raw` volumes across configs: 16, 16, 16, 24, 32, 32, 48, 64. The ratio between the
synthesised PSG sample level and the synthesised FM sample level is a property of the two
synthesis pipelines, not of the song. One global calibration (a `psg_to_fm_db` in `settings.yaml`,
verified once with `vgm_compare.py`) would replace all of those.

### 6. Rate-3 noise divider from the note data

The driver derives the LFSR clock from the channel's note through `PSGFrequencies`
(`sfx/tables.py` already carries that table). Deriving `tone2_n` from the first noise note
automatically would remove the most confusing field in the PSG config; `tone2_n` stays as the
manual override.

### 7. Multi-sampling for wide ranges

Voice 1 is synthesised at A2 and played up to D4 (17 semitones), so its envelope runs up to 2.7×
faster on the top notes. Splitting the range in two (`A2–D3` at root A1, `D#3–D4` at root D#2)
halves the stretch at the cost of one extra 16 KB sample.

---

## Easier for the end user

- `tools/vgm_compare.py` gives the numbers above for any song with a VGZ in `reference/vgz`
  (needs VGMPlay and an ffmpeg with libopenmpt); flagged rows point at the exact note.
- `tools/vgm_analyze.py --chip all` now prints the tone-2 divider on rate-3 noise key-ons
  (`white/tone2 N=0`) and DAC seek events, so both the noise config and the drum timing can be
  checked without listening.
- `tone2_n:` replaces the octave-12 `synth_root` arithmetic for rate-3 noise.
- The volume comment block in `01_title_screen.yaml` shows the TL → volume formula so other
  configs can copy it until the converter does it (item 4).
