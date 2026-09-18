# User-facing improvements — from the accuracy audits

Sources: `docs/audits/01_title_screen_audit.md` (2026-09-17), `docs/audits/02_ghz_audit.md` (2026-09-18),
`docs/audits/00_soundtrack_survey.md` (2026-09-18, all 18 configs: pitch + levels). Each item
names the problem an audit measured, what to change, and where. Verify any of them with:

```bash
python tools/vgm_pitch_audit.py configs/<song>.yaml "reference/vgz/<song>.vgz"   # every note right?
python tools/vgm_compare.py     configs/<song>.yaml "reference/vgz/<song>.vgz"   # levels, timing, timbre
```

Fully audited: Title Screen, Green Hill Zone. Every other config has been surveyed for pitch and levels
(all 18 have a VGZ); what each still needs is listed in `docs/audits/00_soundtrack_survey.md` §3.

Status key: `[ ]` open, `[x]` done.  A done item keeps what was measured (so the reason stays on
record) but not its old plan — anything still open under it is called out as **Still open**.

---

## Converter accuracy

### [x] 1. Note fill and modulation timers count frames, not ticks
- **Done:** `_ticks_per_frame` = `(mod−1)/mod` in `core/smps2mod.py`, applied to the fill and the `smpsModSet` wait; cuts placed to the MOD tick (`ECx` in-row / `C00` on a boundary); fill-vs-duration test done in frames, so fills equal to the duration byte now fire; parser no longer multiplies the modulation wait by the tempo divider. Verified on GHZ key-off timing (4/11/20/30 frames exact) and Title Screen noise cuts (worst +75 ms → +15 ms). Details: `docs/pipeline.md` gotcha 4b.
- **Still open:** mid-song `smpsSetTempoMod` (Credits, Drowning) uses the header modifier; vibrato *rate* is item 2; off-grid note-ons (cut is at the right absolute time but the note-on is half a row off — GHZ FM4 reads 533 ms instead of 500) are item 3.
- **Measured:** PSG3 fill `$0C` cuts at 250 ms in the MOD, 200 ms on hardware (tempo mod 5 → 48 ticks/s, fill runs at 60 Hz).
- **Why:** `TempoWait` only bumps `DurationTimeout`; `NoteTimeoutUpdate` and `DoModulation` still run every V-int.
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

### [x] 4. Bake channel TL into instrument volume instead of per-note `Cxx`
- **Done (2026-09-18), "Cxx on the minority channel" design:** `fm_volume_scaling: baked` (new default in `configs/settings.yaml`) + `fm_pan_law_db: 3`. Per MOD instrument the most common (TL offset, pan) level is baked = the `sample_list` volume, no command; other notes get `Cxx = volume × 10^(ΔdB/20)` with 0.75 dB/TL step and −3 dB for hard-panned notes. No variant instruments, no extra samples. `Cxx` on FM notes across the 18 configs: 1662 → 887. GHZ: FM1's fade error flat (was +5.8→+0.4 dB drift), shared instruments read the same on every channel (inst 11: +4.8/+4.6/+4.6), and after setting four volumes from those readings every FM channel is within ±0.3 dB. Title Screen FM3 +0.5 → −0.1 dB. Legacy `true` / `false` kept and verified byte-identical to the old output. Details: `docs/pipeline.md` §FM levels.
- **PSG done too (2026-09-18):** `psg_volume_scaling: baked` (default), level = −2 dB × attenuation, no pan. `Cxx` on PSG notes 3241 → 498 across the 18 configs; all PSG `sample_list` volumes migrated to the value their dominant attenuation used to emit (trailing comment on each line), per-note effective volume identical except 268 SBZ notes 12 → 13 (rounding, new value is the closer one). Attack-row note cuts no longer displaced by the volume command (Title Screen 12, Spring Yard 264).
- **`analyze.py` skeleton done too:** FM volume = `76 × carriers × 10^(−(0.75·TL + pan)/20)`, max 64, from the most common (TL, pan) level of the voice on its busiest channel. `carriers` undoes `carrier_balance` (an N-carrier sample is rendered 20·log10(N) dB quieter than the chip). The constant 76 was fitted to the 13 instruments measured in the two audits (each implies 68–92): GHZ skeleton 29/34/21/9/64/24/32 vs measured 32/32/25/9/64/25/23–32. PSG = base × 10^(−2·att/20), tone base 16, noise 16.
- **Measured before:** with `fm_volume_scaling: false` the header TL was ignored and one `smpsAlterVol` step became one linear MOD volume unit (≈ 0.3 dB, the chip does 0.75): Title Screen FM1/FM3/FM4/FM5 1.8–3.2 dB hot, GHZ `Cxx` notes +3.4…+6.7 dB, FM1's fade drifting +5.8 → +0.4 dB. Pan law: FM3 (centre), FM4 (left) and FM5 (right) share GHZ instrument 14 at the same TL and FM4/FM5 read ~+2.9 dB. Variant slots would have cost +122…+248 KB on GHZ, which is why `Cxx` on the minority channel was chosen.
- Nothing open: every config's volumes were re-measured under the new law in item 5.

