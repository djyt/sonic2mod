# VGM/VGZ input — lifting a register log back into an SMPS song

Planned 2026-10-02.  Goal: `convert.py` accepts a `.vgm` / `.vgz` rip of a Mega Drive game whose
music driver is a documented SMPS variant, and produces the same kind of MOD the asm path does —
with no disassembly in hand.

Status key: `[ ]` open, `[x]` done.  As in `user_improvements.md`, a done item keeps what was
measured and drops its old plan.

---

## Approach (Route A)

A VGM is a register log, not a score: YM2612 / SN76489 writes at 44.1 kHz, no notes, ticks, voice
numbers, coordination flags or loop structure — only a loop offset.  Rather than teach the converter
a second input model, a **lifter** rebuilds the `SmpsSong` IR the parser produces
(`core/smps/song.py`) from the log, and everything after `SmpsParser.parse_file()` — `DriverState`,
`walk_channel`, the planners, merging, synthesis, the MOD writer — runs unchanged.

What makes this tractable is that the driver is known.  The SMPS driver is deterministic and
documented: once the variant is known (Sonic 1's 68k driver first), each register write pattern
maps back to the flag that produced it — frequency table → note byte + `smpsAlterNote`, TempoWait
hold frames → ticks, `PSG_ENVELOPES` curves → `smpsPSGvoice`, the modulation triangle →
`smpsModSet`.  Inference is matching against a known machine, not open-ended transcription.

Out of scope (Route B, decided against for now): arbitrary drivers (GEMS, Konami, Krisalis,
Treasure …) — timer-driven tempo, per-frame arpeggios / slides / TL envelopes, LFO, SSG-EG, FM3
special mode, software-mixed PCM.  The lifter should fail loudly on these, not guess.

### The test bed we already have

All 19 Sonic 1 songs exist both as asm (`sonic_1/music/`) and as VGZ (`reference/vgz/`).  Every
phase below is accepted by lifting a VGZ and comparing against the asm parse of the same song —
IR to IR first, then MOD to MOD (`tools/mod_compare.py`, `tools/mod_render_diff.py`), then MOD to
VGZ (`tools/vgm_pitch_audit.py`, `tools/vgm_compare.py`).  No other format conversion in this
project has had a ground truth this complete; use it.

### What the VGZs look like (probed 2026-10-02)

- v1.50, 60 Hz rate field, FM / PSG writes in bursts at each V-int frame (735 samples NTSC),
  jittered up to ~36 samples by the interleaved DAC writes — round to frames.
- DAC: one type-0 data block (the decoded PCM bank: GHZ 16 936 bytes, Title 10 084), `0xE0` seeks
  to sample starts (GHZ 6 distinct offsets, Title 4), `0x8n` write-and-wait for every byte.
- GHZ has a loop offset (38.4 s body); Title Screen, Staff Roll have none.
- The tempo hold is visible: GHZ (`smpsHeaderTempo $01, $03`) never starts a note on frame residue
  2 mod 3.  Title Screen (`$01, $05`) is sparser — residues 1, 3, 4 mod 5 are all empty — so frame
  residues alone are ambiguous there and integer durations have to decide it.

---

## Phase 0 — groundwork

### [x] 0.1 The VGM reader in `core/vgm/`
- `core/vgm/reader.py`: `read_vgm` → `VgmLog` (header, `VgmWrite(sample, op, port, reg, value)`,
  PCM bank, loop sample, GD3).  A `0x8n` is logged as the YM2612 `0x2A` write it is; other chips'
  commands are skipped by their spec lengths, an unknown command raises `VgmError`.
- `core/vgm/chipstate.py`: `ChipState.replay` yields a `Change` per key / frequency / PSG / DAC
  event.  Chip semantics: `A4` latched until `A0`, one latch for the chip (Nuked-OPN2's `reg_a4`;
  the old tools kept one per channel); a PSG period latch plus its data byte at the same sample is
  one change.
- The two command parsers (`vgm_analyze.parse_vgm`, `vgm_pitch_audit.chip_timeline`) are gone;
  both, and `vgm_compare`, replay through `core.vgm`.  MOD row timing moved to `core/mod/timing.py`
  (`timed_pass`, `edx_delay`): three copies in the tools before.
- Measured: `vgm_analyze` (5 modes × 19 VGZs), `vgm_pitch_audit --list` and `vgm_compare` (20
  configs, plus `--merged` on the three merged configs), and the raw `parse_vgm` / `chip_timeline`
  data: byte-identical to before.  Regression PASS.

### [x] 0.2 Frame log
`core/vgm/frames.py`: `frame_log(log)` → `FrameLog`, a `Frame` per V-int: per FM channel fnum /
block, keyed slots, the key writes in order, frequency writes, operator registers `0x30–0x9F`,
`B0`, carrier TLs, `B4`; per PSG channel period, attenuation and the attenuations written, the
noise byte; the DAC's seeks, bytes, bytes since the seek and the gaps between bytes (reset at a
seek).  `vgm_analyze.py --frames` prints it.
- The burst phase differs per rip (+1 … +733 of 735 samples), jitter ±12 samples, bursts up to 325
  samples long.  A frame's window opens a quarter frame before the phase (the commonest burst start).
