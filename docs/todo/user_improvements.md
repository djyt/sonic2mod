# User-facing improvements — from the accuracy audits

Sources: `docs/audits/01_title_screen_audit.md` (2026-09-17), `docs/audits/02_ghz_audit.md` (2026-09-18). Each item
names the problem an audit measured, what to change, and where. Verify any of them with:

```bash
python tools/vgm_pitch_audit.py configs/<song>.yaml "reference/vgz/<song>.vgz"   # every note right?
python tools/vgm_compare.py     configs/<song>.yaml "reference/vgz/<song>.vgz"   # levels, timing, timbre
```

Audited so far: Title Screen, Green Hill Zone. VGZs on hand but not audited: Marble Zone, Spring Yard Zone.

Status key: `[ ]` open, `[x]` done.

---

## Converter accuracy

### [x] 1. Note fill and modulation timers count frames, not ticks
- **Done:** `_ticks_per_frame` = `(mod−1)/mod` in `core/smps2mod.py`, applied to the fill and the `smpsModSet` wait; cuts placed to the MOD tick (`ECx` in-row / `C00` on a boundary); fill-vs-duration test done in frames, so fills equal to the duration byte now fire; parser no longer multiplies the modulation wait by the tempo divider. Verified on GHZ key-off timing (4/11/20/30 frames exact) and Title Screen noise cuts (worst +75 ms → +15 ms). Details: `docs/pipeline.md` gotcha 4b.
- **Still open:** mid-song `smpsSetTempoMod` (Credits, Drowning) uses the header modifier; vibrato *rate* is item 2; off-grid note-ons (cut is at the right absolute time but the note-on is half a row off — GHZ FM4 reads 533 ms instead of 500) are item 3.
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
- **GHZ:** FM3/FM4/FM5 play `nC6, $01, smpsNoAttack, nB5, $0F` (1-tick grace + legato slide) 17× each; at `ticks_per_row: 2` the grace is silenced by a rest's `C00` (FM3) or overwritten (FM4/FM5). `EDx` alone cannot put two notes in one row — see the three options in `docs/audits/02_ghz_audit.md` § Grace notes (`ticks_per_row: 1` / speed 2, `3xx` legato for `smpsNoAttack`, or drop the grace deliberately).
- **Measured:** the 2-tick DAC snare roll at ticks 224/226/228 loses the first hit (collides with a rest on row 74).
- **Fix:** when `tick % ticks_per_row != 0` and the effect slot is free, emit `EDx` with `x = round(offset_ticks × speed / tpr)`; let a delayed note win over a rest `C00` on the same row.
- **Where:** `_tick_to_pattern_row` callers in `core/smps2mod.py`.

