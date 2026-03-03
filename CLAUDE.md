# sonic2mod — SMPS-to-MOD Converter

Converts Sonic 1 SMPS assembly music files to Amiga MOD format.

## Documentation Index

| Document | Contents |
|----------|----------|
| `docs/smps_driver.md` | **Sonic 1 driver reference** — all coord flag bytes ($E0–$F9), smpsDetune vs smpsChangeTransposition, timing system, smpsModSet, smpsNoteFill, FM operator order, DAC, PSG |
| `docs/pipeline.md` | **Conversion pipeline** — SMPS→MOD effect mapping (full table), tick/row math, effect priority, voice_map routing decision tree, BPM derivation, common gotchas |
| `docs/smps_format.md` | Assembly format syntax — header macros, dc.b token types, all effect macros |
| `docs/yaml_config.md` | Full YAML schema — all config fields, voice_map, sample_list, BPM formula |
| `docs/architecture.md` | Module descriptions — IR data classes, parser stages, ModFile layout |
| `docs/effects.txt` | ProTracker MOD effect reference |
| `reference/Nuked-OPN2/` | Cycle-accurate YM2612/YM3438 C emulator |
| `reference/mml2mod-master/` | Reference MML-to-MOD converter |

## Project Structure

```
sonic2mod/
  tables.py          # Note lookup tables, SMPS↔MOD note mapping
  mod.py             # MOD file writer (adapted from mml2mod-master)
  smps_parser.py     # SMPS assembly parser → intermediate representation
  config.py          # Per-song conversion config, YAML loading
  smps2mod.py        # Conversion engine (IR → MOD)
  convert.py         # CLI entry point
  configs/           # YAML config files per song
  configs/settings.yaml  # Global synthesis settings
  output/            # Generated .mod files
  ym2612/            # YM2612 sample synthesis package (all segments complete)
    build.py         #   Auto-compiles ym3438.c → ym2612/ym3438.dll (gcc or cl)
    wrapper.py       #   ctypes OPN2 class — write_reg, key_on/off, render_samples
    voice.py         #   SmpsVoice → YM2612 register writes (program_voice)
    renderer.py      #   SmpsVoice + mod_note_index → 8-bit PCM (render_note)
    sample_generator.py #  voice_map → {inst: (pcm, rate)} dict (generate_fm_samples)
    validate.py      #   Standalone test: python ym2612/validate.py
  docs/              # Technical documentation
  sonic_1/           # Sonic 1 source files (driver asm, music, DAC samples)
```

## Setup

```bash
pip install pyyaml   # only external dependency
```

## Quick Usage

```bash
# Default settings (10 channels, 150 BPM, ticks_per_row=6)
python convert.py "C:/coding/sonic_1/source_s1disasm-AS/sound/music/Mus8A - Title Screen.asm" --output output/title_screen.mod

# With YAML config for per-channel control
python convert.py "C:/coding/sonic_1/source_s1disasm-AS/sound/music/Mus8A - Title Screen.asm" --config configs/title_screen.yaml

# CLI overrides
python convert.py "path/to/song.asm" --bpm 140 --channels 10 --transpose -36

# Verify: open output .mod in OpenMPT or MilkyTracker
# Smoke-test synthesis pipeline (writes output/validate_test.raw — load in Audacity):
python ym2612/validate.py
```

## Pipeline

`SmpsParser.parse_file()` → `SmpsSong` → `SmpsToModConverter.convert()` → `ModFile` → `.mod`

See `docs/pipeline.md` for the full data flow and conversion decisions.

## Key Conventions

- SMPS note range: 8 octaves (C0–B7), byte values $81–$DF
- MOD note range: 3 octaves (C1–B3), 36 semitones
- Default FM transpose: -36 semitones (maps SMPS octaves 3–5 → MOD C1–B3)
- Duration persistence: last explicit `dc.b` duration carries to subsequent notes
- Standalone duration bytes in `dc.b` advance the tick counter (implicit wait/sustain)
- Parser continues past label boundaries — only stops at `smpsStop`/`smpsJump`
- Loop unrolling uses `stop_line` parameter to prevent re-entry into `smpsLoop`
- YAML config requires `pyyaml` (`pip install pyyaml`)

## SMPS Effect → MOD Effect Mapping

Full table with gotchas in `docs/pipeline.md`. Quick reference:

