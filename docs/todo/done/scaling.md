# Scaling to many drivers and songs

Reviewed 2026-10-09, before Streets of Rage Phase 2 (`streets_of_rage.md`).  Goal: hundreds of
ROMs, MAME-like (a framework driven by per-driver descriptions), and a test suite whose cost
follows the code a change touches, not the number of songs.

Status key: `[ ]` open, `[x]` done.

## 1. Review findings

1. Every driver is imported on every run (`core/drivers/registry.py`): an asm Sonic song loads
   all four.  Slow with hundreds, and no test can tell which driver a case needs.
2. A game is not a driver: `SmpsVariant.known_roms` (SHA-1, data fixes) lives in each driver.
3. Sonic 1 as a hidden base: Type 1a, Type 0 FM and MUCOM build their rules with
   `replace(SONIC1_RULES, ...)` and inherit Sonic's tables and envelopes unstated.
4. Layer holes: `config/song.py`, `audit/rip_diff.py` import `drivers.names` (past `source`);
   `plan/derive.py` calls `source.read_dac` (past `config`).
5. Sonic 1 facts in the IR: `SMPS_DAC_NAMES`, `SFX_CHANNEL_IDS` (`core/smps/names.py`).
6. The IR is SMPS-shaped; non-SMPS concepts enter named by meaning.  A non-SMPS family would
   rename the framework away from `Smps*`.  No action now.
7. agents.md: module-only names public (`HEADER_MUCOM`, `VOICE_MUCOM`, SHA-1 constants,
   `locate_mucom`, `FIRST_FLAG` / `END`, `SMPS_OPERATOR_OFFSETS`); `_entries_are` twice,
   `_is_music_header` wrappers x3, MUCOM's four one-line wrappers; stale references (`mucom.py`,
   `mucom_grammar.py`, `smps68k/sonic1/tables.py`, `core/smps/__init__`'s "Sonic 1").
8. Moonwalker and Golden Axe: no MOD regression, no read snapshot.

## 2. Tests

- **Read snapshots**: every song of every ROM (and Sonic's asm) as read, walked and played, as
  text; no rendering.  Cases of `tests/tool_regression.py`.
- **Cases as data**: `tests/cases.yaml`.  Sonic stays complete; each other driver 2-3 cases
  chosen for what they cover.  A case needing a ROM skips without it.
- **Selection by coverage**: `--generate-baselines` records the files each case executed
  (coverage.py); a run picks the cases that executed a changed file.  A change the record cannot
  place (C sources, settings, the test code) runs everything.  Needs finding 1 fixed.

## 3. Plan

- [x] 1. Read snapshots (gates the refactors after it): `tools/song_dump.py`, tool_regression
  `read_*` (~2 s for every song of the four games).
- [x] 2. Lazy driver registry (`load_driver`); `core/drivers/games.py` (findings 1, 2).
- [x] 3. `tests/cases.yaml`, `tests/selection.py`; Moonwalker and Golden Axe three cases each.
  A driver's folder moves only its game's cases.  Shared modules move every case: a module's
  top level runs on import, and `core` imports its packages eagerly.  Finer, if wanted: line
  level (diff hunks against executed statements).
- [x] 4. Findings 3 (each driver's `PlaybackRules` stated; Golden Axe has no PSG tables), 4
  (`source` re-exports `SmpsDriver`; `ConversionConfig.read_dac`), 7.  Finding 5 kept: the DAC and
  channel names are SMPS2ASM's (the asm dialect's vocabulary, like note names), not a driver's.
- Later: Streets of Rage cases (decision 4 revisited, after its Phase 4).
