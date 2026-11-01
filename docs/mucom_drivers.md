# MUCOM drivers

Drivers that are not SMPS but play MUCOM88's track language (the PC-88 MML compiler's): Mucom-MD
0.85, Streets of Rage 2's.  Streets of Rage's driver reads the same language on SMPS's engine
(`docs/smps_variants.md` § Streets of Rage).  The language is the framework's
(`core/rom/grammar/mucom.py`), each driver a dialect of it; Mucom-MD fits the SMPS framework through
its hooks (`grammar`, `music_header_reader`, `voice_reader`, `rules_from_rom`, `dac`), its names
unchanged (`SmpsVariant`: revisit at a third engine).  How it was found and built:
`docs/todo/done/streets_of_rage_2.md`.

---

## Mucom-MD 0.85: Streets of Rage 2

`Streets of Rage 2 (USA).md`, `GM MK-1054 -00`, SHA-1 `8b656eec…`.  `$40010`: `"  MUSIC DRIVER  Mucom-MD
Ver0.85Programmed by T.Maruyama(M.N.M Software)"`: a 68k port of MUCOM88, its songs MUCOM88's
compiled images byte for byte, on an engine of its own (`core/drivers/mucom/md/`).  Read with
`tools/rom/disasm.py` (the 68k's code, its jump tables, the Z80 program) and
`tools/rom/flag_census.py` (every flag the songs hold).

**Where things are** (each found by the code that reads it: `locate.py`)

| What | ROM | Note |
|---|---|---|
| Entry points | `$40000` | music update, SFX update (V-int), a command (`$40008`, d0 word), init |
| Sound index | `$429CE` | longs relative to the table: `$00`-`$1B` music, `$20`-`$56` SFX (listed, not read) |
| Track RAM | `$FFA014` | `$A0` bytes each: FM1-3, PSG1-3, FM4-5, the drum track (FM6) |
| FM update | `$40E46` | flags `$41B74`; the `$FF` prefix's `$41BA8` |
| PSG update | `$42030` | flags `$423E8`; envelope `$422F8` |
| Drum update | `$4262E` | flags `$4285A`; trigger `$427B2` |
| Tables | `$4291A` `$4293E` `$42956` `$4296E` | FM volume (20 TLs), FM octave (Streets of Rage's, byte-equal), PSG octave (dividers x 8), 16 ADSR presets |
| Z80 player | `$559C2` | 8 KB copied raw to Z80 RAM (`$40DA2`) |

**Song header** (MUCOM88's image, `header.py`): `$C200.w end.w $00 voices.w ?.w $00`, then 11
channels A-K as `ptr.w loop.w`, little-endian, relative to +9.  A-C FM1-3, D-F PSG1-3, H-I FM4-5,
K the drum track (FM6 as DAC); G (rhythm) and J (FM6) are empty in every song.  No tempo (a tick is
a V-int), no track volume or transposition.  A track's `$00` jumps to its header loop, or stops.

**Track code** (`grammar.py`): `d n` a note (`d` frames, `n` octave << 4 | semitone), `$80|d` a rest,
`$00` the end, `$F0`-`$FF` flags, one table per kind of track:

| Byte | FM | PSG | Drum track |
|---|---|---|---|
| `$F0 n` | voice n | ADSR preset n | sample `$80 + n` |
| `$F1 v` / `$FB n` | volume step v - 2 / += n, clamped 0-19 | volume / += n, 0-15 | (FM's) |
| `$F2 lo hi m` | detune word (m 0 sets, else adds) | same, `>> octave` as it plays | skipped |
| `$F3 q` | gate: key-off q frames before the end | same | - |
| `$F4` | vibrato (set, off, on) | same, depth x 266/256 | ignored |
| `$F5` `$F6` `$FE` | loop start, end, exit on the last pass | same | same |
| `$F7` | FM3 special mode offsets | n: take the noise channel; 8: give it back | skipped |
| `$F8 n` | pan (1 R, 2 L, 3 C) | noise rate (n 0-11, 12-22, more: 0, 1, 2) | pan |
| `$FA` | a YM write, queued (up to 8) | six bytes: an ADSR envelope | a YM write |
| `$FC` | LFO | no operands | no operands |
| `$FD` | tie | same | same |
| `$FF $F3 n` / `$FF $F5 x` | soft release at level n / on or off | same | skipped |

**How it plays** (`variant.py`: Sonic 1's `TrackRules` with what differs replaced)
- **Volume** (`$41C02`): the step kept is v - 2, at most 19 (`volume.step_range`); TL =
  table[step] on the carriers.  A volume write (`$41A9A`) rewrites all four TLs, a modulator's from
  the voice, so a `$FA` TL write holds to the next one (`volume.tl_writes_hold`).
- **Gate:** FM: a note no longer than its gate keys off after one frame (`key_off.gate_floor`,
  `$40F72`); PSG: it plays whole.  The gate spares a note the next byte ties.
- **Soft release** (18 songs; `SoftRelease`, `key_off.soft_release`): nothing keys off; at the gate
  the FM note drops to TL = table[(step + n) >> 1] and rings through the rests; the next note keys
  off, then on.  PSG: att = ((v - n) >> 1) ^ 15.  `$FF $F5 0` ends it: a note still ringing is keyed
  on alone (the next slides on).  A voice set keys a ringing note off (`$410F4`); a PSG envelope's
  note clears it (`$420FE`).
- **Ties:** a `$FD` read after a flag sets the tie bit after the note's end keyed it off: the next
  note attacks (`ties.late_keys_off`, `$41C9C`).  The loop jump and `$FE` keep the tie bit.
- **Vibrato:** the counter starts at count >> 1, the turn moving too (`modulation.turn_pause`
  False, `first_leg_short`).
- **PSG pitch:** divider = word >> octave, clamped 1-1023 (`pitch.divider_clamp`); (detune +
  vibrato) >> octave (`pitch.octave_shift_from`: a constant interval).  The playable run is A2-B8;
  o1 C-G# clamp to 1023, read as A2 (12 c sharp: one note, `$10`).
- **PSG envelopes** (`core/smps/adsr.py`): level 0-255 from AL, + AR to 255, - DR to SL, - SR to 0
  (SR 0 holds), at the gate - RR a frame; att = ((level x (v + 1)) >> 8) ^ 15.  Each (envelope,
  volume) is one instrument (`adsr_ffffffbe000a_v12`); presets used 0 (flat) to 4.
- **PSG noise:** `$F7 n` moves the track onto the noise channel (`$E4`, white, a fixed rate), its
  tone left unwritten; `$F7 8` gives it back (`PsgForm(None)`).  Fixed-rate noise plays on a
  `NOISE` channel of its own.
- **Drum track:** a note plays sample `$80 + n`; a tie asks for none (`ties.drum_restarts` False,
  `$4277A`); rests and gates do not cut.
- **DAC** (`dac.py`; Streets of Rage's player, rewritten): table at Z80 `$01BF` (start, size, pitch),
  the run-DPCM format (`core/rom/pcm/sor.py`), samples `$81`-`$84` (songs play `$81`-`$83`; from
  `$86` voice clips, banked, not read).  Its loop: a nibble 186 / 246 cycles, a run's step 219, + 13
  a pitch step; the rips play at that count.  The 68k holds the Z80's bus 1.5-3.1 % of the time by
  song: the median 1.8 % (`RUN_DPCM.stall`; pitch 4: 12894-13040 Hz).

**Against Streets of Rage** (`smps68k/streets_of_rage`): the same notes, rests, ties, loops,
voices, FM octave, special mode and LFO, and the bytes of most flags.  Not: the engine and container
(MUCOM88 images, no header volumes), `$F1`'s clamp, pan (L and R swapped), `$FF` (a prefix, not a
jump), the FM gate floor, the vibrato's count, the PSG (its own table and octave shift, ADSR
envelopes, noise on any track at a fixed rate), the drum track's rests (Streets of Rage's cut).

**Against the rips** (`configs/streets_of_rage_2/`, `rips.yaml`: 23 of the pack's 24; Walking
Bottom, the beta's, matches no song; `$04`, `$12`, `$17` have none)
- `vgm_frames`: every FM attack's level, voice and pan exact in all 23, its pitch in 21; 19 songs
  every attack (FM, PSG, noise) exact.
- `vgm_lift`: FM onsets and lengths exact in 20 of 23; DAC onsets and lengths in all 23 (its starts
  found by the ROM's sample bytes: the rips' banks are the stream as heard, a drum cutting into
  another shows no seek).
- `vgm_pitch_audit`: 15 of 23 clean.  Left: modulation sweeps a MOD's vibrato cannot play (depths
  -150 ... 598: Never Return Alive, Player Select), PSG detune on high notes (a divider's -1 is 37 c
  there; PSG detunes take no variant: SOR Super Mix, Dreamer, Back to the Industry), Wave 131.
- Volumes measured (`measure_volumes.py --configs configs/streets_of_rage_2`, one pass: 376).
- Known gaps: state the loop's jump carries into the replay (a gate, a soft release, a detune word:
  Go Straight's FM5); a late `$FD` that keeps a same-pitch note's vibrato running (Wave 131 FM3).

**Slots:** 7 songs need 33-53 instruments as derived (many voices, `$FA` copies, an envelope at
each volume).  A bank (`9xx`) holds one looped sound, and these sustain or fade over seconds, so the
plain build does not bank: a minimal config gives way as `docs/yaml_config.md` says (an envelope at
its most played volume, copies, merged windows, special-mode copies last).  All 26 fit.

**Not done:** the SFX, the voice clips; listening (the user).
