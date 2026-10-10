# Streets of Rage: SMPS 68k with a MUCOM track format

Planned 2026-10-08, closed 2026-10-10.  Goal: `convert.py` reads Streets of Rage's songs from the
ROM, as it reads Sonic 1, Moonwalker and Golden Axe (`binary_import.md`, `docs/smps_variants.md`).
Done: every song converts from the ROM; left open by the user's close: 4.4.

Status key: `[ ]` open, `[x]` done.  Three parts: what the driver is (1), what the code must change
first (2), the plan (3).

---

## 1. Analysis (probe 2026-10-08)

Moved to `docs/smps_variants.md` § Streets of Rage, with what Phases 2-5 found.

---

## 2. What the code must change

Below the walk, SoR shares nothing with the SMPS grammar: track bytes, headers, voices, per-kind
flags, operand pointers.  Above it, it needs driver facts the IR does not hold.  Rules applied
(`agents.md`): levels of abstraction, DRY, layering, settings first.

### 2.1 Refactors (behaviour unchanged, every baseline byte-identical)

1. **Track grammar per variant** (`core/rom/tracks.py`).  `_decode` is SMPS's grammar.
   `decode_tracks` keeps the following, labels, layout and overlap check.  The variant supplies
   `decode(memory, address, kind)`: today's `_decode` becomes the SMPS grammar module, SoR gets
   its own.
2. **Flags per channel kind** (`SmpsVariant.flags`).  SoR's FM and PSG tables differ, and the DAC
   reads FM's with other meanings.  Make it `Mapping[ChannelType, Mapping[int, FlagSpec]]`; the
   existing variants use one table for all kinds.  Code reached from two kinds is decoded per kind.
3. **Header layout as data** (`core/rom/header.py`).  Move `_MUSIC_FIXED`, `_FM_TRACK`, `_PSG_TRACK`
   and the field offsets into `HeaderLayout`: tempo bytes or none, each entry's pointer / pitch /
   volume / mod / envelope offset, the PSG slots' chip channels.  `is_music_header` reads the same
   layout.
4. **Voice layout as data** (`core/rom/voices.py`).  Move FB/ALG first-or-last and the operator
   storage order (SMPS: `reversed`) into `VoiceLayout`.
5. **Op kinds by meaning** (`core/smps/code.py`).  The walk tells notes from durations by byte
   range, so B7 (`$E0`) cannot be a note.
   - Add `OpKind.NOTE / REST / DURATION / NO_ATTACK`; each front end (asm parser, SMPS grammar)
     decodes them.
   - Note values stay SMPS-numbered (`$81` = C0); `$E0` = B7 becomes legal.
   - Rejected: a per-channel `pitch_offset` invented to fit `$81`-`$DF`.  It is a header field
     the song does not have.
6. **The FM table check per variant** (`smps68k/locate.py`).  `locate_68k` demands Sonic's
   octave; SoR's starts at C.  Each variant states its table.  SoR's locate reuses `read_index`.
7. **One Z80 program reader** (`core/rom/z80.py`, `smps68k/dac.py`).  Today two shapes: copy
   loops, and Sonic's Kosinski `lea DACDriver`.  SoR adds a third: unpack to RAM, then copy.
8. **Row grid** (`core/plan/derive.py` `_timing`).  gcd-then-double gives SoR 1 frame (from the
   1-frame staggers), then 2, 4, 8.  Instead, choose the grid with the most onsets on rows: 7, 6,
   8, 5, 3, 13 frames here, the rest by EDx.  At BPM 150 a MOD tick is one NTSC frame, so EDx
   places the staggers exactly.  Recheck the Moonwalker and Golden Axe minimal configs.

### 2.2 New concepts

What the song must say goes in the IR; no variant check downstream (`docs/smps_variants.md`).
State is resolved in the walk where it already keeps state (the tempo divider precedent), or in
a song pass (the `run_out` precedent).