| SMPS | Byte | MOD | Notes |
|------|------|-----|-------|
| `smpsAlterVol` | $E6 | `Cxx` | Cumulative volume → set volume |
| `smpsModSet` | $F0 | `4xy` | Vibrato; steps halved in hardware |
| `smpsModOn` | $F1 | `4xy` | Re-activates stored mod params |
| `smpsModOff` | $F4 | (clear) | No MOD output |
| `smpsNoteFill` | $E8 | `ECx` | Note cut; only values 1–15 representable |
| `smpsJump` | $F6 | `Bxx` | Position jump; first occurrence only |
| `smpsSetvoice` | $EF | (routing) | Updates voice_map instrument lookup |
| `smpsChangeTransposition` | $E9 | (pitch) | Adds to total_transpose |
| `smpsDetune`/`smpsAlterNote` | $E1 | **none** | FNUM offset (~10 cents); NOT semitones, NOT applied to pitch |
| `smpsPan` | $E0 | ignored | MOD panning is channel-based |
| `smpsLoop` | $F7 | (unrolled) | Loop replayed at parse time |
| `smpsCall` | $F8 | (inlined) | Subroutine events spliced inline |
| `smpsNop`, `smpsPSGform`, `smpsPSGvoice` | $E2,$F3,$F5 | ignored | No MOD equivalent |

**Effect priority (one per row):** volume (Cxx) > vibrato (4xy) > note cut (ECx).

## Critical Gotchas

**Full gotchas with causes and fixes in `docs/pipeline.md`.**

1. **`smpsDetune`/`smpsAlterNote` ($E1) is NOT semitones** — it's a raw FNUM offset (~10 cents per unit). Does NOT affect `voice_map` range lookup or pitch placement. For chorus detune (FM5 vs FM4), use `channel_instrument_map` with a `finetune: 1` instrument variant.

2. **`root` is unconditional** — `smpsChangeTransposition` events do NOT affect the root path. Do NOT use `root` on channels that use `$E9` mid-song; use the `total_transpose` path instead (omit `root`, rely on YAML `transpose`).

3. **`smpsChangeTransposition` ($E9) is cumulative semitones** — each call adds to `SMPS_Track.Transpose`. Affects all subsequent notes and is included in `total_transpose`. This IS what shifts channels between register ranges in GHZ.

4. **Operator order** — SMPS binary stores OP4,OP3,OP2,OP1 (reversed). Correct mapping: `_SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)`. Wrong mapping → "overdriven guitar" distortion (OP1 carrier placed in self-feedback slot).

5. **Synthesis disabled by default** — `synthesis.enabled: false` in `configs/settings.yaml`. Set `true` to auto-generate FM samples (requires gcc/MSVC for ym3438.c).

6. **FM5 falls through into FM1 data** — parser does not stop at label boundaries; FM5 typically lacks `smpsStop` and shares FM1's note data (intentional chorus/detune design).

7. **`smpsNoteFill` > 15 ignored** — ECx has a 4-bit parameter; fill values 16+ cannot be represented and produce no MOD effect (note sustains naturally to full duration).

8. **smpsModSet step count halved in hardware** — driver does `lsr.b #1` before storing. Value 16 → 8 actual oscillation steps.

## voice_map (per-voice octave-range instrument routing)

Routes SMPS voice index + **source-note range** → MOD instrument + optional pitch anchor.
Ranges checked against `(note_value − $81)`. `smpsDetune`/`smpsAlterNote` is a raw FNUM offset
and does NOT affect range lookup.

- `low`/`high` — SMPS note names without `n` prefix (e.g. `G5`, `Gs6`, `C7`)
- `mod_instrument` — MOD instrument slot (1-based)
- `root` — **absolute** MOD note anchor; source `low` always plays here regardless of smpsAlterPitch or pitch_offset
- `synth_root` — synthesis pitch override; `target_rate` is NOT adjusted — output pitch = synth_root's frequency
- Output formula: `root + (source − low)`, clamped C1–B3
- Rootless entries use channel-transpose path: `smps_note + total_transpose`
- When `voice_map` covers all notes for a channel, set `transpose: 0`

```yaml
voice_map:
  0:                        # voice index (from smpsSetvoice)
    - low:  G5              # SMPS source note — bottom of range
      high: G6
      mod_instrument: 4
      root: F2s             # G5 plays at F#2; each semitone above shifts output up by 1
      synth_root: C6        # (optional) synthesize at C6 frequency instead of G5
    - low:  Gs6
      high: C7
      mod_instrument: 12
      root: G3
```