- Measured on all 19: every burst in one frame, no frame with two.  Key-on frames of GHZ fill 2 of
  3 residues mod 3, Title 2 of 5 mod 5: the TempoWait hold is visible as expected.

### [x] 0.3 Input dispatch
`core/source/read_song(path, LiftOptions)`: `.vgm` / `.vgz` → `lift_song`, else `SmpsParser`;
`ConversionConfig.read_song()` passes the config's options.  `convert.py`, `analyze.py`,
`merge_survey.py`, `config_to_chip_space.py` and `vgm_pitch_audit.py` read through it.  Config keys
`driver:` (`SmpsDriver`, `sonic1` only), `tempo_modifier:` / `tempo_divider:` (VGM input only,
>= 1); documented in `docs/yaml_config.md`.  `lift_song` raises `VgmLiftError` until Phase 1.

---

## Phase 1 — Sonic 1 driver lift (`driver: sonic1`)

Each item produces part of the `SmpsSong`.  Accept each by IR comparison against the asm parse
(after `extend_looping_channels`, since the VGZ is unrolled).

**The yardstick (done 2026-10-03):** `core.smps.played_song` gives each note as the driver plays
it - ticks, attack, the frequency word, the voice's registers, the carriers' TL, pan, modulation,
fill, noise byte, DAC sample - with the asm's spelling gone (calls, flag order, transposition vs
note byte, voice TL vs track volume), and `compare_songs` matches two songs by start tick per
`Aspect`.  `python tools/vgm_lift.py --all [--aspects timing]` lifts every rip and compares it
with its config's asm in about a second (frame logs cached by `core.vgm.load_frames`); one rip
prints its differences, repeated ones grouped.  "Accept" below means its aspects come out same.

### [ ] 1.1 Time grid: frames → driver ticks
- Find the tempo modifier m: the residue mod m on which no note starts (TempoWait holds every m-th
  frame), confirmed by every note duration coming out a whole number of ticks.  Tick k lands on
  frame `k + k // (m−1)` (`docs/pipeline.md` gotcha 7a) — invert it.
- Tempo divider: GCD of note durations in ticks, cross-checked with `ticks_per_row`.
- Mid-song tempo changes (Drowning's `smpsSetTempoMod`, Credits' `smpsSetTempoDiv`): segment where
  the hold pattern changes; emit the flag at the segment start.
- `tempo_modifier:` in the config overrides the inference.
**Accept:** inferred `smpsHeaderTempo` equals the asm's for all 19; every note's tick equals the asm's.

### [ ] 1.2 Song loop
VGM loop offset → loop sample → loop tick; `has_jump`, `loop_tick` and `loop_event_index` (the first
event at or after that tick) on every channel - no labels needed.  No loop offset → no jump (Title).
Data after one loop pass (some rippers log a fade or second pass) is cut at loop start + body.
**Accept:** `loop_target_tick()` equals the asm's; the MOD's `Bxx` lands on the same row.
Measured 2026-10-03 (the label audit): Marble Zone, Spring Yard and Robotnik loop exactly their tick
spans' frames (1920 ticks at modifier 9 = 2160 frames = the VGZ's 1587600 samples); Labyrinth's VGZ
loop is one frame longer than 1728 ticks' 2073 - at modifier 6 a span of ticks holds 345 or 346
TempoWait frames depending on where it starts, so a loop's length in frames is not a function of its
ticks alone.  Every channel's loop now starts where it reached its own target
(`tests/test_song_units.py` checks all songs' loops agree with the song's period).

### [ ] 1.3 FM notes
- Key-on → note; key-off with no key-on → rest; a key-on while keyed at the same frequency → tie
  (`smpsNoAttack` + duration); a frequency change while keyed and no key-off → legato
  (`smpsNoAttack` + note); a key-on after key-off at the same frequency → retrigger.
- A key-off before the next key-on: note + rest.  The asm may have written `smpsNoteFill`; for the
  MOD both cut at the same frame, so lift the simpler form, and lift a fill only where the same
  frame count recurs on every note of a passage (smaller IR, and the fill's frame semantics stay
  intact).  Record which choice was made per channel in the report.
