# User-facing improvements — from the Title Screen accuracy audit

Source: `docs/title_screen_audit.md` (2026-09-17). Each item names the problem the audit
measured, what to change, and where. Verify any of them with:

```bash
python tools/vgm_compare.py configs/<song>.yaml "reference/vgz/<song>.vgz"
```

Status key: `[ ]` open, `[x]` done.

---

## Converter accuracy

### [ ] 1. Note fill and modulation timers count frames, not ticks
- **Measured:** PSG3 fill `$0C` cuts at 250 ms in the MOD, 200 ms on hardware (tempo mod 5 → 48 ticks/s, fill runs at 60 Hz).
- **Why:** `TempoWait` only bumps `DurationTimeout`; `NoteTimeoutUpdate` and `DoModulation` still run every V-int.
- **Fix:** in `core/smps2mod.py`, scale before placement: `fill_ticks = fill_frames × ticks_per_sec / fps`; same for `vibrato_wait`. For tempo modifier 5 that is ×0.8.
- **Affects:** every song with a tempo modifier ≠ 0.

### [ ] 2. Vibrato (`smpsModSet` → `4xy`) formula
- **Measured:** FM4 closing A2 with `smpsModSet $00,$01,$06,$04`: hardware 5.75 Hz / ±19 cents; MOD `485` = 3.85 Hz / ±33 cents. Correct effect is `4C3`.
- **Driver truth:** steady cycle = `2·speed·(steps+1)` frames (steps = ORIGINAL byte; first half-cycle uses steps/2), amplitude = `delta·steps/2` FNUM units, relative to the note's own FNUM (644–1216), not a fixed 644.
- **ProTracker truth:** cycle = `64/x` processing ticks with `speed−1` processing ticks per row; amplitude ≈ `2·y` period units.
- **Fix:** `x = round(64 × (speed/(speed−1)) × (2.5/BPM) / cycle_s)`, `y = round(period × (2^(cents/1200) − 1) / 2)`.
- **Where:** `core/smps2mod.py` around the `_smps_cycle` / `eff_vib_depth` code; update `docs/pipeline.md` gotcha 4.

### [ ] 3. Sub-row onsets via `EDx` note delay
- **Measured:** the 2-tick DAC snare roll at ticks 224/226/228 loses the first hit (collides with a rest on row 74).
- **Fix:** when `tick % ticks_per_row != 0` and the effect slot is free, emit `EDx` with `x = round(offset_ticks × speed / tpr)`; let a delayed note win over a rest `C00` on the same row.
- **Where:** `_tick_to_pattern_row` callers in `core/smps2mod.py`.

### [ ] 4. Bake channel TL into instrument volume instead of per-note `Cxx`
- **Measured:** with `fm_volume_scaling: false` FM1/FM3/FM4/FM5 were 1.8–3.2 dB too hot vs FM2 (header TL `$0C/$09/$0D/$0C/$0E` ignored). With it `true`, every note gets a `Cxx` (MOD resets volume on trigger) — the "clutter" that keeps it off.
- **Fix:** at first use of an `(instrument, channel)` pair bake `sample_volume × 10^(−TL×0.75/20)` into the instrument's default volume (create a variant slot when two channels share an instrument at different TLs), and emit `Cxx` only when `smpsAlterVol` moves the channel off that baked level.
- **Interim:** hand-compute volumes as done in `configs/01_title_screen.yaml` (comment block above `sample_list`).

### [ ] 5. Global PSG-to-FM level calibration
- **Measured:** noise was +11.6 dB vs FM2 compared with the recording; configs use `psg_noise.raw` volumes of 16, 16, 16, 24, 32, 32, 48, 64 for the same synthesised sample.
- **Fix:** one `psg_to_fm_db` (or equivalent gain) in `configs/settings.yaml` applied to synthesised PSG samples, verified once with `vgm_compare.py`; drop the per-song guesses.
- **Caveat:** VGMPlay's PSG/FM ratio approximates hardware to roughly ±3 dB.

### [ ] 6. Derive rate-3 noise divider from the note data
- **Measured:** `synth_root: A8` gave a 7 kHz LFSR (dull rattle); the driver writes tone-2 divider 0 for `nMaxPSG` (`PSGFrequencies[69]` = 223721 Hz), which the VDP PSG clocks as N=1 → near-white hiss.
- **Done:** explicit `tone2_n:` key (`core/config.py`, `sn76489/sample_generator.py`); Title Screen uses `tone2_n: 1`.
- **Open:** when neither `tone2_n` nor `synth_root` is given, derive N from the channel's first noise note through the driver table (`sfx/tables.py` `PSG_FREQUENCIES`), treating 0 as 1. Check other configs that set `synth_root` on rate-3 noise entries.

