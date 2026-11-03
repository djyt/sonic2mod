# Time Trax: Tim Follin's Z80 driver

Time Trax (an unreleased prototype; Malibu Interactive, music by Tim Follin) uses its own Z80 driver,
not SMPS: frame-counted durations, glides and portamento, volume envelopes, a volume law that scales
operators, and rests that let the note ring.  sonic2mod reads it into the SMPS song IR.  For anyone
converting its songs or a driver whose pitch and level move every frame.

On this page:
- [Summary](#summary)
- [Where things are](#where-things-are)
- [How it differs from Sonic 1](#how-it-differs-from-sonic-1)
- [Track code](#track-code)
- [How it converts](#how-it-converts)
- [Known limits](#known-limits)

## Summary

| | |
|---|---|
| ROM | `Time Trax (USA) (Proto).md`, SHA-1 `d7eb65f6…` |
| Driver | `timetrax`, `core/drivers/timetrax/` (a family of one) |
| Songs | 6 (`$00`-`$05`), all FM: FM1-FM5, two songs FM6 too |
| Configs | `configs/time_trax/`: 6 minimal configs, `rips.yaml` (VGMRips 1.02, `reference/vgz/time_trax/`) |
| Source | no disassembly: read with `tools/rom/disasm.py --z80` |

The driver's PSG, DAC, LFO and random jumps are used only by its SFX, which are not read.

```bash
python convert.py configs/time_trax                                    # the six songs
python tools/vgm/vgm_frames.py --all --configs configs/time_trax       # every read against its rip's frame
python tools/vgm/vgm_pitch_audit.py --all --configs configs/time_trax --slides
python tools/rom/disasm.py "input/roms/Time Trax (USA) (Proto).md" --z80 --table 0DC8 --entries 44 --first 80
```

## Where things are

Each found by the Z80 code that reads it (`timetrax/locate.py`).

| What | Where | Note |
|------|-------|------|
| Z80 program | ROM `$C618E`, `$1A8E` bytes | copied raw to Z80 RAM |
| Music bank | ROM `$F8000` at Z80 `$8000` | mapped by the driver's start |
| Bank header | Z80 `$8000` | big-endian pointers to the three tables below |
| Song table | Z80 `$8007` | 39 entries: 0-5 music, the rest SFX |
| Voices | Z80 `$8205` | 97, 26 bytes each, register order |
| Volume envelopes | Z80 `$82C7` | 28 |
| FM table | Z80 `$1804` | 96 words, 8 octaves from B (index 1 = C0) |
| Command table | Z80 `$0DC8` | `$80`-`$AB` |
| Pan table | Z80 `$1340` | B4's speaker bits by `$87`'s operand |
| Volume law | Z80 `$0100` (256 squares), master volume `$000A` (`$FF`) | |

The FM table's A is fnum `$44B`: +25 cents on the NTSC clock, +10 on PAL's.  Notes play as written.

## How it differs from Sonic 1

| | Time Trax | Sonic 1 |
|---|---|---|
| Update | one a V-int, no tempo: a duration counts frames | TempoWait ticks |
| Clock on the rips | 50 Hz (configs: `region: pal`) | 60 Hz |
| Note byte | `$01`-`$7F` (table entry + transposition), duration after | `$81`-`$DF` |
| Rest | `$00`: writes nothing, the note sounds on (`key_off.rest_holds`) | keys off |
| Transposition | `$89 n` sets it | `$E9` adds |
| Volume | scales each masked operator's output (`VolumeLaw`) | adds to the carriers' TL |
| Pitch movement | glides and portamento, frame by frame (`SlideLaw`) | modulation only |
| Envelopes | volume envelopes on FM, frame by frame | PSG only |
| Fixed duration | `$86 n`: every note n frames, no duration byte | — |
| Track sync | `$8D` signal / `$8E` wait | — |

## Track code

`timetrax/commands.py` (what each byte is), `timetrax/grammar.py` (what it plays as).  `$01`-`$7F` is a
note (table entry n + transposition), `$00` a rest, each with a duration byte unless a fixed duration
(`$86 n`) is on; `$80`-`$AB` are commands.  A duration of 0 is 256 frames.

What a byte means can depend on the path to it (the fixed duration, `$83`'s loop end, `$8E`'s wait), so
each track is first walked frame by frame (`timetrax/walk.py`) and read through what that walk found.

| Bytes | IR |
|---|---|
| `$80` / `$81 w` / `$82 n` / `$83` / `$84 w` / `$85` | stop, jump, loop n passes, loop end, call, return |
| `$86 n` | fixed duration n (0: off) |
| `$87 n`, `$88 n` | pan, voice |
| `$89 n` | `SetTransposition` (a set, not a step) |
| `$8A v`, `$AA m` / `$AB` | `ScaledVolume`, `VolumeMask`: TL' = 127 − (127 − TL) × master × v / 256² per masked operator (`volume.py`) |
| `$8B` / `$8C` | legato off / on: a note neither keys off nor on |
| `$8D n` / `$8E` | a signal / a wait: song 0's FM5 waits for FM3, read as a rest of the frames it waits |
| `$92 s f` / `$93` | `Glide`: the word s fnum units every f frames toward the note's (`glide.py`: blocks crossed at `$4D2` / `$245`) |
| `$94 s f` / `$95` | `Portamento`: the table index s semitones every f frames; a glide runs on top |
| `$96 n`, `$98 n` | `Gate`, `KeyOffAfter`: keyed off n frames before its end / after its start |
| `$9F a r`, `$A0` / `$A1`, `$8A` | `VolumeEnvelope`: envelope a (`envelope.py`) from each note's read; restart on / off; a volume stops it |
| `$A8 d` / `$A9` | `Detune`: d added to the word |
| `$9A`-`$9C` | vibrato: read, dropped (two uses: songs 2 and 3's FM3) |
| `$8F`-`$91`, `$9D` `$9E`, `$A2`-`$A7` | refused: no song plays them (signal jumps, random jump, release envelopes, LFO, DAC) |

**A rest writes nothing** (`TrackRules.key_off.rest_holds`): the note sounds on through it, and a
legato read after it re-pitches the note unkeyed (song 0's FM4 bass under its credits).

## How it converts

**Glides and portamento** (`core/smps/slide.py`, `core/plan/glides.py`).  Every read's word is followed
frame by frame.  Where the sounding sample can follow it (B-3 … C-1), each row slides (`1xx` / `2xx`,
fine `E1x` / `E2x`) to the word on its last frame (`PitchCurve`).  A legato read glides on with no
key-on, whatever `legato` says.  `1xx` moves from a row's second tick, so a semitone-a-frame portamento
sits a frame behind (song 4's FM5).

**Sweeps are baked.**  A sweep a slide cannot follow (the drums: four octaves in five frames) is
[baked](../glossary.md#baked-sample) (`core/plan/baked.py`): the keyed read, the legato reads it cannot slide
to and the held rests after them become one shape — word offsets from the keyed note's — rendered into
a sample of its own.  Those legato reads key nothing (`BakedPlan.carried`).

**Volume envelopes** are baked the same way (each frame's TLs).  With no slot free the note plays its
instrument's sample, its level set by `Cxx` row by row (`LevelCurve`), before a slide on the row.

**Slots.**  A derived config fills all 31 ([../guide/song_configs.md](../guide/song_configs.md)):

```yaml
# configs/time_trax/00_title_screen.yaml
region: pal               # the rips run at 50 Hz
level_patch_min: 3        # a voice copy within 3 TL steps of its voice plays as that voice
baked_slots: 4            # slots kept for baked sweeps and envelopes (songs 3 and 4: 6)
window_root: highest      # each window's sample rooted as high as the 64 KB limit allows
```

`baked_slots` keeps slots for shapes, as far as folding and merging free them, sweeps first: an
unbaked sweep is unheard, while a level falls back to `Cxx`.  Banks (`9xx`) gain only the sweeps, which
fit without them, so none are used.  `window_root: highest` raises the notes' mean sample rate from
~8 kHz to ~20 kHz (the samples about twice the bytes).

**Speed.**  Songs 1, 2, 3 and 5 derive 1 frame a row (250 BPM, speed 2): legato reads (glide,
portamento and sweep steps) land on odd frames.  Variants re-grid them:

| Variant | Songs | Rows |
|---------|-------|------|
| `bar` (`row_grid: bar`) | 1, 2, 3 | by the meter: song 1 6 frames a row (5/4: 20-row bars, 60-row patterns), song 2 5 (32-row phrases), song 3 3 (an intro bar, then 64-row phrases); the same notes, 55-144 a frame off; glides step a row at a time |
| `slow` | 5 (Game Over) | 2.5 frames a row (speed 5): a 16th a row, 4 bars a pattern |

```bash
python convert.py configs/time_trax/01_stages_2_5_7.yaml --variant bar
```

**Against the rips.**  Each rip loses a V-int now and then (a [glitch](../glossary.md#glitch)) (an update running into the next frame: 2-25
a rip, every channel a frame late from there).  `rips.yaml` lists them and every rip tool undoes them
(`RipFaults`), the pitch audit and `vgm_compare` in the VGM's own waits (`core.vgm.unshifted`).  The
rips start up to 0.8 s before their song: the pitch audit's lag search runs both ways.

| Check | Result |
|---|---|
| `vgm_frames` | every read's pitch, level, voice, pan, pitch path and level path on its frame, but 1-4 reads a song (songs 0, 2, 3) |
| `vgm_pitch_audit` | 11 of 12,500 notes off: grace notes (song 4), a sweep's last cents (song 5), one in song 2 |
| `--slides` | 85-98 % of slide steps within 50 cents (song 4: 78 %, its fast portamento) |
| `measure_volumes` | one pass, 79 volumes; residuals ≥ 1 dB on 1- and 2-note instruments or at the 64 ceiling |

## Known limits

- **Vibrato** (`$9A`-`$9C`) is read and dropped.
- **`bar` on song 2:** the pitch audit misjudges it — FM5's intro has 4-frame notes that 5-frame rows
  cannot all hold, and its running alignment settles a note off over the F#5s after (the MOD's note-ons
  match).
- The SFX (PSG, DAC, LFO, random jumps) are not converted.