| SoR fact | IR | Resolved in | Downstream |
|---|---|---|---|
| tick = frame | divider 1, `NO_TEMPO_HOLDS` | header | none |
| FM / PSG volume steps | `TrackRules.volume_steps`; `VOLUME_STEP`, `ALTER_VOLUME_STEP` -> `SET_VOL`; carrier TLs read as 0 | walk | none |
| detune add | `DETUNE_ADD` -> `DETUNE` | walk | none |
| gate | `GATE` -> note + rest, marked off-grid like `run_out` (`SmpsNote.cut`) | walk (it looks at the next byte) | none |
| loop break, tie cleared on exit | `OpKind.LOOP_EXIT` | walk | none |
| DAC sample by flag | `DAC_SAMPLE` -> DAC notes get the selected sample | walk | none |
| DAC rest / gate cut | the drum track's `TrackRules` (a rest cuts the sample) | rules | `channel_writer._on_rest` |
| noise leaves tone3 alone | noise notes keep the last tone note's pitch, else `nMaxPSG` (divider 0) | walk | none |
| PSG row clamp | the decoder maps to Sonic's PSG index | decoder | none |
| FM table, B7 | `fm_frequencies`, 97 entries | variant | none |
| `$FA` voice patches | `VOICE_REGISTER` -> a patched copy of the voice (deduplicated), `SET_VOICE` swapped | walk (`voice_patch.py`) | none |
| FM3 special mode | `FM3_OPS` [4 offsets] -> `TrackState`; instrument key | state + catalogue | render ch3 with `$27` = `$40` |
| LFO | `LFO` [freq, AMS, FMS]; global `$22`: last writer | state + catalogue | `ym2612/voice.py` forces B4 `$C0` |
| vibrato | `MOD_SET`, depth word, count + 1 | decoder | check the vibrato formula |
| pan | `PAN` `$80` / `$40` / `$C0` | decoder | none |
| envelope `$83` | `EnvelopeCommand.MUTE`: hold att 15 | `envelopes.py` | none |
| unequal loops | check loop extension against `$8A` `$8C` `$8F` `$91` | - | maybe |

---

## 3. Plan

### Phase 0: refactors (2.1)
- [x] 0.1 Snapshot (2026-10-08, scratch, not kept): every song read (asm, 3 ROMs, fixed and
  shipped: SongCode ops and SmpsSong repr), `rom_import` listings and `--asm`, Sonic's
  `--compare`, the 35 Moonwalker / Golden Axe minimal MODs and derived configs.
