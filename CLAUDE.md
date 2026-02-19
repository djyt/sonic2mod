# sonic2mod — SMPS-to-MOD Converter

Converts Sonic 1 SMPS assembly music files to Amiga MOD format.

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
  output/            # Generated .mod files
  mml2mod-master/    # Reference project (MML-to-MOD converter)
  docs/              # Technical documentation
```

## Quick Usage

```bash
# Default settings (10 channels, 150 BPM, ticks_per_row=6)
python convert.py "C:/coding/sonic_1/source_s1disasm-AS/sound/music/Mus8A - Title Screen.asm" --output output/title_screen.mod

# With YAML config for per-channel control
python convert.py "C:/coding/sonic_1/source_s1disasm-AS/sound/music/Mus8A - Title Screen.asm" --config configs/title_screen.yaml

# CLI overrides
python convert.py "path/to/song.asm" --bpm 140 --channels 10 --transpose -36
```

## Pipeline

`SmpsParser.parse_file()` → `SmpsSong` → `SmpsToModConverter.convert()` → `ModFile` → `.mod`

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

| SMPS                | MOD       | Notes                           |
|---------------------|-----------|---------------------------------|
| `smpsNoteFill N`    | `ECx`     | Note cut after x ticks          |
| `smpsModSet`        | `4xy`     | Vibrato (speed→x, depth→y)     |
| `smpsAlterVol`      | `Cxx`     | Cumulative volume → set volume  |
| `smpsAlterNote`     | transpose | Per-channel pitch offset        |
| `smpsJump`          | `Bxx`     | Position jump (song loop)       |
| `smpsNop/PSGform`   | ignored   | No MOD equivalent               |

## Known Limitations

- Notes clamped to C1–B3 with warnings when out of range after transpose
- Samples are 2-byte silent placeholders — replace in a tracker (OpenMPT/MilkyTracker)
- `smpsPan` informational only (MOD panning is channel-based, not per-note)
- Song loop (`smpsJump`) only sets Bxx from the first channel that has a jump

## voice_instrument_map (per-voice octave-range instrument routing)

Routes SMPS voice index + **source-note range** → MOD instrument + optional pitch anchor.
Ranges are checked against `(note_value − $81) + smpsAlterNote` — before channel transpose.

- `low`/`high` — SMPS note names without `n` prefix, parsed by `parse_smps_note()` in `tables.py` (e.g. `G5`, `Gs6`, `C7`)
- `root` — ModNote enum name where source `low` plays (`F2s`, `G3`, `A2`, etc.); output = `root + (source − low)`, clamped C1–B3
- When `voice_instrument_map` covers all notes for a channel, set `transpose: 0` — `root` handles pitch placement entirely

```yaml
voice_instrument_map:
  0:                        # voice index (from smpsSetvoice)
    - low:  G5              # SMPS source note — bottom of range
      high: G6
      instrument: 4
      root: F2s             # G5 plays at F#2; each semitone above shifts output up by 1
    - low:  Gs6
      high: C7
      instrument: 12
      root: G3
```

## Testing

Verified against `Mus8A - Title Screen.asm`:
- All 9 channels parsed (1 DAC, 5 FM, 3 PSG)
- FM5 fall-through into FM1 data works
- PSG3 loop unrolled correctly (5 iterations)
- Output is valid 10CH MOD, opens in OpenMPT/MilkyTracker
