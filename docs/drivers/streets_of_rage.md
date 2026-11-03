# Streets of Rage: SMPS 68k with MUCOM track code

Streets of Rage (*Bare Knuckle*) runs SMPS 68k's engine — Sonic 1's sound RAM, queue, tracks and DAC
mailbox — but its songs are written in MUCOM88's track language, with no tempo and absolute volume
steps.  For anyone converting its songs, or comparing it with Streets of Rage 2's Mucom-MD
([streets_of_rage_2.md](streets_of_rage_2.md)), which reads the same language on an engine of its own.

On this page:
- [Summary](#summary)
- [Where things are](#where-things-are)
- [How it differs from Sonic 1](#how-it-differs-from-sonic-1)
- [Track code](#track-code)
- [How it plays](#how-it-plays)
- [DAC](#dac)
- [How it converts](#how-it-converts)
- [Known limits](#known-limits)

## Summary

| | |
|---|---|
| ROM | `Bare Knuckle - Ikari no Tekken ~ Streets of Rage (World) (Rev A).md`, `MK 00001019-01`, SHA-1 `731cdf18…` |
| Driver | `smps68k_streets_of_rage`, `core/drivers/smps68k/streets_of_rage/` |
| Songs | 17, `$81`-`$91` (`$8D` = `$8E`) |
| Configs | `configs/streets_of_rage/`: 16 minimal configs, `rips.yaml` (rips in `reference/vgz/streets_of_rage_1/`) |
| Source | no disassembly: the driver (`$72800`-`$73C16`) read with capstone, the Z80 player with `z80dis`; every fact checked against the 15 rips |

## Where things are

Each table found by the code that reads it (`streets_of_rage/variant.py`: the `lea` after a known
instruction).

| What | ROM | Note |
|------|-----|------|
| Driver entry | `$72914` | V-int; PAL: a second call every 6th frame (58.3 Hz) |
| Music index | `$7288C` | 17 absolute longs, `$81`-`$91` |
| SFX index | `$73B56` | 48 longs, `$A0`-`$CF`: listed, not read |
| FM octave | `$732D2` | 12 words C-B (`$284` … `$4C0`); block = octave: 97 words, C0-B7 |
| PSG table | `$73A64` | 10 rows of 12; rows 2-6 are Sonic 1's (checked as the ROM is read) |
| FM volume | `$73600` | carrier TL by step 0-20; songs step to −4 (`36 33 30 2D` before it) |
| Carrier masks | `$73728` | per algorithm |
| PSG envelopes | `$728D0` | 5; `$81` holds, `$80` restarts, `$83` silences (`EnvelopeCommand.MUTE`) |
| Flag tables | `$73302` FM and drum track, `$73342` PSG | one table per kind of track |
| Z80 player | `$795A2` | Kosinski, unpacked to `$FF7000`, then copied to Z80 RAM |

**Song header:** `voices.w fm.b psg.b`; FM entries `ptr.w volume.b` (FM1-FM5, then the drum track);
PSG entries `ptr.w volume.b envelope.b` (tone 3, 2, 1).  No tempo, no pitch.

## How it differs from Sonic 1

**The same:** sound RAM `$FFF000`, queue `$FFF00A`-`C`, sound IDs (music `$81`, SFX `$A0`, `$E0`-`$E3`
commands), priority table, `$30`-byte tracks (FM1-FM5 `$FFF040`, DAC `$FFF130`, PSG `$FFF160`), the Z80
DAC mailbox (`$A01FFF`), the relative jump.

| | Streets of Rage | Sonic 1 |
|---|---|---|
| Track bytes | MUCOM's: `d n` (duration first), `$00` ends | SMPS's: note, then duration |
| Flags | `$F0`-`$FF`, a table per kind of track | `$E0`-`$F9`, one table |
| Tempo | none: a tick is a frame | TempoWait, dividers |
| Loops | `$F5` / `$F6` / `$FE` on a per-track stack | `smpsLoop` slots |
| FM volume | absolute step → carrier TL table, + header volume | attenuation added to the voice's TL |
| Voices | 25 bytes in register order (DT/MUL, TL, KS/AR, AM/D1R, D2R, D1L/RR), FB/ALG last | SMPS order, FB/ALG first |
| Vibrato | count + 1 moves a half cycle, no pause at the turn | a pause at each turn |
| Drum track | rests and gates cut the sample | a rest lets it play out |

## Track code

`core/rom/grammar/mucom.py`, its dialect `streets_of_rage/grammar.py`.  `d n` is a note (`d` `$01`-`$7F`
frames, `n` octave | semitone), `$80|d` a rest, `$00` the end, `$F0`-`$FF` a flag.  Durations stop at
127: longer notes are tie chains.  FM octave o plays block o; PSG row r plays octave r + 1 (rows 0-1
read row 2, 6-9 row 6).

| Byte | FM | PSG | Drum track | The IR |
|---|---|---|---|---|
| `$F0 n` | voice n | — | notes play sample `$80` \| n | `SetVoice`, `SelectSample` |
| `$F1 v` | volume step v | att (−v & 15) + header volume | — | `VolumeStep` → `SetVol` |
| `$F2 lo hi m` | detune word: m 0 sets, else adds | the word >> 4 | same | `Detune`, `DetuneAdd` |
| `$F3 n` | [gate](../glossary.md#gate): key off n frames before the end | same | cuts the sample | `Gate` → note + rest |
| `$F4 0 d s w c` | vibrato: delay, speed, depth word, count | the sum >> 4 | — | `ModSet` (count + 1) |
| `$F4 1` / `2` | vibrato off / on | same | — | `ModOff`, `ModOn` |
| `$F5` / `$F6 x n o` / `$FE o` | loop start / end, n passes / leave on the last pass | same | same | label, `LOOP`, `LOOP_EXIT` |
| `$F7 a b c d` | FM3 special mode: OP4, OP3, OP2, OP1 fnum offsets | white noise at tone 3's rate | refused | `Fm3Special`, `PsgForm` |
| `$F8 n` | pan: 1 L, 2 R, else C | — | pan | `Pan` |
| `$F9` | pause toggle (refused) | envelope | refused | `PsgVoice` |
| `$FA r v` | YM register write: the voice patched | envelope | YM write | `VoiceRegister` |
| `$FB n` | volume += n steps | att −= n | — | `AlterVolumeStep`, `AlterVol` |
| `$FC f p a` | LFO: `$22` = f \| 8, B4 FMS p AMS a | hangs the driver (refused) | LFO | `Lfo` |
| `$FD` | tie | same | same | `NO_ATTACK` |
| `$FF w` | jump; clears the tie (FM, drums) | jump | jump | `JUMP` |

## How it plays

Each kind of track's [`TrackRules`](../glossary.md#trackrules) (`core/smps/rules.py`, walked by
`core/smps/driver_track.py`): the switches named below are its fields.

**Volume.**  FM carrier TL = table[step] + header volume (`add.b`), absolute: a voice's own carrier TLs
never play (`VoiceLayout.carrier_tl`).  A `$FB` in a loop crescendos.  PSG: attenuation + envelope
step, clamped to 15.

**Detune.**  Added to the 14-bit block|fnum word; the PSG takes the word >> 4 (`pitch.word_shift`).  Up
to +195: several semitones, played as [detune variants](../glossary.md#detune-variant) per pitch class
([../conversion/detune.md](../conversion/detune.md)).  A word past the PSG's 10 bits wraps: `$8F`
PSG1's ramp runs B7 down to A2, then from the top again; its notes play as the pitches they reach.

**Ties and the gate.**
- Every note keys off, then on; after `$FD` neither: a new pitch slides legato, written as a frequency
  alone.
- The gate keys off n frames before the end, but not a note the next byte ties
  (`key_off.gate_sees_tie`) nor, on FM, a tied note (`key_off.gate_spares_tied`).  A note no longer
  than n plays whole.
- A rest after a tie keys FM off a frame in, the PSG at once (`key_off.tied_rest_holds` 1 / 0).
- A jump drops an FM or drum track's tie, not a PSG's (`ties.jump_clears`).

**Vibrato.**  SMPS's shape, but the count is tested before it counts down and the turn's step moves
too: a half cycle is count + 1 moves, no pause (`modulation.turn_pause` False), the first leg half the
count (`modulation.first_leg_short` 1).  The PSG adds the depth words' sum >> 4, not each step's.
Cycles too slow for `4x1` play as slides.

**Voices.**  `$FA` writes (D1R, D2R, D1L/RR; 203 in the songs) last until the next voice: a patched
[voice copy](../glossary.md#voice-copy) (`core/smps/voice_patch.py`).  `$FA $24`-`$26` (MUCOM's timers) are inert.

**FM3 special mode.**  `$F7` stores four offsets the FM3 track's frequency writes add per operator
(`$72CAE`: A6/A2 OP4, AC/A8 OP3, AE/AA OP2, AD/A9 OP1) and sets `$27` = `$40` when any is nonzero.  The
mode is never cleared; zeros sound as normal mode.  The songs use only OP4 +100, on FM3 drums (`$84`
`$88` `$89` `$8C` `$8D` `$90`): converted as a voice copy carrying the offsets, rendered on channel 3.

**LFO.**  `$22` is the chip's, so a note plays at the frequency the last `$FC` of any track wrote
(`core/smps/lfo.py`; `$88`, `$8B` depend on it).  B4's sensitivity survives voice sets and pans.
Converted as a voice copy under the LFO, its loop spanning whole LFO cycles.

**PSG noise.**  `$F7` on tone 3 writes no tone 3 frequency (`pitch.noise_writes_tone3` False), so a
noise note plays the last tone note's divider, or 0 (`nMaxPSG`) before any.

## DAC

`streets_of_rage/dac.py`; the Z80 player's table at Z80 `$019B`, 5 bytes a sample: start.w, size.w,
pitch.b.

- Music samples `$81`-`$84`.  `$85` is empty (size 0): the drum track's rests and gates play it, so
  they **cut** the sample (`key_off.rest_cuts`: `C00`).  From `$86`: voice clips, not read.
- Format (`core/rom/pcm/sor.py`): each sample has its own 16-byte delta table (byte 0 = a run length);
  nibble n adds table[n], nibble 0 repeats the last delta table[0] times.  Byte-equal to every rip bank
  that holds them.
- Rate: cycles per output — a literal nibble 190 / 242 (high / low), a run step 219, each + 13 a pitch
  step — less the 68k's 1.6 % stall: `$81` 12,983 Hz, `$82` `$84` 15,381, `$83` 9,464.

## How it converts

```bash
python convert.py configs/streets_of_rage
python tools/vgm/vgm_lift.py --all --configs configs/streets_of_rage
python tools/vgm/vgm_frames.py --all --configs configs/streets_of_rage
python tools/vgm/measure_volumes.py --configs configs/streets_of_rage
```

Tracks that loop at unequal lengths (`$8A`, `$8C`, `$8F`, `$91`) are replayed to their common period
([../conversion/song_structure.md](../conversion/song_structure.md)).

**Against the rips:**
- `vgm_lift`: FM onsets and lengths equal on all 15 but the logs' last frames.  Its `note` differs
  where the lift cannot read: a detune, special mode, a key-less slide.
- `vgm_frames`: FM pitch, level and voice registers exact at every key-on, special mode included.
- `vgm_pitch_audit`: clean but where sample slots run out (`$88` FM3 B notes, `$90`: voice copies take
  the slots before detune variants — `copy_no_slot` / `detune_no_slot`), `$8B`'s re-struck ties a row
  late, and `$91`'s first second (the rip starts 39 frames in).
- Volumes measured in one `measure_volumes` pass.
- Vibrato (`vgm_compare`, 15 rips): 7 rate mismatches left — swings under the MOD's smallest `4xy`
  (0.7 periods: PSG and FMS 2, 5-8 cents), slow sweeps the estimator reads as beats (played as slides),
  rows whose effect slot a `Cxx` / `Axy` takes at speed 2 (`$8C`).

## Known limits

- **Data quirk:** `$89` Stealthy Steps' noise track has an `$F6` at `$7E196` closing a loop never
  opened.  The walk unrolls a loop from its `$F6`, so it plays as written; its PSG3 (5,173 frames)
  drifts against the 5,120-frame loop.
- **Driver bug, not simulated:** the drum gate fires on a rest too and saves `$85` over the saved
  sample, silencing hits after it until the next `$F0`.  The rips miss those hits (`$85` 32, `$89` 64,
  `$8B` 23); the MOD plays them.
- **Levels:** FM3's one-frame G2 hits (gate 6 of 7) play −27 … −30 dB off; noise samples played with
  another envelope.
- **LFO:** a jump centres B4's RAM copy (pan, LFO) for the next voice set; not modelled (every song
  sets both after its loop target).  A hardware LFO is baked into its copy's sample, so its rate
  follows the note across a window and restarts with each re-struck tie (`legato: retrigger`: `$8B`
  FM4).
- Not converted: the SFX, the voice clips.  No config has an Amiga merged build.
