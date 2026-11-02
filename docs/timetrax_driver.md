# Time Trax: Tim Follin's Z80 driver

`Time Trax (USA) (Proto).md` (SHA-1 `d7eb65f6…`; Malibu Interactive, music Tim Follin, unreleased).
Not SMPS: a family of its own (`core/drivers/timetrax/`), read into the SMPS IR.  Six songs, all
FM; the driver's PSG, DAC, LFO and random jumps are left to its SFX.  How it was found and built:
`docs/todo/done/time_trax.md` (its tables, its command bytes, the rips' faults).

```
python convert.py configs/time_trax                                    # the six songs, minimal configs
python tools/vgm/vgm_frames.py --all --configs configs/time_trax       # every read against its rip's frame
python tools/vgm/vgm_pitch_audit.py --all --configs configs/time_trax --slides
python tools/rom/disasm.py "input/roms/Time Trax (USA) (Proto).md" --z80 --table 0DC8 --entries 44 --first 80
```

---

## Where things are

Found by the code that reads each (`locate.py`): the Z80 program at ROM `$C618E` (`$1A8E` bytes,
copied raw), the music bank at ROM `$F8000` (Z80 window `$8000`): a header of big-endian pointers to
the song table (39 entries: 0-5 music), 97 voices (26 bytes, register order) and 28 volume envelopes.
The FM table (Z80 `$1804`): 96 words, 8 octaves from B, A = fnum `$44B` (+10 cents on PAL, played
as heard).

**Update:** one a V-int, no tempo: a duration counts frames.  The rips run at 50 Hz (configs
`region: pal`); MOD speed and BPM give one tick a frame.

## The track code (`grammar.py`, `commands.py`)

`$01`-`$7F` a note (table entry n + transposition), `$00` a rest, each with a duration byte unless a
fixed duration (`$86 n`) is on; `$80`-`$AB` commands.  What a byte means can depend on the path to
it (the fixed duration, `$83`'s loop, `$8E`'s wait), so each track is first walked frame by frame
(`walk.py`) and read through what that walk found.

| Bytes | IR |
|---|---|
| `$89 n` | `SetTransposition` (a set, not a step) |
| `$8A v`, `$AA m` / `$AB` | `ScaledVolume`, `VolumeMask`: TL' = 127 − (127 − TL) × master × v / 256² per masked operator (`volume.py`) |
| `$8B` / `$8C` | legato off / on: a note neither keys off nor on |
| `$8D n` / `$8E` | a signal / a wait: song 0's FM5 waits for FM3, read as a rest of the frames it waits |
| `$92 s f` / `$93` | `Glide`: the word s fnum units every f frames toward the note's (`glide.py`: blocks crossed at `$4D2` / `$245`) |
| `$94 s f` / `$95` | `Portamento`: the table index s semitones every f frames; a glide runs on top |
| `$96 n`, `$98 n` | `Gate`, `KeyOffAfter`: a read keyed off n frames before its end / after its start |
| `$9F a r`, `$A0` / `$A1`, `$8A` | `VolumeEnvelope`: envelope a (`envelope.py`) from each note's read; restart on / off; a volume stops it |
| `$A8 d` / `$A9` | `Detune`: d added to the word |
| `$9A`-`$9C` | vibrato: read, dropped (two uses: songs 2 and 3's FM3) |

**A rest writes nothing** (`TrackRules.key_off.rest_holds`): the note sounds on through it, and a
legato read after it re-pitches the note unkeyed (song 0's FM4 bass under its credits).

## How it converts

- **Glides and portamento** (`core/smps/slide.py`, `core/plan/glides.py`): every read's word frame by
  frame.  Where the sounding sample can follow it (B-3 .. C-1), each row slides (`1xx` / `2xx`, fine
  `E1x` / `E2x`) to the word on its last frame (`PitchCurve`); a legato read glides on, no key-on,
  whatever `legato` says.  `1xx` moves from a row's second tick: a semitone-a-frame portamento sits a
  frame behind (song 4's FM5).
- **Sweeps** a slide cannot follow (the drums: four octaves in five frames) are **baked**
  (`core/plan/baked.py`): the keyed read, the legato reads it cannot slide to and the held rests after
  them are one shape, word offsets from the keyed note's, rendered into a sample of its own; those
  legato reads key nothing (`BakedPlan.carried`).
- **Volume envelopes** are baked the same way (each frame's TLs); with no slot the note plays its
  instrument's sample, the level by `Cxx` row by row (`LevelCurve`), before a slide on the row.
- **Slots** (song keys, `docs/yaml_config.md` § 2): a derived config fills all 31.  `level_patch_min:
  3` plays a volume law's voice copy within 3 TL steps of its voice as that voice; `baked_slots` (4;
  songs 3 and 4: 6) keeps slots for shapes, as far as folding and merging frees them, sweeps first
  (an unbaked sweep is unheard; a level falls back to `Cxx`).  Banks (`9xx`) were weighed and left:
  only the sweeps would gain, and they fit.
- **Quality:** `window_root: highest` in every config: each window rooted as high as its range and the
  64 K limit allow (notes' mean rate ~8 kHz -> ~20 kHz; the samples about twice the bytes).
- **Speed:** songs 1, 2, 3 and 5 derive 1 frame a row (250 BPM, speed 2): legato reads (glide,
  portamento and sweep steps) land on odd frames.  `--variant slow` rows the keyed notes by the bar:
  songs 2 and 5 a 16th (2.5 frames, speed 5: 4 bars a pattern), song 3 a 32nd (3 frames: 2 bars),
  song 1 3 frames (its 120-frame bar, 20 16ths, is 40 rows).  The same notes (song 3: one 1-frame
  stagger off); glides step a row at a time.

## Against the rips

Rips: VGMRips 1.02, `reference/vgz/time_trax/`, `configs/time_trax/rips.yaml`.  Each loses a V-int
now and then (an update running into the next frame: 2-25 a rip, every channel a frame late from
there); `rips.yaml` lists them and every rip tool undoes them (`RipFaults`), the pitch audit and
`vgm_compare` in the VGM's own waits (`core.vgm.unshifted`).  The rips start up to 0.8 s before
their song: the pitch audit's lag search runs both ways.

| Check | Result (2026-11-02) |
|---|---|
| `vgm_frames` | every read's pitch, level, voice, pan, pitch and level path on its frame, but 1-4 reads a song (songs 0, 2, 3) |
| `vgm_pitch_audit` | 11 of 12,500 notes off: grace notes (song 4), a sweep's last cents (song 5), one in song 2 |
| `--slides` | 85-98 % of slide steps within 50 cents (song 4: 78 %, its fast portamento) |
| `measure_volumes` | one pass, 79 volumes; residuals ≥ 1 dB on 1- and 2-note instruments or at the 64 ceiling |

Open: the vibrato (`$9A`); listening (the user, in the FT2 clone).