- Pitch: FNUM/block → nearest entry of the driver's FM frequency table (`core/smps/driver_tables`)
  → note byte at `pitch_offset 0`, the remainder → `smpsAlterNote`.  FM labels are real pitches
  already, so lifted configs are `range_space: chip` by construction.  `smpsChangeTransposition`
  is not recovered — it is invisible and unnecessary in chip space.
**Accept:** per channel, same notes (chip pitch), ticks, durations, ties and legatos as the asm;
`smpsAlterNote` values equal where the asm sets one (GHZ FM5 `$03`, Scrap Brain FM4 scoops).

### [ ] 1.4 FM voices and levels
- Snapshot operator registers (`0x30–0x9F`) and `B0` at each key-on after a voice write
  (`SetVoice` writes them all).  Modulators + algorithm/feedback identify the voice; carrier TLs
  are voice TL + track volume — take each voice's lowest observed carrier TL as the voice's own and
  the rest as `smpsAlterVol` offsets (plus `SmpsChannelHeader.volume` for the channel's first).
- Deduplicate into `SmpsVoice`, indices in order of first appearance.  `SmpsVoice.params` holds
  `smpsVc*` strings today; give it an int constructor (or store ints) rather than formatting hex
  text to be parsed back.
- Optional `voice_bank: <asm file>`: match lifted voices to a known voice table and take its
  indices, so an asm-made config's `voice_map` applies to the VGZ unchanged — the key to comparing
  MODs in the test bed, and useful for hacks that reuse Sonic 1's bank.
- Pan from `B4` → `smpsPan`.  `B4` AMS/PMS ≠ 0, `0x22` LFO on, SSG-EG (`0x90–0x9F`) ≠ 0 or FM3
  special mode (`0x27` bit 6) → error: not a Sonic 1 song, and `ym2612/voice.py` renders none of it.
**Accept:** every lifted voice equals an asm voice (param for param); `LevelPlanner.levels` gives the
same baked levels.

### [ ] 1.5 Vibrato → `smpsModSet`
Per-frame FNUM movement inside a note.  Fit `wait, speed, delta, steps` against the driver's
modulation (first half-swing at half the steps, then `2·speed·(steps+1)`-frame cycles,
`docs/smps_driver.md`).  The fit is exact-or-nothing: simulate the candidate and require every
frame to match.  `smpsModOn` / `smpsModOff` where it starts and stops between notes.
**Accept:** the `4xy` rows of the lifted MOD equal the asm MOD's on all six songs `vgm_compare`
measures vibrato on (Title, GHZ, Spring Yard, Scrap Brain, Stage Clear, Special Stage).

### [ ] 1.6 PSG tone
- Notes: audible + divider → note via `PSGFrequencies` (including the measured out-of-table indices
  125–127 in `PSG_FREQUENCIES_EXTENDED`); a same-pitch retrigger shows only as the envelope
  restarting (attenuation stepping back up to the curve's start).
- Envelope: per-frame attenuation from key-on, minus the note's base → match against
  `PSG_ENVELOPES_BY_NAME` → `smpsPSGvoice fTone_NN`; the base → `smpsPSGAlterVol` / header volume.
- Vibrato as 1.5, on the divider.
**Accept:** same notes, envelope labels and attenuations as the asm on PSG1/PSG2 of all 19 (Spring
Yard's below-table notes, Labyrinth's `smpsAlterPitch` loops, Ending's PSG2 are the hard ones).

### [ ] 1.7 PSG noise
Noise register → form byte `$E0 | white << 2 | rate` → `smpsPSGform`; key-ons as 1.6 on channel 3's
attenuation; envelope label matched as 1.6 (Scrap Brain's `fTone_08` hi-hat variant); rate 3: the
tone-2 divider lifted as PSG3's note so `derive_rate3_dividers` finds it.
**Accept:** `psg_map` keys and noise envelopes derived from the lifted song equal the asm's.

### [ ] 1.8 DAC
- Notes from `0xE0` seeks; the sample is the data-block bytes from the seek to the last `0x8n`
  before the next seek or silence; the rate from the `0x8n` wait nibbles (the Z80 loop's period).
  Same offset at another rate = another pitch of the same sample (timpani).
- Name each distinct (offset, length, rate) and lift them as `dac_name` notes; write the PCM as
  `.raw` into the config's `samples_dir` with a manifest, so `dac_samples:` can point at them.
  The data block holds the *decoded* bytes, so this may beat the current hand-extracted `.raw`s.
- Watch for the logger merging back-to-back restarts of the same sample (`vgm_analyze` docstring):
  a restart with no seek shows as the `0x8n` stream running past the sample's known length.
**Accept:** DAC note count and ticks equal the asm's; extracted kick/snare/timpani byte-equal (or
explained) against `samples/`.

### [ ] 1.9 Recording artefacts
- Song-start key-on artefacts (Spring Yard audit) and the sound-test state before the first write:
  drop writes before the first real key-on, warn.
- A VGM logged in-game may carry SFX on FM/PSG channels: detect a voice/state change with no
  matching music pattern and warn; not handled.

### [ ] 1.10 Tooling
- `tools/vgm_lift.py <file.vgz> [--asm OUT] [--compare song.asm]`: print the lifted song, write it
  as SMPS asm (readable, editable, and round-trippable through `SmpsParser` — a check on the lift in
  itself), or diff it against an asm parse event by event.  The comparison and `--all` are done
  (the yardstick above); printing and `--asm OUT` are left.
- `analyze.py file.vgz` works (dispatch, 0.3), including its YAML skeleton generator, so a new rip
  gets a starter config.

### [ ] 1.11 Regression
Add a VGZ case per song that has a lifted config (start with Title Screen and GHZ) to
`tests/regression.py`; `tests/test_vgm_lift_units.py` for the inference primitives (tempo hold,
tie/legato/retrigger, envelope match, modulation fit) with hand-built frame logs.

**Phase 1 done when:** all 19 VGZs lift to an IR that matches the asm parse (differences listed and
explained), and the MODs from VGZ and asm agree in `mod_render_diff.py`.

---

## Phase 2 — other SMPS variants

### [ ] 2.1 Driver variant table
Today `core/smps/driver_tables.py` *is* Sonic 1: the FM frequency table, `PSGFrequencies`,
`PSG_ENVELOPES`, the TempoWait timing in `core/plan/timeline.py`, the modulation shape in
`core/convert/vibrato.py`, the FNUM range (644 C … 1216 B) in the vibrato depth.  Make a
`DriverVariant` (frequency tables, PSG envelope set, tempo algorithm, modulation algorithm, note
range, DAC scheme) chosen by the config's `driver:`, Sonic 1 the default, and thread it through
the lifter **and** the converter (both read the same tables — the converter must render and place
a Sonic 2 note with Sonic 2's tables).  Baselines must stay byte-identical.

### [ ] 2.2 Sources for the variants
- ValleyBell's **SMPSPlay** driver definitions (`DefDrv` / per-game INI): frequency tables, tempo
  modes, PSG and modulation envelopes, per-game differences — the most complete written record.
- Sonic Retro's SMPS documentation and the `SMPS2ASM` macro sets: coordination flag tables per
  variant.
- The Sonic 2 / Sonic 3 & Knuckles disassemblies (s2disasm, skdisasm) for the Z80 drivers.
Each variant added gets a section in `docs/smps_driver.md` with what differs from Sonic 1, cited.

### [ ] 2.3 Variants, in order
1. **Other SMPS 68k games** (same family as Sonic 1; many Sega titles and Sonic 1 hacks): mostly
   tables.  Test with a game's VGZ + its SMPSPlay definition.
2. **Sonic 2 (SMPS Z80)**: different tempo algorithm (accumulator overflow rather than TempoWait —
   1.1's inference becomes per-variant), Z80 DAC.
3. **Sonic 3 & Knuckles (SMPS Z80, later)**: modulation envelopes and FM volume envelopes beyond
   Sonic 1's flags — check against SMPSPlay which ones the converter can express (`4xy`, `Axy`)
   before lifting them; may need IR effects the asm path never produced.
Each needs a ground-truth pair (disassembled song + VGZ) to accept it, as Phase 1 had.

### [ ] 2.4 Variant detection
From the log alone: which frequency table the FNUMs come from, the tempo hold pattern, PSG
envelope matches.  Suggest a `driver:` when none is set; never pick silently.

---

## Open questions

- **Fill vs rest (1.3):** if the MOD never differs, is lifting fills worth it at all?  Only for a
  readable `vgm_lift --asm`; decide after 1.3 has numbers.
- **Measured effects for the non-matching case:** a vibrato or PSG envelope that matches no table
  entry — fail, or lift a "measured" effect (`render_psg_tone` already takes an `envelope=` list)?
  Phase 1 fails; revisit in Phase 2 when a variant's tables are incomplete.
- **Voice identity across channels (1.4):** two voices with the same modulators but carrier TLs
  that differ by other than a constant are different voices; check no Sonic 1 voice pair trips the
  lowest-carrier-TL rule.
- **PAL rips:** 882-sample frames, 50 Hz; the hold-frame inference is the same, the tick rate is not.
  Read the header rate field.