**When NOT to use `root`:** channels with mid-song `smpsChangeTransposition` ($E9) — root ignores total_transpose and will place notes incorrectly. Use `transpose` only and let `total_transpose` handle pitch.

## sample_list Entry Format

`[inst_num, "filename.raw", volume, finetune]` — loaded from `samples_dir`.
- `inst_num`: 1-based MOD instrument slot
- `volume`: 0–64
- `finetune`: -8..+7 (MOD finetune nibble; +1 ≈ +12.5 cents)
- Synthesis path: entries with `inst_num` in `fm_samples` apply finetune only (file not loaded)

## YM2612 Synthesis (Segments 1–5 — all complete)

All segments complete. Enable synthesis: set `synthesis.enabled: true` in `configs/settings.yaml`.
**Sample generator:** `python ym2612/sample_generator.py` → `output/sample_gen_test.raw`.
`generate_fm_samples(song, config, synth)` → `{inst_num: (pcm_bytes, target_rate_hz)}`.

**FM synthesis pitch:**
- `synth_note_idx = entry.low - 12` (or `entry.synth_root - 12` if set) — YM2612 synthesizes at this SMPS note's frequency
- `target_rate = round(amiga_clock / PERIOD_TABLE[entry.root.value])` — always; no compensation applied for `synth_root`
- Output pitch = synthesis pitch (= `low` when no synth_root, = `synth_root` otherwise)
- FM timbre is pitch-dependent — synthesizing 3+ octaves lower produces unrecognisable sound
- SMPS semitone offset: semitone 0 = C0; `render_note` idx 0 = C1 → `synth_note_idx = entry.low - 12`
- For channels with smpsAlterPitch, set `synth_root` to the chip's actual pitch

**Fallback synthesis:** `synth_idx=48` (C5, 523 Hz). For voices with source notes ≥ G6 and algorithm 4, add an explicit `voice_map` entry with correct `low` to avoid brightness loss.

**Validate:** `python ym2612/validate.py` — renders A4 tone, prints SUCCESS/WARNING.
**Renderer validate:** `python ym2612/renderer.py` — renders voice 1 at A3 (220 Hz).
Smoke test raw files use **true 16-bit PCM** — do NOT upscale from 8-bit (×256).

**render_note API:** `render_note(voice, mod_note_index, sustain_secs=1.5, release_secs=0.5,
target_rate=None, opn2=None, channel=0) → (bytes, int)` — always resets OPN2 internally.

**Critical OPN2_Clock timing:** In YM2612 mode, `OPN2_Clock()` time-multiplexes 6 channels
across 24 internal clocks. `mol`/`mor` is `audio×3` at the 6 output-enable clocks
(`cycles & 3 == 3`), and `sign×3` (≈ ±3 DC bias) at all other clocks. `render_samples()`
**accumulates all 24 values per sample** and subtracts `DC = 72` (24×3) to zero-centre.
Do NOT take only the last clock's value — it captures DC bias, not audio.

**OPN2 register write:** Each `write_reg()` call clocks 24× after address and 24× after data.
Key-on (reg 0x28): bits[6:4]=operator mask, bits[2:0]=channel (ch 3-5 map to 4-6).
Bank 0 = ch 0-2 (ports 0/1), Bank 1 = ch 3-5 (ports 2/3).

**Critical SMPS operator → YM register offset mapping:** `_SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)`
— SMPS OP1→YM offset 0x0C, OP2→0x04, OP3→0x08, OP4→0x00. Do NOT use (0x00, 0x08, 0x04, 0x0C).
Source: `s1.sounddriver.asm` FMInstrumentOperatorTable + `_smps2asm_inc.asm` smpsDcb (else/non-v2 branch).

**Headroom / carrier balance:** `configs/settings.yaml` `headroom_db: 6.0`, `carrier_balance: true`.
Boosts carrier TL at register-write time to prevent YM2612 DAC saturation before Python normalization.

## Testing

Verified against `Mus8A - Title Screen.asm`:
- All 9 channels parsed (1 DAC, 5 FM, 3 PSG)
- FM5 fall-through into FM1 data works
- PSG3 loop unrolled correctly (5 iterations)
- Output is valid 10CH MOD, opens in OpenMPT/MilkyTracker
