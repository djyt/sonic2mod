# Space Harrier II: an early SMPS Z80

Planned 2026-10-10.  Goal: `convert.py` reads Space Harrier II's songs from the ROM, as it reads
Sonic 1, Moonwalker, Golden Axe and Streets of Rage (`docs/smps_variants.md`).  Branch
`space_harrier_2`.

Status key: `[ ]` open, `[x]` done.  Three parts: what the driver is (1), what the code must change
(2), the plan (3).

---

## 1. Analysis (probe 2026-10-10)

Moved, with what the phases found, to `docs/smps_variants.md` § Early SMPS Z80: Space Harrier II.

---

## 2. What the code must change

### 2.1 Refactors (behaviour unchanged, every baseline byte-identical)

1. [x] **Z80 programs kept apart** (`ab2e876`): `z80_loads`, the verified copy; Kosinski refuses
   bytes that are no stream (Super Thunder Blade crashed the reader).
2. [x] **The driver among them** (`ab2e876`): `smpsz80/program.py` `driver_ram`, `fm_table`
   (from Type 0 FM's `locate.py`).
3. [x] **Header readers per variant** (`c18eb37`): `SmpsVariant.music_header_reader`,
   `sfx_header_reader`.
4. [x] **Voice reader per variant** (`c18eb37`): `SmpsVariant.voice_reader`.
5. [ ] **Drum model** (`core/smps/percussion.py`, `core/synth/fm_render.py`, `fm_samples.py`,
   `core/convert/fm_drums.py`, `core/plan/instruments.py`).  `FmDrum` is one voice, one frequency
   word a frame, FM only.  Generalise: a frame carries a word per operator (special mode:
   `fm_render._set_special_freq` exists), and an optional PSG part (a divider, noise mode and
   attenuation a frame: `psg_render` has no frame-by-frame renderer yet), mixed into the one-shot.
   Golden Axe's drums render byte-identical.

### 2.2 New concepts

| Fact | IR | Resolved in | Downstream |
|---|---|---|---|
| track-list header, tempo table | `SmpsSongHeader`; the drum track once (FM3 + PSG3 one DAC channel) | header reader | none |
| tempo hold every n frames | `tempo_modifier` as Sonic 1's TempoWait (check the phase) | header | none |
| register / value voices | `SmpsVoice` | voice reader | none |
| legato `$EE` | persistent tie: `NO_ATTACK` before each note while on | walk | none |
| alter / set volume | `ALTER_VOL`, `SET_VOL` | decoder | none |
| pan, AMS / FMS, LFO | `PAN`, `LFO` (Streets of Rage's) | decoder | none |
| pitch envelope | new: `PitchEnvelope` effect, the table in `PlaybackRules` | walk | baked: an instrument per (voice, envelope), rendered with it (D1) |
| pan animation | the next pan step at each note read (`$0489`) -> `PAN` | walk | `8xx`, the lowest effect (D2) |
| follow-on song `$E9` | dropped: each song converts alone (D3) | decoder | none |
| FM / PSG drums | the drum model (2.1.5) | drum reader | render |

---

## 3. Plan

### Phase 0: refactors
- [x] 0.1 2.1 items 1-4; gates passed (regression, tool regression, pytest, ruff, pyright,
  vulture, layers).
- [x] 0.2 Merge main (`c254462`: chips in `core/chips/`, renderers in `core/synth/`); gates passed.

### Phase 1: read the ROM (done 2026-10-10, `1f71fa0`)
- [x] 1.1 `SmpsDriver.SH2` (`smpsz80_sh2`; renamed for the family once a second game reads),
  `core/drivers/smpsz80/sh2/`, pinned by SHA-1 in `games.py`.
- [x] 1.2 Locate by shape (`locate.py`): the bank from the run of 9 writes that maps inside the ROM
  (the other maps `$C00000`, the PSG); the song loader's `ld hl` operands (tempos, index; as many
  songs as the tempo table has bytes: 25); the voice flag's (voices).  SFX: not listed (Z80 RAM, D4).
- [x] 1.3 Track lists (`header.py`): the slot decides how a track plays (slot 3 the drum track,
  slot 7 its PSG half, checked to share its pointer); one divider per song; the tempo by song
  (`Sh2Memory.tempo`).  Record byte 7 is never loaded at the start: 117 FM tracks set a voice before
  their first note, 7 start with a rest.
- [x] 1.4 Flags (`variant.py`): the table in `docs/smps_variants.md`; dropped and reported: legato, pitch envelope
  (Phase 2), the follow-on song (D3), the drum track's own flags (Phase 3); unused ones refused.
  Voices (`voices.py`): register lists through `core/rom/voices.py`'s `voice_from_registers`; voices
  18 / 20 / 22 are patches, not voices: an `$EF` naming one is refused (none does); `$BC` in 73 / 74
  is no register (no pan).  FM table from the driver (`program.py`).  No `$7F` drum byte in any song.
- [x] 1.5 `rom_import.py` lists the 25 songs, `song_dump.py` reads, walks and plays each; read
  snapshot `read_space_harrier_2`.
- [x] 1.6 `tests/core/drivers/smpsz80/sh2/`: header, voices and flags on hand-built bytes; the
  tables and every song with the ROM.
- Found for later:
  - `$81` sounds near B0 (fnum `$269`, block 1), not Sonic 1's C0: samples render at the table's
    word, so the pitch is right, but note names in reports read 11 semitones low (check in 4.1).
  - `$84` FM1 plays a note before any voice (it inherits the chip's): Phase 2.

### Phase 2: the walk
- [x] 2.1 Legato `$EE` (`4cefe8e`): `Legato`, a DriverEffect; while on, each read starts as
  smpsNoAttack leaves it (a rest still keys off: its word, the table's entry 0, is 0, and a word
  of 0 keys off, `$032C`).  The "34 ties held in `$81`, 1 in `$94`" first counted here were rests
  the walk tied (a keyed-off channel keeps its pitch); fixed in Phase 4: Harrier Saga's FM4 rang
  on from pattern `$18`.  No song ties a note under legato.
  Volume, transposition, divider and pan were Phase 1's; AMS / FMS / LFO: no song uses them.
- [x] 2.2 Tempo holds: Sonic 1's TempoWait at phase 0 (the counter loaded as the song starts, the
  same frame's check after it): the tempo-3 songs' key-ons land 1.5 frames a tick, every one.
- [x] 2.3 Pitch envelopes, baked (D1).
  - [x] Read, walked, played (`2d56755`): `core/smps/pitch_envelope.py` (steps, played a frame at
    a time from each read), `SetPitchEnvelope`, `PlaybackRules.pitch_envelopes`, the header's
    starting envelope; the played pitch is the read frame's.  Every rip's FM pitch, frame by frame
    after each read, as the envelopes say.
  - [x] Baked (`6da218a`): the detune plan keys a sample on (detune, envelope): each instrument's
    own sample takes its commonest pair, any other a free slot (Handcuff: one own, three
    variants).  `render_layers` steps a layer's envelope a frame at a time through the release;
    an enveloped instrument is not looped; the render cache keys on the steps.
  - Approximations: a sample rendered at one pitch carries the envelope's depth and timing scaled
    to the notes it plays elsewhere (no pitch-class split for envelopes yet); a tie under legato
    does not restart the envelope.  Both to hear in Phase 4.
- [x] 2.4 Pan animation (D2, `32383b7`): flag bit 6; every read steps the driver's first list
  (C L C R, again and again: the driver never sets another).  The walk emits `PanStep`, a Pan to
  everything that reads one but the level: the MOD pans it, so no hard-pan dip and no `Cxx` for
  it.  The writer puts each step as `8xx` last, where no other effect is, not where the pan
  already is ($8B's FM4: all 237).
- [x] 2.5 Yardstick (`4cefe8e`): `configs/space_harrier_2/` (20 minimal configs), `rips.yaml`.
  The follow-on bytes (`$E9`) are resume points: `$81`-`$86` name each other (the stage theme's
  sections), the rest `$C1` / `$AF`.  `vgm_lift` misreads the tempo-3 songs' holds as tempo
  changes: `vgm_frames` is the yardstick here.  With the rip glitches undone (merge of
  `rip_glitches`: `$81`'s five lost V-ints and `$96`'s one logged in rips.yaml), 19 of 20 play every
  attacking note.  `$98` FM4 (Phase 4): its part played 768 frames later in the rip than the walk
  had it.  The track opens on two rests and `$AE` before any duration byte, and the driver keeps a
  duration in a byte it counts up to (`$024E`: `inc (ix+11) / ld a,(ix+10) / sub (ix+11)`), from
  byte x divider by adds (`$0449`): none read yet (0) lasts 256 ticks, a product past 255 wraps.
  `TrackRules.byte_durations`; the three notes at 256 put every FM4 attack on its rip's frame.
  The jump back leaves 18 (6 x 3), so the second pass plays them at 18 (the rip: frames 3217,
  3235, 3253): the walk now walks a second pass where a jump leaves another duration than the
  label's opening notes took, and loops on it (`code.py` `_replay_differs`; no other game's song
  has one).  Its loop, 2502 ticks against FM2 / FM5's 3456, drifts on the hardware too: the MOD
  cannot unroll it (the converter's warning).  The longer walk reached a lost V-int at 3458
  (rips.yaml).  879 attacks, every pitch and level as the rip.
- Found with it: the rip's FM4 voice differs in one register (100 attacks): OP1's SSG-EG (`$90`)
  is `$FF`.  The `$F2` stop (`$06D3`) silences a track through the RR / TL list at `$0A95`
  offset by `channel and 7` (2, 5 and 6 special-cased), so FM4's lands an operator slot high:
  RR `$FF` on `$84`-`$90`, TL `$7F` on `$44`-`$50`.  No voice list writes `$90`, so it stays until
  something clears it: Game Over's FM4 stop writes it (frame 385), and the Title Screen's rip
  opens with the chip so (its frame-0 state).  Which song plays with it depends on what stopped
  before: not converted (don't simulate bugs); to confirm with the user.

### Phase 3: drums (done 2026-10-10, `276699a`)
- [x] 3.1 The drum model: an FmFrame's word per operator and key mask (special mode), an FmDrum's
  PSG part frame by frame; `render_frames` plays them, `render_psg_frames` the PSG part,
  `generate_fm_drums` mixes it in (`fm_synthesis.drum_psg_db`).  Golden Axe byte-identical but for
  one fix found here: `render_frames` dropped each register write's chip time (2 samples a write),
  so drum frames ran 0.45 % short and sharp (1.8 % with four operators); its three cases' drum
  samples changed, nothing else.
- [x] 3.2 `sh2/drums.py`: each drum byte played as the driver does, every table found by the code
  that reads it; D6 and D7 as heard; a register a list leaves out keeps what the other lists write
  (OP2's D1L/RR `$4F`, every rip's).
- [x] 3.3 Against the rips (a scratch check, each hit's own operators): keys, OP4's word and the PSG
  part on every frame of every hit, but `$81` (drifting) and `$96` (a frame off from the start),
  and 7 PSG frames in `$8A` / `$90`.
- `drum_psg_db` measured (Phase 4): -5.7 dB.  Each rip's FM3 alone against its PSG3 + noise alone
  (VGMPlay), the energy of every hit to the next against ours at 0 dB: 19 songs, -5.2 to -6.9 dB,
  median -5.69 - VGMPlay's mix (the SN76496 at half the YM2612's volume, -6.0 dB) less the 0.3 dB
  our TL 0 carrier's 789 misses by.
- Open: a drum hit cuts the last on the drum track's MOD channel, where the chip lets a unit ring
  on under a hit that does not retrigger it.

### Phase 4: convert and verify (2026-10-10)
- [x] 4.1 Every song converts.
  - `drum_psg_db` measured (above); `$98`'s FM4 (2.5).
  - `vgm_pitch_audit`: every FM note read wrong, as if each sounded at its SMPS name; this table
    plays each 11 semitones and 26 c above it.  `sounding_pitches` now adds the table word's cents
    (as the PSG divider's): 7422 of 7424 right.  The other two were Game Over's: three long notes
    made a 96-tick grid, 2.3 BPM at any speed, clamped to 32 (13.6x fast); derive now divides a
    grid too coarse for any BPM (`_refined`): all 7424.
  - `measure_volumes`: two derive fixes first.  A window's volume came from the song as read and
    the converter's baked level from the prepared one (loops replayed): near a tie they chose
    apart, An Epitaph's FM6 at `C00`; derive now counts the prepared song.  Every FM drum derived
    64; one keying only a quiet pair (drum81) measured 13-19 dB loud: it now starts that far below
    its voice (`keyed_carriers`).  Then one pass: 147 volumes, pitch 20/20 in `vgm_compare`.
  - Left (to hear or later): drum81's level varies by song (+3 to +9 dB in `$8C` `$95` `$99`); a
    voice's quieter channels play a sample rendered at its loudest's level (An Epitaph's FM6, ~7 dB
    quiet: one render level per instrument); `$98`'s FM4 SSG-EG (above: not converted, to confirm);
    loops out of step where the tracks' loop lengths share no short period (`$98` FM2 / FM5, the
    drum track in `$8D` `$92`: the hardware drifts too); the pitch envelope approximations (2.3).
- [x] 4.2 Five cases in `tests/cases.yaml`, by line coverage over the 20 songs (An Epitaph, Harrier
  Saga, Game Over, Motion, Machine Henge).  `frames_` / `glitches_` / `read_space_harrier_2` in
  tool_regression; its pitch audits now run every game's cases against their own rips (Moonwalker,
  Golden Axe and Space Harrier II: every note right).
- [x] 4.3 `docs/smps_variants.md` § Early SMPS Z80: Space Harrier II; CLAUDE.md index.

### Decisions (the user's, 2026-10-10)
- **D1 Pitch envelopes:** baked into each note's sample.
- **D2 Pan animation:** `8xx`, the lowest-priority effect.
- **D3 `$E9` follow-on:** each song alone.
- **D4 SFX:** music only.
- **D5 The family:** no further games yet.
- **D6 Drum unit B's record:** `$0BF0`, as heard.  The code meant `$0BE6` when bit 5 is clear
  (`ld hl,$0BE6 / bit 5,a / ld hl,$0BF0 / jr nz,+0`), but no song sets bit 5: the game, and the
  composer, only ever heard `$0BF0`.
- **D7 The op-Y step:** as the driver does it.  It stores op Y's low byte + step into the high byte
  (`$0DA5`), so OP2 / OP4 jump a block on frame 2 (the rip: OP4 42/2, then 1322/5); fixed, OP4 would
  sit near 4 Hz.  D6 and D7 refine "don't simulate bugs": a bug that shapes an instrument's
  sound on every play, which the songs were written against, is played as heard.
