# Verifying against a VGZ

How to check a converted MOD against a recording of the real game: a **VGZ** rip (a compressed VGM:
the log of every register write the game made to its sound chips).  For anyone tuning a song config
or changing the converter.  Set up once, then pick the tool by the question you are asking: the
table below.  Run the symbolic checks (`vgm_pitch_audit`, `vgm_lift`, `vgm_frames`) first: they need
no rendering and are exact; `vgm_compare` renders audio for what only the ear-level picture shows
(levels, vibrato, noise).

On this page:

- [Setup](#setup)
- [Which tool](#which-tool)
- [Pairing configs and rips](#pairing-configs-and-rips)
- [Is every note right: `vgm_pitch_audit`](#is-every-note-right-vgm_pitch_audit)
- [Does the song read right: `vgm_lift`](#does-the-song-read-right-vgm_lift)
- [Pitch, level, voice per frame: `vgm_frames`](#pitch-level-voice-per-frame-vgm_frames)
- [The rip's own faults: `rips.yaml`](#the-rips-own-faults-ripsyaml)
- [The rendered audit: `vgm_compare`](#the-rendered-audit-vgm_compare)
- [Every song's volumes: `measure_volumes`](#every-songs-volumes-measure_volumes)
- [Reading a rip: `vgm_analyze`](#reading-a-rip-vgm_analyze)
- [Checking a MOD alone](#checking-a-mod-alone)

---

## Setup

`reference/vgz/` is untracked: it holds the rips and VGMPlay.

1. Put the rips in `reference/vgz/<game>/`, the folder name the game's configs have under `configs/`
   (Sonic 1's: `reference/vgz/sonic_1/`), unless the game's `rips.yaml` names another
   ([Pairing](#pairing-configs-and-rips)).
2. Unzip a **VGMPlay 0.51.x** Windows build (<https://github.com/ValleyBell/vgmplay-libvgm>) into
   `reference/vgz/vgmplay/`: `VGMPlay64.exe` or `VGMPlay.exe`, `VGMPlay.ini`, `zlib1.dll`.  The 0.51
   line is required: the tools write `Core = NUKE` and `MuteMask = …` into `VGMPlay.ini`, which 0.40.x
   lays out differently.  Looked up as `--vgmplay DIR`, then the `VGMPLAY_DIR` environment variable,
   then `reference/vgz/vgmplay/`.
3. An **ffmpeg** on PATH built with the libopenmpt demuxer (check: `ffmpeg -h demuxer=libopenmpt`;
   the gyan.dev *full* build has it), and `pip install numpy`.

Only the tools that render audio need these: `vgm_compare` and `measure_volumes` need 2 and 3,
`mod_render_diff` only 3.  The symbolic tools need just the rips.

Reference renders are cached in `samples.render_cache` (shipped `output/cache`; VGMPlay renders under
`vgmplay/`), keyed on the rip and `VGMPlay.ini`: each song is rendered once.

---

## Which tool

| Question | Tool | Example |
|----------|------|---------|
| Is every MOD note at the right pitch? | `vgm_pitch_audit.py` | `python tools/vgm/vgm_pitch_audit.py configs/sonic_1/02_green_hill_zone.yaml --list` |
| Every song of a game, pitch only | `vgm_pitch_audit.py --all` | `python tools/vgm/vgm_pitch_audit.py --all --configs configs/streets_of_rage_2 --convert` |
| Why was this note judged wrong? | `vgm_pitch_audit.py --trace` | `python tools/vgm/vgm_pitch_audit.py configs/streets_of_rage_2/11_under_logic.yaml --trace PSG2 --at 29.9` |
| Glides and slides right too? | `vgm_pitch_audit.py --slides` | `python tools/vgm/vgm_pitch_audit.py configs/sonic_1/02_green_hill_zone.yaml --slides --list` |
| Does the song as read (asm / ROM) play what the rip recorded — onsets, lengths, notes? | `vgm_lift.py` | `python tools/vgm/vgm_lift.py "reference/vgz/sonic_1/02 - Green Hill Zone.vgz"` |
| Detune, level, voice, pan on each note's frame? | `vgm_frames.py` | `python tools/vgm/vgm_frames.py configs/golden_axe/89_the_battle.yaml` |
| Did the rip drop a frame? | `vgm_frames.py --glitches` | `python tools/vgm/vgm_frames.py --all --glitches --configs configs/space_harrier_2` |
| Are the instrument volumes right?  Balance, vibrato, timing, noise, DAC rate? | `vgm_compare.py` | `python tools/vgm/vgm_compare.py configs/sonic_1/01_title_screen.yaml "reference/vgz/sonic_1/01 - Title Theme.vgz"` |
| Set every song's volumes at once | `measure_volumes.py` | `python tools/vgm/measure_volumes.py --configs configs/moonwalker` |
| What does the rip actually write? | `vgm_analyze.py` | `python tools/vgm/vgm_analyze.py "reference/vgz/sonic_1/01 - Title Theme.vgz" --chip psg --channel NOISE` |
| Can a ProTracker player sound every note? | `mod_lint.py` | `python tools/mod/mod_lint.py output/sonic_1/02_green_hill_zone.mod` |
| Are the samples the right length and size? | `mod_audit.py` | `python tools/mod/mod_audit.py output/sonic_1/02_green_hill_zone_merged.mod --banks` |
| Do two MODs sound the same? | `mod_render_diff.py` | `python tools/mod/mod_render_diff.py before.mod after.mod` |
| The merged (Amiga) build against the rip | `vgm_compare.py --merged` | [amiga_builds.md § Checking](amiga_builds.md#checking-a-merged-build) |

Where a difference shows tells you whose it is:

```
 vgm_lift / vgm_frames differ    → the song as read (the reader) or the lift
 only vgm_pitch_audit / vgm_compare differ → the converter
```

> **Pitfall:** the MOD audits read the MOD on disk.  A stale MOD is the commonest false result:
> re-convert first, or pass `--convert` to `vgm_pitch_audit.py`.

> **Note:** a rip is only as good as its emulator and ripper.  Corroborate a surprising difference
> with the driver's code before "fixing" a song to match it.

---

## Pairing configs and rips

Every rip tool finds a config's rip the same way (`core/audit/rips.py`):

- by number: `configs/sonic_1/02_green_hill_zone.yaml` ↔ `reference/vgz/sonic_1/02 - Green Hill Zone.vgz`;
- or by the `rips.yaml` beside the configs, `{config stem: rip file}`, where numbers differ
  (Moonwalker's rips are in game order, its configs in sound-ID order).  Its `folder:` key names the
  rips' folder under `reference/vgz/` when it is not the configs' mirror (Streets of Rage:
  `folder: streets_of_rage_1`).

```yaml
# configs/golden_axe/rips.yaml (excerpt)
81_wilderness: "02 - Wilderness.vgz"
84_path_of_fiend:
  rip: "07 - Path of Fiend.vgz"
  glitches: [...]        # the rip's own faults: see below
```

The rip tools share `--configs DIR` (default `configs/sonic_1/`), `--vgz-dir DIR` and `--rips FILE`.

---

## Is every note right: `vgm_pitch_audit`

The authority on pitch.  Symbolic, no rendering, self-aligning: the chip's frequency registers
(every YM2612 frequency write and key on/off, every SN76489 tone write) against the pitch each MOD
note sounds at, computed from the config.  Exit 1 on a wrong or missing note.

```bash
python tools/vgm/vgm_pitch_audit.py configs/sonic_1/01_title_screen.yaml
```

```
VGM 8.8 s   MOD one pass 9.1 s   MOD lags by +250 ms (auto)   (output\sonic_1\01_title_screen.mod)
chip segments >= 60 ms; wrong = more than 35 cents from the chip

FM1   ok   16   wrong   0   missing   0   (+0 shorter than 60 ms)
FM4   ok   16   wrong   0   missing   0   (+108 shorter than 60 ms)
```

The per-instrument verdict says what to fix: `inst 8: synth_root is 1 octave too high (243 of 243
notes)` — fix that instrument's [synth_root](../glossary.md#synth_root); `mixed` — a note problem.

| Flag | Default | Effect |
|------|---------|--------|
| `vgz` (2nd positional) | the config's rip | the recording |
| `--all` |  | every pair of `--configs`, a line each, in parallel (`--only NAME ...` narrows) |
| `--convert` |  | write each MOD first |
| `--mod FILE` | config `output_file` | the MOD to audit |
| `--list` |  | every wrong / missing segment with its time |
| `--min-ms MS` | 60 | ignore chip segments shorter than this (grace notes, vibrato steps) |
| `--tolerance C` | 35 | cents before a note counts as wrong |
| `--slides [C]` | off; 50 when given bare | judge glides: runs of segments shorter than `--min-ms`, each step within C cents of the MOD's `1xx` / `2xx` |
| `--trace CH --at S` |  | how CH's chip segments within ±0.5 s of S were paired and judged |
| `--offset S` | found from note starts | seconds the MOD lags the rip |
| `--json FILE` |  | alignment and verdicts as JSON |
| `--variant NAME`, `--settings PATH` |  | the build the MOD was made with |

---

## Does the song read right: `vgm_lift`

Does the song as read — asm or ROM bytecode — play what the rip recorded?  No MOD, no audio: the rip
is **[lifted](../glossary.md#lift)** (turned back into a song: notes, durations, ticks) and both are
compared note by note as the driver plays them.  A difference here is the reader's or the lift's,
never the converter's.

```bash
python tools/vgm/vgm_lift.py "reference/vgz/sonic_1/01 - Title Theme.vgz"   # a rip: its config found
python tools/vgm/vgm_lift.py configs/moonwalker/88_round_clear.yaml           # a config: its rip found
python tools/vgm/vgm_lift.py rip.vgz --input "input/roms/X.md" --rom-song '$88'   # any song
python tools/vgm/vgm_lift.py --all --aspects onset                           # every rip, a line each
```

```
  only the rip: PSG1 PSG2
  DAC     30 notes   same
  FM1     16 notes   same
  PSG3    24 notes   onset 1  length 4  note 24
            48    1.00s  note       0x0001 (A12) -> 0x0356 (C3)   x24, last at 324 (6.75s)
```

- **Aspects:** what the lift reads — `onset`, `length`, `note` (`--aspects` picks).  Pitch, level,
  voice and pan: [`vgm_frames`](#pitch-level-voice-per-frame-vgm_frames).
- **Verdict per channel kind:** FM is what the lift reads in full, so an FM difference is the song's;
  a DAC or PSG difference may be the lift's.  `--all` ends with the counts.
- **Tempo:** the lift is given the song's tempo modifier and divider; it infers one only where the
  song fits no schedule, and says so (`--infer-tempo`, `--tempo-modifier N`, `--tempo-divider N` to
  judge the inference).  The divider never moves a note: durations are ticks on both sides.
- **Both sides as shipped** (data bugs kept).  A tie that changes nothing is one note on both sides.
- **Window:** up to the rip's last whole frame (its last is often half written); past it the log
  loops, not the game.
- **DAC:** a ROM song's drum starts are read by its samples' bytes; an asm song's by seeks and pauses.
- **Channels:** those both sides play; the others are named, not compared.  `--channels` / `--skip`
  take names or prefixes (`FM`, `PSG`).  `--diffs N` (default 12) differences listed per channel.
- A whole song off from one point on is a frame the rip lost or gained: log it in `rips.yaml` first
  ([below](#the-rips-own-faults-ripsyaml)), or the lift reads it as a tempo change.
- Frame logs are kept in `samples.render_cache`; a warm `--all` over Sonic 1's 19 rips takes about
  four seconds.

---

## Pitch, level, voice per frame: `vgm_frames`

What the lift cannot read — a detune, a voice, a slide that writes no key-on.  No lift: each note's
pitch (detune included), level, voice registers and FM pan against the rip's registers on the note's
frame, and on every later frame where the walk models them (a glide, a volume envelope).  Any driver.

```bash
python tools/vgm/vgm_frames.py configs/golden_axe/89_the_battle.yaml           # one song: its misses
python tools/vgm/vgm_frames.py --all --configs configs/streets_of_rage           # a line per pair
python tools/vgm/vgm_frames.py --all --configs configs/time_trax --aspects pitch pitch-path
```

| Flag | Default | Effect |
|------|---------|--------|
| `--aspects ...` | all | `pitch`, `level`, `voice`, `pan`, `pitch-path`, `level-path` |
| `--channels` / `--skip` |  | names or prefixes |
| `--misses N` | 8 | misses listed per channel and aspect |
| `--tied` |  | list tied notes' misses instead of attacking ones (vibrato runs on through a tie) |
| `--glitches` |  | scan for the rip's own glitches instead ([below](#the-rips-own-faults-ripsyaml)) |

Attacking notes are judged; tied ones are counted apart.  The rip's start offset is found from the FM
channels' first key-ons.  Two misses every rip shows alike are excused and counted apart: **log end**
(a note on the log's last frame, which ends mid-write) and **re-entry** (a channel's first note back
in its loop keyed a frame late).

---

## The rip's own faults: `rips.yaml`

A rip records its emulator's run, not only the song.  Faults that are the rip's go in its
`rips.yaml` entry; every rip tool then undoes or sets them aside.  Frames are the rip's frame
indices, as `vgm_analyze.py --frames` and `--glitches` print them.

```yaml
84_path_of_fiend:
  rip: "07 - Path of Fiend.vgz"
  glitches:          # a V-int lost (-1) or gained (+1): every channel off from `frame` on
    - {frame: 2304, frames: -1, why: "burst at 2303 (164 writes, every channel re-keyed) runs into 2304: its V-int lost"}
  foreign:           # another sound on these channels; from / to: frames (default: all)
    - {channels: [FM4], from: 120, to: 900, why: "..."}
  unlogged:          # what the ripper left out of a channel
    - {channels: [PSG3], aspects: [pitch], why: "..."}
```

- **Glitch:** the driver missed a V-int (vertical interrupt: the 60 Hz tick it runs on) or ran twice
  in one, so every channel keys a frame late (early) from there on, for good.  Seen in Space Harrier
  II's stage theme and staff roll and Golden Axe's Path of Fiend: a burst that re-keys every channel
  and loads their voices runs past its frame, and the next V-int is lost.  `vgm_frames` and `vgm_lift`
  read the log with each glitch undone, so the song after it is still checked.
- **Foreign:** another sound on a channel (a sound effect, a voice left keyed).  `vgm_frames` excuses
  its notes ("excused: another sound"); `vgm_lift` skips a channel held throughout and sets aside the
  differences on its frames ("known rip faults: FM4 3").
- **Unlogged:** writes the ripper dropped, so an aspect of a channel the rip cannot show (OutRun's rips
  lose PSG3's tone divider while that channel is muted).  Those aspects are set aside as known rip faults.
- `vgm_frames` alone cannot see a glitch: a frame late, the registers mostly still hold the note.
  Path of Fiend "matched" every note at offset −1 with its glitch in place; with it undone, at its
  true offset 0.

### Finding glitches

```bash
python tools/vgm/vgm_frames.py --all --glitches --configs configs/space_harrier_2
```

The scan follows each FM channel's key-ons and moves a channel when two attacks running land only on
another offset.  A candidate is a **glitch** when every channel playing across it moved (two at
least), else the song's own (one channel alone).  Each prints its frame, seconds and each moved
channel's attacks before / after (`[rips.yaml]` once logged), then the driver's updates around it
that follow a frame with none:

| Line | Meaning |
|------|---------|
| `after an overrun: a V-int lost` | the update before ran past the next V-int, and none came in that frame |
| `after a near overrun?` | it ended just short of the V-int, then an idle stretch: possible |
| `updates a frame apart: no V-int lost` | something else moved the channels |

The scan never writes.  Read the writes around a candidate (`vgm_analyze.py --frames` / `--updates`)
before logging it in `rips.yaml` by hand.

---

## The rendered audit: `vgm_compare`

Renders the rip one chip channel at a time (VGMPlay, mute masks, the Nuked OPN2 core) and the MOD one
column at a time (ffmpeg + libopenmpt), lines them up and reports per channel.  Renders go to
`output/compare/<config>/`.

```bash
python convert.py configs/sonic_1/01_title_screen.yaml
python tools/vgm/vgm_compare.py configs/sonic_1/01_title_screen.yaml "reference/vgz/sonic_1/01 - Title Theme.vgz"
```

Report sections: per-note pitch and level, per-channel summary, pitch verdict (as
`vgm_pitch_audit`), vibrato, channel balance, per-instrument levels, onset timing, noise (decay, band
profile), DAC rate.  Notes the rip plays after the MOD's single pass are left out.  Levels are L/R
power, never a mono mix (a hard-panned YM2612 channel reads ~5 dB low in mono).

| Flag | Default | Effect |
|------|---------|--------|
| `config vgz` |  | both required |
| `--mod FILE` | config `output_file` | the MOD |
| `--merged` |  | the merged build ([amiga_builds.md](amiga_builds.md#checking-a-merged-build)) |
| `--skip-render` |  | reuse the WAVs in the workdir |
| `--reuse-vgm` |  | re-render only the MOD |
| `--workdir DIR` | `output/compare/<config>/` | where renders go |
| `--ref CH` | FM2 | balance reference channel |
| `--core NAME` | NUKE | VGMPlay YM2612 core: NUKE, GPGX, GENS |
| `--offset S` | auto | MOD-minus-VGM offset |
| `--max-rows N` | 400 | per-note rows printed (flagged rows always) |
| `--write-volumes` |  | write the suggested volumes into the config |
| `--json FILE`, `--fail-*` |  | [CI use](#ci-use) |
| `--vgmplay DIR`, `--variant NAME`, `--settings PATH` |  | as elsewhere |

The per-note `vgm_c` / `mod_c` pitch columns are audio cross-checks on the strongest of a note's first
four partials; `<-- PITCH` flags the renders disagreeing by more than 25 c.  They are unreliable on
grace notes and legato runs: trust the pitch verdict.

### Per-instrument levels

The table `sample_list` volumes are set from.  Each note is matched to the instrument (and `Cxx`) that
plays it; the error MOD − VGM is reported per instrument, with a per-channel breakdown:

```
inst sample            vol notes    err spread suggest   per channel (Cxx: err xnotes)
  11 ghz_v05_lo.raw     16   116   +4.6    0.2       9   FM3 C1D: +4.8 x20  FM4: +4.6 x58  FM5: +4.6 x58
```

- **suggest** = volume × 10^(−err/20), from the notes without a `Cxx`.  With fewer than 4 of them the
  `Cxx` notes count too (a `Cxx` is the volume scaled by the note's level, so its error is the
  volume's; one at 64 may be clamped and does not count).
- **spread:** every channel sharing an instrument should show the same error.  Above 3 dB nothing is
  suggested — look at pan, fills, a wrong instrument, alignment.
- Errors are relative to the song's median note.  The DAC (fixed samples at 64) becomes the anchor only
  when it sits 2 dB or more below that median.
- A note's window ends 20 ms before the next key-on: the MOD's rows are even, the chip's ticks not,
  and a loud hit would read into a quiet note before it.
- MOD renders are moved onto the rows' clock: libopenmpt plays a tick as a whole number of samples
  (816 for 816.67 at 135 BPM), so a render runs up to 0.1 % fast — 81 ms by 99 s.

`--write-volumes` writes suggestions of 1 dB or more into the config, with `# VGZ: +4.6 dB at 16` on
the line.  Re-convert and re-run to verify.  A normalised MOD's volumes are the config's raised by one
gain; each suggestion is written back through it (config 16, MOD 41, suggested 12 → 5).

### Vibrato table

Every FM / PSG-tone note of 0.5 s or more is pitch-tracked in both renders.  A row prints when either
side modulates: rate and ± cents over the modulated stretch.

| Flag | Means |
|------|-------|
| `VIBRATO` | rate off by > 15 %, or depth by > 5 c and 30 % |
| `MISSING in MOD` / `not in VGM` | one side only |
| `b` row, `BEAT RATE` | **beating**: two detuned carriers swing the level at the rate; a wrong beat rate points at `synth_root` (a beat follows playback speed), never at `4xy` |

A PSG note starts where the channel becomes audible or its period moves more than 70 c; smaller
moves are modulation.

### Onset timing

Each FM / PSG / noise key-on in the log is matched one to one to a MOD note row within 40 ms (`EDx`
rows at their delayed start).  `unmatched` counts notes the MOD lacks or misplaces, `MOD-only`
re-triggers where the hardware ties.  A chip key-on counts as a note only after a key-off or a pitch
move of more than 70 c (under `smpsNoAttack` the driver still writes key-on).  A change of 20 ms or
more from first notes to last prints as `drift` (a MOD off the driver's tempo); a step of up to 120 ms
confirmed by the next two notes re-syncs (a tempo change).  The DAC uses audio onsets (the log holds
sample seeks, not hits), so its count is approximate.

### CI use

`--json FILE` writes the whole report plus a `checks` list and `passed`.  Thresholds are opt-in; any
failure exits 1:

| Flag | Fails when |
|------|-----------|
| `--fail-balance-db DB` | a channel's level relative to `--ref` differs from the rip's by more than DB |
| `--fail-pitch-cents C` | a MOD note is more than C cents off the chip, missing, or silent in the MOD render |
| `--fail-unmatched N` | a channel has more than N chip key-ons with no MOD note within 40 ms |

```bash
python tools/vgm/vgm_compare.py configs/sonic_1/01_title_screen.yaml "reference/vgz/sonic_1/01 - Title Theme.vgz" \
    --json output/compare/title.json --fail-balance-db 2 --fail-pitch-cents 25
```

---

## Every song's volumes: `measure_volumes`

Per song, in parallel (CPU cores − 1): convert, one `vgm_compare --write-volumes` pass, re-convert,
verify.  Then a line per song: volumes changed, what is still ≥ 1 dB off (at the 64 ceiling, channels
disagreeing, two-note instruments marked), and the pitch verdict.  Run it after any change to how
samples are rendered.

```bash
python tools/vgm/measure_volumes.py                                   # every Sonic 1 song
python tools/vgm/measure_volumes.py --only green_hill special_stage --no-write
python tools/vgm/measure_volumes.py --configs configs/moonwalker
```

| Flag | Default | Effect |
|------|---------|--------|
| `--only NAME ...` |  | configs whose stem holds a NAME |
| `--no-write` |  | measure and report only |
| `--jobs N` | cores − 1 | songs at once |
| `--min-db DB` | 1.0 | report instruments off by this much or more |
| `--configs`, `--vgz-dir`, `--rips`, `--vgmplay` |  | as elsewhere |

> **Pitfall:** exactly one write pass.  The errors are relative to the song's median note; once many
> instruments move, the median moves with them, and a second pass drifts the whole song.

---

## Reading a rip: `vgm_analyze`

The rip itself, decoded: per-channel pitches, levels, writes.  For checking a `synth_root`, a noise
mode, or what the driver really did around a suspect frame.

```bash
python tools/vgm/vgm_analyze.py "reference/vgz/sonic_1/01 - Title Theme.vgz" --chip fm --channel FM1 FM2
python tools/vgm/vgm_analyze.py "reference/vgz/sonic_1/01 - Title Theme.vgz" --chip all --max-rows 0
python tools/vgm/vgm_analyze.py "reference/vgz/sonic_1/02 - Green Hill Zone.vgz" --frames --chip all --channel FM1 PSG1
python tools/vgm/vgm_analyze.py "reference/vgz/streets_of_rage_2/15 - Expander.vgz" --writes --channel FM3 --from 0 --to 3
python tools/vgm/vgm_analyze.py "reference/vgz/time_trax/03 - Stages 1, 3, 8.vgz" --updates --from 1270 --to 1280
```

| Flag | Default | Shows |
|------|---------|-------|
| `--chip fm\|psg\|dac\|all` | fm | which chip (rate-3 noise rows show the tone-2 divider; DAC rows sample seeks) |
| `--channel CH ...` | all | `FM3 FM4`, `PSG1 NOISE DAC` ... |
| `--max-rows N` | 200 | rows printed; 0 unlimited |
| `--psg-mod-cents C` | 70 | PSG period moves within C cents are modulation, not a new note (0: a row per write) |
| `--volumes` |  | per-channel amplitude stats and suggested `sample_list` volumes |
| `--dac-stall` |  | the share of the DAC's playing time its driver holds the loop (a config's `dac_stall`) |
| `--frames` |  | the log frame by frame: keys, fnum, carrier TLs, PSG attenuations, DAC sample starts |
| `--writes` |  | every register write in order, decoded; a run of one field a row |
| `--updates` |  | each driver update (its burst of writes): start sample, lateness, reach past the next V-int, gap; a V-int lost flagged |
| `--from F` / `--to F` | whole log | frame range for `--frames`, `--writes`, `--updates` |
| `--clock HZ` / `--psg-clock HZ` | from the file | chip clocks |

On `--updates`: `V-int lost` marks the update after a frame with none, when the one before ran into
the V-int (`runs into N`), or ended under 1/6 frame short of it after filling 3/4 of a frame.  `V-int
lost?` (possible) marks such a near one followed by a longer idle stretch: where in it, or whether,
the writes cannot tell.  `Lost` lists the empty frame as `rips.yaml` takes it.  `--frames` and
`--writes` place each write in a frame, not on the sample it falls on.

---

## Checking a MOD alone

No rip needed.

| Tool | What it checks |
|------|----------------|
| `python tools/mod/mod_lint.py a.mod [b.mod ...]` | notes a ProTracker player cannot sound: a `3xx` with no sample playing or a played-out one, a `3xx` at volume 0, a note or instrument number on an empty slot, a note with `C00` in its own cell.  FT2 clone's / ProTracker's rules; libopenmpt (and so `vgm_compare`) plays these differently.  Exit 1 on any issue |
| `python tools/mod/mod_audit.py MOD [--slack S] [--banks]` | each sample against the notes that play it: unused, too short, oversize, low rate, duplicates; `--banks` each bank sound ([amiga_builds.md](amiga_builds.md#checking-a-merged-build)) |
| `python tools/mod/mod_render_diff.py A B [--floor DB]` | renders both per channel, reports the worst 20 ms difference — for a change meant to be inaudible |
| `tools/mod/mod_compare.py` | library only: `compare_mods(a, b, ignore_channels=[8])` diffs two MODs cell by cell (the regression suite uses it) |