### [x] 5. Global PSG-to-FM level calibration
- **Done (2026-09-18) — by measurement, not by a global gain.** A gain on the synthesised PSG samples would cost sample resolution; the level belongs in the `sample_list` volume. With a VGZ for every song, `vgm_compare.py --write-volumes` set every instrument's volume from its measured level error and a second render verified it: instruments ≥ 2 dB off 74 → 2, every song except Drowning at ≤ 1.2 dB weighted RMS error (Marble Zone 9.7 → 0.3, Star Light 9.2 → 0.6, Scrap Brain 6.3 → 0.5). The fitted formula (`76 × carriers × 10^(−(0.75·TL + pan)/20)`, PSG base 16, in the `analyze.py` skeleton) predicted the same errors beforehand and stays as the starting point for new configs. Table in `docs/audits/00_soundtrack_survey.md`.
- **Measured:** noise was +11.6 dB vs FM2 compared with the recording; configs use `psg_noise.raw` volumes of 16, 16, 16, 24, 32, 32, 48, 64 for the same synthesised sample.
- **Caveat:** VGMPlay's PSG/FM ratio approximates hardware to roughly ±3 dB.

### [x] 6. Derive rate-3 noise divider from the note data
- **Done (2026-09-18):** `SmpsToModConverter._derive_rate3_dividers` looks each rate-3 noise instrument's note (+ transpose) up in the driver's `PSGFrequencies` table — at the entry's `low` note when it has one, else the note it plays most, index 69 (`nMaxPSG`, divider 0) counting as 1 — and hands the divider to the PSG synthesiser; `convert.py` prints it. `tone2_n`, then `synth_root`, remain as overrides, but **no config states either any more**: `synth_root` removed from the nine rate-3 entries that had one (eight `A8` in six configs → derived 1; Marble Zone `C7` = N 53 → derived 34) and the hand-set `tone2_n: 1` from Title Screen and GHZ (byte-identical output). The `analyze.py` skeleton emits a comment with the derived value instead of a key.
- **Validated against every recording:** divider 0 on every hi-hat in the soundtrack; Marble Zone's derived per-note dividers match its VGZ note for note (17 ×20, 19 ×12, 22 ×18, 26 ×28, 31 ×10 …) apart from the five notes the disassembly flags as a data bug (they index past the table and read ROM garbage). Hi-hat 4–8 kHz band: about −11 dB → −5.2…−5.3 dB (hardware −4.6…−5.7). Volumes re-measured afterwards because the noise samples changed (two one-step changes).
- **Measured before:** `synth_root: A8` gave a 7 kHz LFSR (dull rattle) where the hardware plays near-white hiss.
- Nothing open.  (Inherent limit, not a todo: one static LFSR rate per sample — Marble Zone's pitched noise follows its melody by MOD playback speed.)

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
- **Was:** both functions used `2^(20−block)` where the chip and the driver's `MakeFMFrequency` (`f·2^21/fs`) are `2^(21−block)`: the analyzer read FM an octave high and the synth rendered an octave below the `synth_root` name, so configs tuned one against the other sounded right. FM pitches quoted before this date (including in `docs/audits/`) are one octave high.

### [x] 10. Coordination flags were applied one note early (parser)
- A note byte with no duration byte stayed "pending" while the flags after it were emitted, so `smpsSetvoice` / `smpsAlterPitch` / `smpsAlterVol` / `smpsNoteFill` hit the *previous* note. Fixed in `core/smps_parser.py` (flags, `smpsCall` and `smpsReturn` complete the pending note — matches `FMDoNext`'s put-back). Found by the GHZ audit: last intro note of FM3 played on voice $08 two octaves low. GHZ 52 cells / SBZ 7 cells changed; the GHZ config's one-note `B4` workaround instruments (7, 9 — 115 KB) were removed.

### [x] 11. Loop extension replayed flags written just before the jump label
- `_extend_looping_channels` chose the loop body by tick, so `smpsPSGAlterVol $FF` before `Mus85_SYZ_Jump03:` ran on every repetition: Spring Yard's hi-hat crept from attenuation 5 (−10 dB) to 0 within five loops and stayed there. The SYZ VGZ shows attenuation 5 throughout. Fixed with a per-channel label → event index from the parser. Changed SYZ (500 cells) and Marble Zone (2).

### [x] 12. Marble Zone noise instrument has no volume entry
- **Resolved by measurement (2026-09-18):** `psg_map[0xE7]` uses instrument 10, which had no `sample_list` line (so it played at the default 64) while a line for the unused instrument 11 carried the intended volume. Instrument 10 measured +7.7 dB and is now 13; the orphaned line is deleted.

### [ ] 13. Samples synthesised at the wrong pitch — 10 fixed, 4 songs left
- The survey's pitch audit found ten instruments whose every note was out by the same interval (nine by whole octaves, Star Light's PSG by two) — `synth_root` errors in seven configs, all corrected; wrong notes 1623 → 395. Left: **Drowning** (200; needs mid-song `smpsSetTempoMod` in the converter), **Invincibility** (64) and **Stage Clear** (22; one instrument shared across transpositions — needs a `channel_instrument_map` variant or range split), **Star Light** (48) and **Continue Screen** (31; scattered ±100–300 c). Details in `docs/audits/00_soundtrack_survey.md`.

---

## Tooling / workflow

### [x] `tools/vgm_compare.py` levels use L/R power, not a mono mix
Averaging to mono read hard-panned YM2612 channels ~5 dB low against centred ones (2.1 dB vs a power sum) while every MOD channel lost the same ~1 dB — GHZ FM4/FM5 looked hotter than they are and two GHZ volumes were over-corrected (since reverted). Title Screen (no pans) unaffected.

### [x] `tools/vgm_compare.py` — per-instrument level table and `--write-volumes`
Level error per MOD instrument with a per-channel / per-`Cxx` breakdown and the `sample_list` volume that zeroes it; `--write-volumes` applies them (≥ 1 dB), `--reuse-vgm` re-renders only the MOD. Anchored on the song's median note (DAC only when ≥ 2 dB quieter), clamps small excesses over 64 and scales everything together for large ones, refuses when channels disagree by > 3 dB or the error is > 18 dB. Alignment now comes from note starts (the envelope method was 0.9 s out on Chaos Emerald, 1.2 s on Drowning).

### [x] `tools/vgm_pitch_audit.py` — symbolic pitch audit (no rendering)
- [x] Aligns itself to the recording (note-start matching) and gives a verdict per instrument: "synth_root is 1 octave too high (243 of 243 notes)" vs "mixed". `--json` for scripting.
Chip frequency-register timeline vs the pitch each MOD note sounds at (from `root` / `synth_root` / finetune); sees legato pitch changes, ignores grace notes and vibrato steps below `--min-ms`; exit 1 on any wrong or missing note. GHZ 867/867, Title Screen 86/86.
- [x] Folded into `vgm_compare.py` as its **Pitch verdict** section (2026-09-18): `audit()` / `print_audit()` are shared, `--fail-pitch-cents` now tests the symbolic audit (plus notes silent in the MOD render), and `res["pitch_audit"]` is in the JSON. The per-note audio columns stay as a cross-check but are measured on the note's strongest partial and flag MOD-vs-VGM: GHZ 453 flags → 25, all grace notes (item 3). **Measured before:** FM voices with carrier multiples ≥ 2 have no energy at the register frequency, so both columns read −50…−110 c of leakage (GHZ FM1 median −46 c with every note right). Both tools also stop at the end of the MOD's single pass — the recording's second time round the loop was being judged against the MOD's last note (GHZ FM2 "F3 plays G3") and reported as `SILENT in MOD` (GHZ ×5, Marble Zone ×2).

### [x] `tools/vgm_compare.py` — rendered per-channel MOD-vs-VGZ audit
Per-note pitch and level, channel balance, onset timing, vibrato, noise spectrum, DAC rate. Needs VGMPlay and an ffmpeg build with libopenmpt.
- [x] `--json FILE` output plus opt-in CI thresholds `--fail-balance-db`, `--fail-pitch-cents`, `--fail-unmatched` (exit 1 when exceeded; results in the JSON `checks` list).
- [x] Vibrato rate/depth estimate on notes ≥ 0.5 s (heterodyne partial tracking, modulated stretch only). Title Screen FM4 closing A2: hardware 5.99 Hz ±19 c vs MOD 3.98 Hz ±36 c — feeds item 2.
- [x] Beating vs vibrato: rows are tagged `b` when the partial's level swings at the same rate (≥ 15 %) — that is two detuned FM carriers beating, not `smpsModSet`. **Correction:** the GHZ FM4/FM5 C6 rows (4.46 Hz vs 6.5 Hz) first recorded here as item 2 evidence are beating — those channels have no modulation at all. The rate differs because the MOD sample is resampled, so they belong to item 7 (multi-sampling / `synth_root`), as do the MOD-only 2.5–3 Hz rows on FM3/FM4.
- [x] PSG vibrato shows in the table (2026-09-18): `vgm_analyze._parse_vgm` starts a PSG note when the channel becomes audible or its period moves more than 70 cents (`psg_mod_cents`, `--psg-mod-cents`) from where the note started; smaller moves are modulation. Volume alone could not be the test — legato PSG notes change period with no silence between them. It also no longer judges the half-written period between the SN76489's two frequency bytes. GHZ PSG1 rows 277 → 72; `smpsModSet $0E,$01,$01,$03` reads 7.35 Hz ±7 c on hardware (theory 7.5 Hz) against 4.98 Hz in the MOD — second test case for item 2. **Measured before:** a modulated PSG note was chopped into frame-long "notes", so none reached the 0.5 s the table needs, and the per-note table listed the fragments (the per-instrument levels come out the same either way).
- [x] VGMPlay location: defaults to `reference/vgz/vgmplay/` (untracked) after `--vgmplay` / `VGMPLAY_DIR`; fresh-checkout setup in `docs/pipeline.md` § Verifying against a VGZ. Needs the 0.51.x (libvgm) line for the `Core = NUKE` ini key. No direct binary download URL is recorded — only the source repo could be confirmed.
- [ ] `--fail-unmatched` is noisy on sustained FM channels (MOD re-triggers where hardware ties notes → extra onsets; Title Screen FM2 reports 4). Match on key-on events instead of detected onsets for channels that have them.

### [x] `tools/vgm_analyze.py` — tone-2 divider on rate-3 noise rows, DAC seek events, per-channel counts
- [x] `reference/vgm/` paths in the CLAUDE.md examples and the tool docstring corrected to `reference/vgz/`.

### [x] Config authoring
- [x] `analyze.py` skeleton: FM and PSG `sample_list` volumes pre-computed (formula and fit in item 4), with a per-channel breakdown comment; rate-3 noise gets a comment with the divider the converter will derive, and pitched-noise channels get `low:`.
- [x] `convert.py` and `analyze.py --config` warn when a rate-3 noise entry without `tone2_n` has a `synth_root` outside the driver table (C3–Gs8). It fired on `synth_root: A8` in GHZ (fixed in its audit) and six more configs; since item 6 it fires on none.
- [x] `tests/baselines/title_screen_baseline.mod` regenerated (25 intended differences: 24× channel 6 `C13`→`C06`, channel 3 instrument 5→8 on the closing note). `regression_test.py --only NAME` added so one baseline can be refreshed alone.