### [ ] 4. Bake channel TL into instrument volume instead of per-note `Cxx`
- **GHZ (biggest remaining level error in that song):** with `fm_volume_scaling: false` the header TL is ignored *and* one `smpsAlterVol` step becomes one linear MOD volume unit (≈ 0.3 dB) instead of 0.75 dB. Notes carrying a `Cxx` are +3.4…+6.7 dB on FM4/FM5 (FM5 `$13`→`$19`: predicted +3.6, measured +3.4); FM1's fade drifts from +5.8 to +0.4 dB across `C1A`→`C1F`. Per-instrument table in `docs/audits/02_ghz_audit.md`.
- **Pan law:** a centred YM2612 channel drives both speakers, an Amiga channel one. FM3 (centre), FM4 (left) and FM5 (right) share GHZ instrument 14 at the same TL and FM4/FM5 still read ~+2.9 dB. Bake −3 dB for channels the song hard-pans (needs the dominant `smpsPan` per channel/voice).
- **Sample cost — prefer `Cxx` on the minority channel over variant slots:** MOD instruments cannot share sample data (~61 KB each in GHZ). Variants by TL alone: +2 instruments / +122 KB on a 760 KB MOD; with the pan law: +4 / +248 KB. Baking the busiest channel's level and emitting `Cxx` on the other channels' notes costs no samples (GHZ: FM3's 16 notes on instrument 5 and 63 on 11/13/14). Make that the default, variants optional.
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
- **GHZ:** detuned-carrier beating scales with playback rate — voice $04 (synthesised at C5) beats at 6.5 Hz on C6 where hardware beats at 4.46 Hz; FM3/FM4 low notes beat at 2.5–3 Hz in the MOD only.
- **Measured:** voice 1 synthesised at A2, played up to D4 (17 semitones) → envelope runs up to 2.7× faster on top notes (G3: 5.7 dB decay in 0.3 s vs 3.5 dB on hardware).
- **Fix:** split ranges wider than ~9 semitones into two `voice_map` entries with their own `root`/`synth_root` (e.g. `A2–D3` at root A1, `D#3–D4` at root D#2). Could be automated: a `max_range_semitones` option that auto-splits and synthesises each half.

### [ ] 8. Detune variants (`smpsAlterNote`)
- **Measured:** `$03` is +5…8 cents on hardware; finetune +1 = +12.5 cents is the closest MOD step.
- **Done for Title Screen:** FM5 and FM3's ending note use finetune +1 variants via `channel_instrument_map`.
- **Open:** synthesise the variant with the FNUM offset applied instead of using finetune, and auto-create it when a channel carries `smpsAlterNote` ≠ 0.

### [x] 9. FM frequency convention is an octave off in two places that cancel
- **Done (2026-09-18):** both functions now use `2^(21−block)`; all 103 FM `synth_root` values in the 18 configs lowered one octave (`voice_map` / `channel_instrument_map` only — PSG untouched); `analyze.py` skeleton no longer adds 12; legacy fallback C5 → C4; `_smps_note` FM offset removed (its SMPS column is unchanged). Every config's MOD is byte-identical before/after. `vgm_analyze` now reads GHZ FM2 as `A2 A3 A2 A#2` = the source's `nA2, nA3, nA2, nBb2`. The documented rule `synth_root = low + total_transpose` now holds literally (GHZ voice $08: C5 − 36 = `C2`).
- `tools/vgm_analyze._fnum_to_hz` and `ym2612/renderer.freq_to_fnum_block` use `2^(20−block)`; the chip and the driver's `MakeFMFrequency` (`f·2^21/fs`) are `2^(21−block)`. The analyzer reads FM an octave high, the synth renders an octave below the `synth_root` name, and configs tuned one against the other sound right (GHZ 867/867). Fix = both functions plus every FM `synth_root` down an octave, in one commit. `sfx/` already uses the driver table and is correct. Project memory's "FM chip plays one octave above the SMPS label" is this artefact.

### [x] 10. Coordination flags were applied one note early (parser)
- A note byte with no duration byte stayed "pending" while the flags after it were emitted, so `smpsSetvoice` / `smpsAlterPitch` / `smpsAlterVol` / `smpsNoteFill` hit the *previous* note. Fixed in `core/smps_parser.py` (flags, `smpsCall` and `smpsReturn` complete the pending note — matches `FMDoNext`'s put-back). Found by the GHZ audit: last intro note of FM3 played on voice $08 two octaves low. GHZ 52 cells / SBZ 7 cells changed; the GHZ config's one-note `B4` workaround instruments (7, 9 — 115 KB) were removed.

---

## Tooling / workflow

### [x] `tools/vgm_compare.py` levels use L/R power, not a mono mix
Averaging to mono read hard-panned YM2612 channels ~5 dB low against centred ones (2.1 dB vs a power sum) while every MOD channel lost the same ~1 dB — GHZ FM4/FM5 looked hotter than they are and two GHZ volumes were over-corrected (since reverted). Title Screen (no pans) unaffected.

### [x] `tools/vgm_pitch_audit.py` — symbolic pitch audit (no rendering)
Chip frequency-register timeline vs the pitch each MOD note sounds at (from `root` / `synth_root` / finetune); sees legato pitch changes, ignores grace notes and vibrato steps below `--min-ms`; exit 1 on any wrong or missing note. GHZ 867/867, Title Screen 86/86.
- [ ] Fold it into `vgm_compare.py` as the pitch verdict: that tool's per-note pitch column measures audio windows and flagged 245 GHZ notes that were all artefacts of grace notes and legato runs. At minimum flag on MOD-vs-VGM, not MOD-vs-key-on.

### [x] `tools/vgm_compare.py` — rendered per-channel MOD-vs-VGZ audit
Per-note pitch and level, channel balance, onset timing, vibrato, noise spectrum, DAC rate. Needs VGMPlay and an ffmpeg build with libopenmpt.
- [x] `--json FILE` output plus opt-in CI thresholds `--fail-balance-db`, `--fail-pitch-cents`, `--fail-unmatched` (exit 1 when exceeded; results in the JSON `checks` list).
- [x] Vibrato rate/depth estimate on notes ≥ 0.5 s (heterodyne partial tracking, modulated stretch only). Title Screen FM4 closing A2: hardware 5.99 Hz ±19 c vs MOD 3.98 Hz ±36 c — feeds item 2.
- [x] Beating vs vibrato: rows are tagged `b` when the partial's level swings at the same rate (≥ 15 %) — that is two detuned FM carriers beating, not `smpsModSet`. **Correction:** the GHZ FM4/FM5 C6 rows (4.46 Hz vs 6.5 Hz) first recorded here as item 2 evidence are beating — those channels have no modulation at all. The rate differs because the MOD sample is resampled, so they belong to item 7 (multi-sampling / `synth_root`), as do the MOD-only 2.5–3 Hz rows on FM3/FM4.
- [ ] PSG vibrato is invisible to the table: `vgm_analyze._parse_vgm` reports every PSG period change as a key-on, so a modulated PSG note is chopped into frame-long "notes". Needs key-on detection from volume only (GHZ PSG1 `smpsModSet $0E,$01,$01,$03` is the test case).
- [x] VGMPlay location: defaults to `reference/vgz/vgmplay/` (untracked) after `--vgmplay` / `VGMPLAY_DIR`; fresh-checkout setup in `docs/pipeline.md` § Verifying against a VGZ. Needs the 0.51.x (libvgm) line for the `Core = NUKE` ini key. No direct binary download URL is recorded — only the source repo could be confirmed.
- [ ] `--fail-unmatched` is noisy on sustained FM channels (MOD re-triggers where hardware ties notes → extra onsets; Title Screen FM2 reports 4). Match on key-on events instead of detected onsets for channels that have them.

### [x] `tools/vgm_analyze.py` — tone-2 divider on rate-3 noise rows, DAC seek events, per-channel counts
- [x] `reference/vgm/` paths in the CLAUDE.md examples and the tool docstring corrected to `reference/vgz/`.

### [x] Config authoring
- [x] `analyze.py` skeleton: FM `sample_list` volumes pre-computed from each channel's TL offset at the voice's first note (`smpsHeaderFM` volume + `smpsAlterVol`), relative to the loudest channel, with a per-channel breakdown comment and a `channel_instrument_map` hint when channels sharing a voice differ. Reproduces the hand-computed Title Screen values (25 / 21 / 32). `tone2_n` for rate-3 noise comes from the driver's `PSGFrequencies` table (divider 0 → 1); pitched-noise channels also get `low:`.
- [x] `convert.py` and `analyze.py --config` warn when a rate-3 noise entry without `tone2_n` has a `synth_root` outside the driver table (C3–Gs8). Currently fires on GHZ, SYZ, LZ, SLZ, SBZ (×3), Ending and Invincibility — all `synth_root: A8` on an `nMaxPSG` channel; switching them to `tone2_n: 1` is item 6 (it changes the noise sample, so re-check levels per item 5).
- [x] `tests/baselines/title_screen_baseline.mod` regenerated (25 intended differences: 24× channel 6 `C13`→`C06`, channel 3 instrument 5→8 on the closing note). `regression_test.py --only NAME` added so one baseline can be refreshed alone.