- [x] 0.2 2.1 items 1-7, one commit each; each gate passed (regression, tool regression, pytest,
  ruff, pyright, vulture, the snapshot unchanged).  1 `grammar.py` + `SmpsVariant.grammar`;
  2 `flags` per `ChannelType` (`every_kind`), a header's tracks name their kind; 3 `HeaderLayout`
  `tempo` / `EntryLayout`; 4 `VoiceLayout.operator_offsets` / `feedback_last`; 5 `OpKind.NOTE` /
  `DURATION` / `NO_ATTACK` (`track_byte`); 7 `z80_ram`: copy, Kosinski, buffered Kosinski
  (SoR's, found at `$10636`), `kosinski.py` in `core/rom`.  6 not needed: SoR's index is not a
  `Go_` block, so its own locate reuses `read_index`; `locate_68k`'s octave check fails it fast.
- [x] 0.3 Row grid: where the exact grid is too fine, the multiple of it with the most notes on
  rows (was: doubled).  No Moonwalker / Golden Axe song changes (each fits at its exact grid).
  Speeds stay 2-8: a 13-frame grid (`$91`) wants speed 13 for BPM 150 exactly (Phase 4).

### Phase 1: read the ROM (done 2026-10-09)
- [x] 1.1 `SmpsDriver.MUCOM` (`smps68k_mucom`), pinned by SHA-1.  Each table found by the code
  that reads it (a lea after `subi.b #$81,d0` ...); PSG rows 2-6 checked equal to Sonic 1's.
  `HeaderLayout`: no tempo, 3- / 4-byte entries, `psg_slots` (PSG3 PSG2 PSG1).  `VoiceLayout`:
  register order, FB/ALG last.  `fm_frequencies`: 97 words (B7 included).  Envelopes: `$83`
  is `EnvelopeCommand.MUTE` (hold att 15).  The volume table waits for Phase 2.
- [x] 1.2 The grammar: notes, rests, `$00` stop, ties (`NO_ATTACK`), loops (`$F5` a body label,
  `$F6` `LOOP`, `$FE` `OpKind.LOOP_EXIT`), `$FF` jump, pan, vibrato (`MOD_SET`, count + 1),
  detune that sets, PSG noise (`PSG_FORM $E7`), drum samples (`CoordFlag.DAC_SAMPLE`, notes
  `SELECTED_SAMPLE`).  Read and dropped (reported): volume, volume step, gate, register write,
  detune that adds (Phase 2); LFO, FM3 special mode (decision 3).
  The walk: `LOOP_EXIT` (out on the loop's last pass, the tie dropped), a replay or call hands
  its tie state back (no Sonic / Moonwalker / Golden Axe song changes), DAC notes play the
  selected sample.
- [x] 1.3 `rom_import.py`: the 17 index entries read; the 48 SFX listed "not read (music only)".
- [x] 1.4 `tests/test_rom_mucom.py`: the grammar on hand-built bytes; with the ROM: tables, every
  song on its chip channels, what is dropped, envelope 3, `$89`'s loop.
- Read through the real path, every FM key-on of the 15 rips is on its frame.  Not yet: the
  jump back clears a tie (`$8F` FM1 / FM4 / FM5: the note at the loop target attacks on each
  replay, tied on the first pass).
- `$89`'s `$F6` without `$F5`: the walk unrolls a loop from its `$F6`, so it plays as written
  (16 passes) with no data fix: 6.1 is done by this.

### Phase 2: the walk
- [x] 2.1 IR flags and walk rules of 2.2 (2026-10-09), found in the driver's code:
  - **Volume:** `$F1` `VolumeStep`, FM `$FB` `AlterVolumeStep`, resolved in the walk to `SET_VOL` by
    `TrackRules.volume_steps` (each kind's level by signed step), the header volume added
    (`add.b`).  FM: the table at `$73600` read by the code (`ext.w d3` / `move.b (pc,d3.w)`); songs
    step down to -4 (`$88`, `$8F`), which reads `36 33 30 2D` before it.  The step starts at 0
    (the RAM is cleared).  PSG: `$F1 v` att = (-v & 15) + header volume (`$FE` = -2); `$FB n`
    takes n from the att the walk holds, unclamped as the driver keeps it.  Voices: carrier TLs
    read as 0 (`VoiceLayout.carrier_tl`): loading a voice writes them from the volume.
  - **Detune add:** `$F2` with a third byte `DetuneAdd`; the walk keeps the word (`add.w`) and the
    PSG's `>> 4` moved to the PSG's `TrackRules.detune_shift`: the driver shifts the sum (`$8B` PSG3
    adds 100 and -100: 0, not -1).  The vibrato depth still shifts each step (Phase 5.3).
  - **Noise:** in noise mode the driver writes no tone 3 frequency (`noise_writes_tone3`): a noise
    note plays the last tone note's divider, none: `nMaxPSG` (`MAX_PSG`, divider 0).
  - **The jump's tie:** `$FF` clears an FM or drum track's tie (`jump_clears_tie`), not a PSG's.
    `SmpsChannel.replay_tie`: a replay's first note tied or attacking where the jump leaves another
    tie than the first pass reached the label with; loop extension applies it.  `$8F` FM1 / FM4 /
    FM5 attack on each replay; its PSG2 / PSG3 stay tied.
  - **The chip's bits:** a played pitch is the word the chip takes: `$8F` PSG1's detune pushes
    dividers past 10 bits (1024-1031 wrap to 0-7, ultrasonic in the game too).
  - Checked against the rips' frame logs (scratch, at each key-on frame): FM level exact on every
    note of the 15 rips; FM pitch exact but FM3 under special mode (+100, Phase 5); PSG pitch
    exact but 3 wrapped `$8F` notes (the rip reads 0 for 2 and 5); PSG level exact but where the
    envelope is 0 (the att lands a frame after the key, `$91` PSG3) and the logs' last notes.
    FM onsets: every one but the logs' last frames (`vgm_lift`).
  - The lift snaps a rip note to the table and reads no detune (vgz_conversion 1.3): its `note`
    differs where a detune passes half a semitone (`$C3` = 195), and on FM3 in special mode.
- [x] 2.2 Gate and voice patches (2026-10-09), found in the driver's code:
  - **Gate:** `$F3 n` `Gate`, applied in the walk (it looks at the byte after the note): the note
    keyed off n frames before its end, the rest of it a rest, both `SmpsNote.cut` (was `run_out`,
    the run-out's marker too): the row grid takes a cut note's onset, not its length nor the rest.
    A note no longer than n plays whole.  Spared, per kind (`TrackRules`): FM and PSG a note
    the next byte ties (`gate_sees_tie`); FM a tied note, its key-off waiting on the tie bit
    (`gate_spares_tied`; the PSG's silences anyway, `$73A42`); the drum track's cuts every note.
  - **A rest after a tie** keys FM off on its first frame (the key-off at the rest's read waits on
    the tie bit, the next frame's does not), the PSG at once: `tied_rest_holds` (Sonic 1's holds
    through the rest: left out).
  - **Voice patches:** `$FA r v` `VoiceRegister` (r: the track's channel in its low bits);
    `voice_patch.py`: a patched copy per voice and set of patches, the walk's `SetVoice`
    where the write was; the next voice set drops them.  The songs write D1R, D2R and D1L/RR
    (203 writes); a carrier's TL (+ header volume, rewritten after each volume change) is refused,
    none written.  Timers A / B (`$24`-`$26`, MUCOM's tempo) are inert: dropped ("timer write").
  - Against the rips' frame logs (scratch): FM voice registers exact at every key-on of the 15
    (1381 of `$85`'s differ without the patches).  `vgm_lift` FM onsets and lengths: every note
    but the logs' last frames (`$87`, `$89`).
- [x] 2.3 Yardstick (2026-10-09): `python tools/vgm_lift.py --all --configs configs/streets_of_rage`
  (pairs in `rips.yaml`, which names the rips' folder, `streets_of_rage_1`: not the configs'
  mirror; Phase 4.1's 16 minimal configs made for it).
  - **FM onsets and lengths** (`--aspects onset length`): equal on all 15 but the logs' last
    frames (`$87`, `$89`: notes the log cuts).
  - **FM notes:** the lift snaps a rip's frequency to the table and reads ties only from key
    writes, so its `note` differs where the song is right: a detune past half a semitone (`$C3`
    = 195: `$84` `$87` `$88` `$89` `$8B` `$8C` FM2/FM3), FM3 under special mode (+100, Phase 5),
    and a legato slide, which this driver writes as a frequency alone (Sonic 1's re-keys; `$86`
    FM1 E3 -> F3 at 105).  With `note` compared, the slide also splits a length.  Reading
    detune and a key-less slide is the lift's work (vgz_conversion 1.3), not this phase's.
  - **Against the frame logs** (`tools/vgm_frames.py`, made from the scratch check): FM pitch,
    level and voice registers exact at every key-on; a tied note's pitch differs only by the
    vibrato running through it (FM 1-18 units; the PSG's sweeps); PSG pitch and level as 2.1.

### Phase 3: DAC
- [x] 3.1 The sample decoder (2026-10-09; `mucom/dac.py`, the Z80 program Phase 0's): a
  sample's own 16-byte delta table, nibble 0 a run of the last delta (table[0] steps).
  `$81`-`$84` byte-equal to every rip bank that holds them (`$81` `$82` most, `$83` `$84` in
  rips 06, 10, 12, 13).  `$85` is empty (the rests' cut); from `$86` voice clips,
  not read.  First, `dpcm.py`: `PcmTable` (an entry layout) and `SampleFormat` (decode, cycles),
  Sonic 1 and Moonwalker through them.
- [x] 3.2 Rates: counted per output, a literal nibble 190 / 242 (high / low) + 13 a pitch step,
  a run's step 219 + 13, its entry 23 / 75: `$81` 271.3 cycles, `$82` `$84` 229.0, `$83` 372.2.
  The rips between stalls: pitch 1 equal (229.4-229.6), pitch 4 and 12 1.3 % and 2.7 % fast,
  their emulator's djnz as Sonic 1's: the count stands.  The 68k holds the Z80 1.6 % of
  each frame (11.6 of 735 samples, every rip alike): in the rate.  `$81` 12983 Hz, `$82` `$84`
  15381, `$83` 9464.
- [x] 3.3 Rests and gates cut (2026-10-09): `TrackRules.rest_cuts` (`$72AF2`: the sample saved to
  `+$11`, `$85` written), read by `channel_writer._on_rest` (C00).  Onsets against the rips:
  - **Starts without a seek:** a ripper seeks only where its bank does not hold the sample next;
    the frame log now starts a sample where bytes resume after a quarter frame's pause too
    (`DacStart`).  SoR: 8 of 15 equal on every drum onset (was 1).  Sonic 1 and Moonwalker gain
    as much: Title, Ending, Continue equal; Smooth Criminal 79 -> 8, Beat It 113 -> 47.
  - **Driver bug, not simulated:** the gate also fires on a rest and saves `$85` over the saved
    sample, so a note after a rest longer than the gate plays nothing until the next `$F0`.  The
    data writes `$F0` before such rests: the drums were meant.  The rips miss them: `$85` Beatnik
    32 (every hit after 6400), `$89` Stealthy Steps 64, `$8B` Dilapidated Town 23.
  - **Ripper artefacts, left:** a seek to the same sound's next bytes elsewhere in the bank
    (`$87` 24, `$8B` 48); a cut the rip hears first, its bank recorded straight on (`$86` 6); a
    seek pair across a frame boundary (`$88` 8); hits past the walked first pass.

### Phase 4: convert
- [x] 4.1 `configs/streets_of_rage/`: 16 minimal configs and `rips.yaml` (Phase 2.3); all 16 convert.
- [x] 4.2 `vgm_pitch_audit` clean (bar Phase 5 and what is left below); `measure_volumes.py --configs configs/streets_of_rage`.
  - **Pitch (2026-10-09):** 15 rips, 8 clean but FM3 special mode (Phase 5: 7 songs, -200 / -300 c on
    every FM3 note).  Fixed on the way:
    - modulation that cycles too slowly for `4x1` (251 / 255 steps: sweeps, `$90` FM4 +8 FNUM a
      frame) was a full-depth `41F`; now slides to the chip's pitch row by row
      (`modulation_slides`), from the attack or a `ModSet` on a tie (`$8B` FM2).  Moonwalker's Beat It
      sweeps too;
    - `vibrato_depth` reads the song's FNUM, not 644 · 2^(pc/12): Sonic 1's B is 606, a block up, so
      its B notes' `4xy` were half deep (Sonic / Moonwalker baselines: `4xy` depths only);
    - a detune variant carries its interval at its render pitch: +195 is +336 c on F#, +266 c on A#.
      Rendered in its notes' pitch class, notes further than 25 c from it a variant per class
      (`$85` FM1 96 notes, `$84` FM2, `$83` FM3);
    - the audit: a sweep's frame-by-frame steps are no note starts (a move counts from a pitch held
      over a frame), slides are no note-ons to pair with, a PSG sample's pitch is its table divider's
      (B7: divider 29, -42 c; Sonic's pitch audits moved 3-4 c).
  - **Volumes (2026-10-09):** `measure_volumes.py --configs configs/streets_of_rage`, one write
    pass: 193 volumes in 15 songs.  Left over 1 dB: FM3 voices of the special-mode songs (-28 /
    -38 dB, Phase 5), noise samples rendered with one envelope and played with `$00` (+6-8 dB, the
    `noise_envelopes` warning), and instruments whose channels disagree.
  - **Derived configs:** a speed past 8 where 2-8 miss the tempo (`$91`: 150 BPM at speed 13, was 81
    at 7); a noise form's other envelopes a slot each (`$89` `$90` `$91`: `$00` beside `fTone_03`),
    which the converter now renders (its synthesised PSG set missed `envelopes:` variants).  Every
    song at its driver's exact tempo.  `$81` FM1's leading rest finds no row-0 slot (the loop goes
    to row 0): a spare channel (`num_mod_channels`) would carry it.
  - Left: `$8B` 56 notes -40 ... -70 c for a row: a tie re-struck for its level (`legato: retrigger`)
    restarts at the note's period, the slide catches up a row later; `$81` FM2 12 notes: a detune
    sweep (`DetuneAdd` in a loop, 19 detunes) with no free slot (`$87` `$88` `$90` warn too);
    single notes the audit pairs with a re-struck tie, and `$91`'s first second (the rip starts 39
    frames in).
- [x] 4.3 Unequal loops (2026-10-09): `prepare_song` replays every track to the last loop start plus
  the loops' common period, each loop's the shortest its body repeats at.  `$8F` (2304 / 1152 /
  1728 / 4608 / 144) unrolls to 18852 frames (99 patterns), checked against the rip's frame log to
  its end.  `$8A` (4608 / 1152 / 576), `$8C` (4608 from 1536 / 1792 / 2048, PSG3 128) and `$87`
  (PSG1 21 frames late) were in step already; `$91` ends.  `$89`'s PSG3 (5173, the `$F6` quirk)
  against 5120 is past four loops: warned (`loop_drift`), it drifts 53 frames a loop.  Sonic,
  Moonwalker, Golden Axe unchanged (Green Hill's 1024-tick drum loop is a 512 bar twice).
- [ ] 4.4 (left open at the close) Listen in the FT2 clone (`output/streets_of_rage/`); Amiga merged builds if wanted.  Open for
  the user: same-pitch ties re-struck for their level (`legato: retrigger`) or held with `Cxx`.

### Phase 5: chip features
- [x] 5.1 FM3 special mode (2026-10-10): `$F7`'s four bytes (A6, AC, AE, AD: OP4, OP3, OP2, OP1, a
  voice's operator order) are `Fm3Special`; the walk sets a copy of the voice carrying them
  (`SmpsVoice.fnum_offsets`, voice_patch.py), so it is an instrument of its own.  The driver keeps
  the mode once set (bit 2, never cleared); all 0 sounds as normal mode.  Only `64 00 00 00` (OP4
  +100) and `00 00 00 00` are used, on FM3 drums: `$84 $88 $89 $8C $8D $90` (`$83` writes zeros
  only).  Rendered on channel 3 with `$27` = `$40`; playback's pitch adds OP4's offset (the channel's
  A2 / A6).  `vgm_frames`: the 2440 FM3 pitch misses are gone.  The offsets count in the detune
  variants' intervals: a variant per pitch class (+100 is +254 c on C, +193 c on F).  `$90` is short
  of slots: 128 notes play the base sample (`detune_no_slot`).  Volumes: the copies' rows by hand
  from one measured pass (copies renumber the `$FA` ones: 6 rows carried to their new names).
  Left: FM3's one-frame G2 hit (gate 6 of 7, `$84 $8D $90`) -27 ... -30 dB, not the mode.
- [x] 5.2 LFO (2026-10-10): `$FC f p a` is `Lfo(frequency, fms, ams)`, kept on the track;
  `core/smps/lfo.py` gives each attacking FM note a voice copy under its LFO (`SmpsVoice.lfo`):
  the track's sensitivity at the frequency the last `$FC` of any track wrote (header order within
  a frame).  It matters in `$88` (FM5's freq 2 under FM2 / FM4's FMS 6) and `$8B` (FM4's `$FC 0 0
  0` takes FM5 to freq 0).  Rendered with `$22` and B4, the LFO from the note's start; a sustain
  loop spans whole LFO cycles (`find_sustain_loop(cycle=)`: `$8B`'s $0C loops 150 ms, one cycle at
  6.6 Hz, not 31 ms).  Not modelled: a jump clears B4's RAM copy (pan and LFO) for the next voice
  set; every song writes `$FC` at its loop's start.  The render cache now keys a voice's special
  mode and LFO (5.1's copies had been served their base voice's renders).  `$90` then needed 36
  slots: a minimal config plays the least played copies as their plain voice until it fits
  (`copy_no_slot`, 5 copies).  Banking its DAC samples frees 2 at most (3 samples, finetunes 6, 2,
  -3: a bank has one).  Under AMS the render starts a quarter cycle in (Nuked's AM is at its
  quietest on step 0: `$8B`'s copies measured -10 ... +8 dB).  Volumes: every copy from one
  measured pass (stale rows of voices now copies dropped; AMS copies measured again after the
  pre-roll).  Left: slots.  The copies come before the detune variants, so `$88`'s FM3 B notes
  lose their pitch-class variant (130 notes +100 c) and `$90` more (706 / 901).
- [x] 5.3 Vibrato (2026-10-10, after the close): `vgm_compare` on all 15.  Two misreadings fixed:
  the driver's turn moves (Sonic 1's spends a silent step), so a half cycle is count + 1 moves
  and the converter's + 1 counted it twice (`TrackRules.modulation_turn_pause`); the PSG adds
  the depth words' sum >> 4, not each step's (`word_shift`, was `detune_shift`: Moon Beach's
  -20 a step read -2, twice the chip's 1.25).  Rate mismatches 85 -> 7; the rip's frame log
  agrees (PSG2 G#4: 266-272 every 10 frames, the MOD ±19 c).  Left (`docs/smps_variants.md`):
  swings under 4x1, sweeps read as beats, speed-2 rows, and the LFO baked into samples (its rate
  follows the note across a window, restarts with each re-struck tie).
- Not taken: banking DAC samples outside the merged build (frees `$90` 2 slots at most).

### Phase 6: close
- [x] 6.1 `$89`: plays as written without a fix (Phase 1).
- [x] 6.2 (2026-10-10) `docs/smps_variants.md` § Streets of Rage (part 1, moved and brought up to
  date); `docs/architecture.md` already current; CLAUDE.md's index.

### Playback rules (2026-10-09, before Phase 2)
A driver's tables, envelopes, drum names and timing are one `PlaybackRules` (`core/smps/rules.py`)
each song and channel carries; Sonic 1's are `core/drivers/reference.py` (`SONIC1_RULES`, shared
by the drivers that read none of their own).  Nothing below `core/drivers` names a driver's table:
the parser and the lift take rules, the chip renderers take a table or a divider.  How each kind
of track reads (Phase 2's volume steps, gate, ties; Phase 3's DAC rests that cut) is its
`TrackRules` (`PlaybackRules.tracks`), resolved by `core/smps/driver_track.py`.  `CoordFlag` is a
meaning (a `StrEnum`), no driver's byte: Phase 2's new flags are names, not values parked past
`$FF`; a driver's own (`DriverEffect`) never reaches an event.

### Test selection (the user, 2026-10-08)
Hundreds of songs cannot each be a regression case.  Phase 0 uses a one-off snapshot of
everything (scratch, not kept).  Later: cases chosen by coverage (each driver, IR flag, walk rule,
config feature exercised at least once), a song added only for what no case covers yet.

### Decisions (the user, 2026-10-08)
1. Name: `smps68k_mucom`.
2. `$89`: played as written (no `RomFix` was needed: 6.1).
3. FM3 special mode and LFO: dropped with a report first; Phase 5 later.
4. Regression: no SoR cases.
5. Music only: the SFX are not read (`rom_import.py` lists them).

Branch: `streets_of_rage`.
