# Sound drivers

What a Mega Drive sound driver is, which ones sonic2mod reads, what they share, and how to add one.
For anyone converting a game's music or reading a new ROM.  Each game has its own page below.

On this page:
- [What a sound driver is](#what-a-sound-driver-is)
- [Supported games](#supported-games)
- [What every driver shares](#what-every-driver-shares)
- [How a ROM is recognised](#how-a-rom-is-recognised)
- [Probing a ROM](#probing-a-rom)
- [Adding a driver](#adding-a-driver)

## What a sound driver is

A Mega Drive game does not store its music as audio.  It stores a compact program per track — notes,
durations, instrument changes, volume and pitch commands — and a **sound driver**, code running on the
68000 or the Z80 once a frame, reads that program and writes the YM2612 (FM) and SN76489 (PSG)
registers.  The same song bytes sound different under a different driver: the driver decides what a
flag means, how long a tick lasts, how vibrato turns, what a rest does.

So sonic2mod reads a song **with its driver's rules**: it decodes the bytes as that driver does, then
plays them through one shared model of the chips.  Most games here use Sega's **SMPS** driver (Sample
Music Playback System) in one of its many versions; two use other engines.

## Supported games

| Game | Driver (`driver:`) | Family | CPU | Code | Configs | Page |
|------|--------------------|--------|-----|------|---------|------|
| Sonic the Hedgehog | `sonic1` (SMPS 68k Type 1b) | SMPS 68k | 68k | `core/drivers/smps68k/sonic1/` | `configs/sonic_1/` | [sonic_1.md](sonic_1.md), [smps_asm.md](smps_asm.md) |
| Michael Jackson's Moonwalker | `smps68k_type1a` | SMPS 68k | 68k | `core/drivers/smps68k/type1a/` | `configs/moonwalker/` | [moonwalker.md](moonwalker.md) |
| Golden Axe | `smpsz80_type0fm` | SMPS Z80 | Z80 | `core/drivers/smpsz80/type0fm/` | `configs/golden_axe/` | [golden_axe.md](golden_axe.md) |
| Streets of Rage | `smps68k_streets_of_rage` | SMPS 68k | 68k | `core/drivers/smps68k/streets_of_rage/` | `configs/streets_of_rage/` | [streets_of_rage.md](streets_of_rage.md) |
| Space Harrier II | `smpsz80_sh2` | SMPS Z80 | Z80 | `core/drivers/smpsz80/sh2/` | `configs/space_harrier_2/` | [space_harrier_2.md](space_harrier_2.md) |
| OutRun | `smpsz80_outrun` | SMPS Z80 | Z80 | `core/drivers/smpsz80/outrun/` | `configs/outrun/` | [outrun.md](outrun.md) |
| Streets of Rage 2 | `mucom_md` (Mucom-MD 0.85) | MUCOM | 68k | `core/drivers/mucom/md/` | `configs/streets_of_rage_2/` | [streets_of_rage_2.md](streets_of_rage_2.md) |
| Time Trax (prototype) | `timetrax` (Tim Follin's) | Time Trax | Z80 | `core/drivers/timetrax/` | `configs/time_trax/` | [time_trax.md](time_trax.md) |

The CPU is where the driver's sequencer runs; where a game plays DAC samples, a Z80 routine plays them.

Sonic 1 is the reference: its driver has a full disassembly, its songs can be read as assembly
([smps_asm.md](smps_asm.md)) or from the ROM, and every other driver is described by how it differs
from it.  Every other game is read from its ROM only (`input/roms/`, not in git), with a
[minimal config](../glossary.md#minimal-config) — `name`, `input_file`, `rom_song` — and the rest derived:

```yaml
# configs/golden_axe/81_wilderness.yaml (its sample_list volumes aside)
name: "Wilderness"
input_file: "input/roms/Golden Axe (World) (Rev A).md"
rom_song: "$81"
```

```bash
python convert.py configs/golden_axe/81_wilderness.yaml --show-config   # print what was derived
python convert.py configs/golden_axe                                     # every song of the game
```

Every non-Sonic game has a `rips.yaml` beside its configs pairing each song with its VGZ
[rip](../glossary.md#rip) (`reference/vgz/<game>/`, not in git), which the audit tools read
([../guide/verifying.md](../guide/verifying.md)).

## What every driver shares

```
 ROM bytes ──► driver (core/drivers/<family>/<driver>/)            one per game's sound engine
                 locate     where the songs, voices, tables are
                 grammar    track bytes ──► ops (notes, rests, flags)
                 rules      PlaybackRules: tables, envelopes, timing, TrackRules
                     │
                     ▼
               song IR (core/smps: SmpsSong + its PlaybackRules)  one vocabulary for every driver
                     │
                     ▼
               shared walk (core/smps/code.py), DriverState / resolve_note, converter, chip renders
```

- **The [song IR](../glossary.md#song-ir)** (`core/smps/`): every driver's song becomes the same `SmpsSong` — channels of
  notes, rests, ties and effects in SMPS terms.  A driver whose bytes mean something SMPS has no word
  for adds an effect to the IR (a glide, an ADSR envelope, a soft release), never a driver check
  downstream.
- **[PlaybackRules](../glossary.md#playbackrules)** (`core/smps/rules.py`): what the driver does that
  the notes alone do not say.  Tables (`fm_frequencies`, `psg_frequencies`, `psg_read`), envelopes
  (`psg_envelopes`, `pitch_envelopes`, `adsr_presets`), drum names (`dac_names`, `dac_pans`), timing
  (`update_clock`, `tempo_phase`, `key_run_out`), and per kind of track (FM, PSG, drum track) a
  **[TrackRules](../glossary.md#trackrules)**: volume, key-off, ties, modulation and pitch switches.
  Every switch defaults to Sonic 1's; a driver states only where it departs.  A song carries its
  rules (`song.rules`); nothing below `core/drivers` holds a driver's table.
- **The [`SmpsVariant`](../glossary.md#smpsvariant)** (`core/rom/variant.py`): one driver's description — memory map, `locate`,
  flag tables per kind of track, `grammar`, header and voice layouts, envelope commands, `rules`,
  `rules_from_rom` (the ROM's own tables read in), `dac`, `fm_drums`, and its own readers where its
  data is not SMPS's.  The ROM framework (`core/rom/`) is driven by it: no reader asks which driver it
  reads.
- **Families** (`smps68k/`, `smpsz80/`, `mucom/`, `timetrax/`): what two drivers of a family share
  (the 68k's relative pointers, the Z80's bank window) lives in the family folder.  A driver imports
  the framework absolutely (`core.rom...`) and its family relatively; no driver imports another, and
  only `core.source` reaches `core.drivers` (`pyproject.toml` `[tool.importlinter]`).

## How a ROM is recognised

`core/drivers/games.py` pins every known ROM by SHA-1 to its driver (and any data fixes, as byte
edits: Sonic 1's three).  Any other ROM is tried with every driver (`core/drivers/detect.py`): the one
that locates the songs and decodes every song — each flag known, no code starting inside an
instruction, every pointer in the ROM — is it.  None or several stop with what each attempt hit; then
state `driver:` in the config.

## Probing a ROM

The tools in `tools/rom/` read a driver's code and data.  Disassembly needs `pip install capstone
z80dis` (capstone has no Z80).

| Tool | What it shows | Example |
|------|---------------|---------|
| `rom_import.py` | the sound indexes; each song vs its asm (`--compare`); SMPS2ASM text (`--asm`); DAC samples (`--dac`); `--driver` to force one, `--shipped` without data fixes, `--only 81 8A` | `python tools/rom/rom_import.py input/roms/sonic_rev01.bin` |
| `song_dump.py` | every song as read, walked and played, as text, no rendering (`--only`, `--shipped`) | `python tools/rom/song_dump.py "input/roms/Golden Axe (World) (Rev A).md" --only 81` |
| `disasm.py` | a routine to its end (68k, or `--z80` the Z80 program as the 68k loads it); `--table` a jump table's targets (`--entries`, `--first`, `--longs`); `--find` every match of a code pattern; `--raw` every byte | `python tools/rom/disasm.py "input/roms/Streets of Rage 2 (USA).md" --table 41B74 --entries 16 --first F0` |
| `flag_census.py` | every flag the songs hold, counted by its bytes and what it reads as (`--flag`, `--kind`, `--only`, `--rows`) | `python tools/rom/flag_census.py "input/roms/Streets of Rage 2 (USA).md" --flag FF FA --kind PSG` |

A code pattern for `--find` is a listing, one instruction a line (or split by `;`): its hex bytes, then
anything (the mnemonic).  `?` is any byte, `?n` any n bytes, `<b>` / `<w>` a captured byte / word
(`core/rom/pattern.py`):

```bash
python tools/rom/disasm.py "input/roms/Time Trax (USA) (Proto).md" --z80 --find "3A <w> ld a,(table hi); 47; 3A ? ?; 4F"
```

Every `locate.py` finds its tables this way — by the code that reads them, not by one game's
addresses — so another revision of the game reads too.

## Adding a driver

1. **Probe.**  Find the driver (68k code, or the Z80 program the 68k copies), its flag jump table, the
   song index, a song header and a voice.  Compare each table with Sonic 1's
   (`core/drivers/reference.py`).
2. **Code.**  A folder in its family (`core/drivers/<family>/<driver>/`): `variant.py` builds the
   `SmpsVariant`, its `PlaybackRules` being `SONIC1_RULES` with what differs replaced; plus whatever
   only this driver has (`locate.py`, `dac.py`, drums...).  What two drivers of a family share moves up
   to the family folder.
   - Name it in `core/drivers/names.py` (`SmpsDriver`) and `registry.py` (loaded on first use; a family
     `__init__` imports no driver).  Each ROM it is known in, with any data fixes, goes in `games.py`.
   - Nothing outside its folder names it: what the song must say goes in the IR (`core/smps`), what a
     reader needs in the variant's description.  Never a variant check.
   - Its PSG table's pitch comes from the table itself (`PlaybackRules.psg_table`: Space Harrier II's
     starts at A2, the rest at C3); a table that is not chromatic is an error.
3. **Register it with the tests.**
   - Its folder in `pyproject.toml`'s "drivers: none imports another" contract (`tests/test_layers.py`
     fails until it is).
   - Its ROM, rips and configs folder as a `GameFiles` line in `tests/roms.py` `GAMES` (the `read_`,
     `frames_` and `glitches_` tool cases follow).
   - `tests/core/drivers/test_contract.py` then holds it to the contract: its ROM pinned to it, no
     other driver reading its songs (and it no other game's), every song read.  It fails until the
     driver's line in `_REVIEWED` lists where it plays differently from Sonic 1 — every
     `PlaybackRules` and `TrackRules` switch not stated there is Sonic 1's, so check each against the
     driver's code.
4. **Check.**  `tools/rom/rom_import.py` lists every song; `tools/vgm/vgm_lift.py` against the rips
   (FM is what it reads in full); `tools/vgm/vgm_frames.py`; `tools/vgm/vgm_pitch_audit.py`;
   `tools/vgm/measure_volumes.py` ([../guide/verifying.md](../guide/verifying.md)).

> **Note:** A rip is only as good as its emulator and ripper.  Back every finding with the driver's
> code; a rip that shows a driver bug is evidence of the bug, not a target to match.