### [ ] 7. Multi-sample wide FM ranges
- **Measured:** voice 1 synthesised at A2, played up to D4 (17 semitones) → envelope runs up to 2.7× faster on top notes (G3: 5.7 dB decay in 0.3 s vs 3.5 dB on hardware).
- **Fix:** split ranges wider than ~9 semitones into two `voice_map` entries with their own `root`/`synth_root` (e.g. `A2–D3` at root A1, `D#3–D4` at root D#2). Could be automated: a `max_range_semitones` option that auto-splits and synthesises each half.

### [ ] 8. Detune variants (`smpsAlterNote`)
- **Measured:** `$03` is +5…8 cents on hardware; finetune +1 = +12.5 cents is the closest MOD step.
- **Done for Title Screen:** FM5 and FM3's ending note use finetune +1 variants via `channel_instrument_map`.
- **Open:** synthesise the variant with the FNUM offset applied instead of using finetune, and auto-create it when a channel carries `smpsAlterNote` ≠ 0.

---

## Tooling / workflow

### [x] `tools/vgm_compare.py` — rendered per-channel MOD-vs-VGZ audit
Per-note pitch and level, channel balance, onset timing, vibrato, noise spectrum, DAC rate. Needs VGMPlay and an ffmpeg build with libopenmpt.
- [x] `--json FILE` output plus opt-in CI thresholds `--fail-balance-db`, `--fail-pitch-cents`, `--fail-unmatched` (exit 1 when exceeded; results in the JSON `checks` list).
- [x] Vibrato rate/depth estimate on notes ≥ 0.5 s (heterodyne partial tracking, modulated stretch only). Title Screen FM4 closing A2: hardware 5.99 Hz ±19 c vs MOD 3.98 Hz ±36 c; GHZ FM4/FM5 C6: 4.46 Hz ±6 c vs 6.5 Hz ±7 c — both feed item 2.
- [x] VGMPlay location: defaults to `reference/vgz/vgmplay/` (untracked) after `--vgmplay` / `VGMPLAY_DIR`; fresh-checkout setup in `docs/pipeline.md` § Verifying against a VGZ. Needs the 0.51.x (libvgm) line for the `Core = NUKE` ini key. No direct binary download URL is recorded — only the source repo could be confirmed.
- [ ] `--fail-unmatched` is noisy on sustained FM channels (MOD re-triggers where hardware ties notes → extra onsets; Title Screen FM2 reports 4). Match on key-on events instead of detected onsets for channels that have them.
- [ ] GHZ shows MOD-only 2.5–3 Hz ±13 c wobble on some long FM3/FM4 notes (`not in VGM` rows) — check whether it is a stray `4xy` or a sample loop that is not a whole number of periods.

### [x] `tools/vgm_analyze.py` — tone-2 divider on rate-3 noise rows, DAC seek events, per-channel counts
- [x] `reference/vgm/` paths in the CLAUDE.md examples and the tool docstring corrected to `reference/vgz/`.

### [x] Config authoring
- [x] `analyze.py` skeleton: FM `sample_list` volumes pre-computed from each channel's TL offset at the voice's first note (`smpsHeaderFM` volume + `smpsAlterVol`), relative to the loudest channel, with a per-channel breakdown comment and a `channel_instrument_map` hint when channels sharing a voice differ. Reproduces the hand-computed Title Screen values (25 / 21 / 32). `tone2_n` for rate-3 noise comes from the driver's `PSGFrequencies` table (divider 0 → 1); pitched-noise channels also get `low:`.
- [x] `convert.py` and `analyze.py --config` warn when a rate-3 noise entry without `tone2_n` has a `synth_root` outside the driver table (C3–Gs8). Currently fires on GHZ, SYZ, LZ, SLZ, SBZ (×3), Ending and Invincibility — all `synth_root: A8` on an `nMaxPSG` channel; switching them to `tone2_n: 1` is item 6 (it changes the noise sample, so re-check levels per item 5).
- [x] `tests/baselines/title_screen_baseline.mod` regenerated (25 intended differences: 24× channel 6 `C13`→`C06`, channel 3 instrument 5→8 on the closing note). `regression_test.py --only NAME` added so one baseline can be refreshed alone.
