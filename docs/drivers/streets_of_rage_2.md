# Streets of Rage 2: Mucom-MD 0.85

Streets of Rage 2 does not use SMPS.  Its driver, Mucom-MD 0.85 (T. Maruyama, M.N.M Software), is a
68k port of MUCOM88, the PC-88's MML music compiler: its songs are MUCOM88's compiled images byte for
byte, played by an engine of its own with soft releases, ADSR envelopes on the PSG and noise on any
track.  For anyone converting its songs, or another MUCOM88 descendant.

On this page:
- [Summary](#summary)
- [Where things are](#where-things-are)
- [How it differs](#how-it-differs)
- [Track code](#track-code)
- [How it plays](#how-it-plays)
- [DAC](#dac)
- [How it converts](#how-it-converts)
- [Known limits](#known-limits)

## Summary

| | |
|---|---|
| ROM | `Streets of Rage 2 (USA).md`, `GM MK-1054 -00`, SHA-1 `8b656eec…` |
| Driver | `mucom_md`, `core/drivers/mucom/md/` |
| Songs | `$00`-`$1B` (SFX `$20`-`$56`: listed, not read) |
| Configs | `configs/streets_of_rage_2/`: 26 minimal configs, `rips.yaml` |
| Source | no disassembly: read with `tools/rom/disasm.py` and `tools/rom/flag_census.py` |

The ROM names its driver at `$40010`: `"  MUSIC DRIVER  Mucom-MD Ver0.85Programmed by T.Maruyama(M.N.M
Software)"`.

**How it fits the framework.**  MUCOM88's track language is the framework's
(`core/rom/grammar/mucom.py`), each driver a dialect of it.  Streets of Rage's driver reads the same
language on SMPS's engine ([streets_of_rage.md](streets_of_rage.md)).  Mucom-MD fits the SMPS framework
through the `SmpsVariant` hooks — `grammar`, `music_header_reader`, `voice_layout`, `rules_from_rom`,
`dac` — under SMPS's names.

## Where things are

Each found by the code that reads it (`mucom/md/locate.py`: a `lea (pc)` and the instructions after it).

| What | ROM | Note |
|------|-----|------|
| Entry points | `$40000` | music update, SFX update (V-int), a command (`$40008`, d0 word), init |
| Sound index | `$429CE` | longs relative to the table: `$00`-`$1B` music, `$20`-`$56` SFX |
| Track RAM | `$FFA014` | `$A0` bytes each: FM1-3, PSG1-3, FM4-5, the drum track (FM6) |
| FM update | `$40E46` | flags `$41B74`; the `$FF` prefix's `$41BA8` |
| PSG update | `$42030` | flags `$423E8`; envelope `$422F8` |
| Drum update | `$4262E` | flags `$4285A`; trigger `$427B2` |
| FM volume | `$4291A` | 20 carrier TLs by step |
| FM octave | `$4293E` | Streets of Rage's words, byte-equal |
| PSG octave | `$42956` | dividers × 8 |
| PSG presets | `$4296E` | 16 ADSR envelopes × 6 bytes (AL AR DR SL SR RR) |
| Z80 player | `$559C2` | 8 KB copied raw to Z80 RAM (`$40DA2`) |

```bash
python tools/rom/disasm.py "input/roms/Streets of Rage 2 (USA).md" 40E46                                  # the FM update
python tools/rom/disasm.py "input/roms/Streets of Rage 2 (USA).md" --table 41B74 --entries 16 --first F0   # its flags
python tools/rom/flag_census.py "input/roms/Streets of Rage 2 (USA).md" --flag FF FA --kind PSG
```

**Song header** (MUCOM88's image, `mucom/md/header.py`):

```
+0  $C200.w end.w     the PC-88's load address, the image's end (little-endian)
+4  $00
+5  voices.w
+7  ?.w               not read
+9  $00               the base every offset counts from
+10 11 channels A-K:  start.w loop.w

A B C  FM1-FM3     D E F  PSG1-PSG3     G  rhythm (empty)
H I    FM4 FM5     J      FM6 (empty)   K  the drum track (FM6 as DAC)
```

No tempo (a tick is a V-int), no track volume or transposition.  A track's `$00` jumps to its header
loop, or stops.

## How it differs

| | Mucom-MD | Streets of Rage | Sonic 1 |
|---|---|---|---|
| Engine, container | its own; MUCOM88 images | SMPS 68k; SMPS-like header with volumes | SMPS 68k |
| Track bytes | MUCOM's (`d n`) | same | SMPS's |
| Tempo | a tick a frame | same | TempoWait |
| `$F1` volume | step v − 2, clamped 0-19 | step v | (`$E6` adds) |
| Pan `$F8` | 1 R, 2 L, 3 C | 1 L, 2 R | `$E0` |
| `$FF` | a prefix (`$FF $F3`, `$FF $F5`) | jump | — |
| Key-off | gate (FM floor 1 frame), or a soft release | gate | note fill |
| Vibrato | first leg half the count, no turn pause | count + 1 moves, no turn pause | a pause at each turn |
| PSG | its own octave table, `>> octave`, ADSR envelopes, noise on any track at a fixed rate | Sonic 1's table, step envelopes, noise on tone 3 | step envelopes, noise on PSG3 |
| Drum track rests | play out | cut | play out |

The same as Streets of Rage: notes, rests, ties, loops, voices, FM octave, special mode, LFO, and the
bytes of most flags.

## Track code

`mucom/md/grammar.py`: `d n` a note (`d` frames, `n` octave << 4 | semitone), `$80|d` a rest, `$00` the
end, `$F0`-`$FF` flags, one table per kind of track.

| Byte | FM | PSG | Drum track |
|---|---|---|---|
| `$F0 n` | voice n | ADSR preset n | sample `$80 + n` |
| `$F1 v` / `$FB n` | volume step v − 2 / += n, clamped 0-19 | volume / += n, 0-15 | skipped |
| `$F2 lo hi m` | detune word (m 0 sets, else adds) | same, `>> octave` as it plays | skipped |
| `$F3 q` | gate: key-off q frames before the end | same | skipped (the sample plays on) |
| `$F4` | vibrato (set, off, on) | same, depth × 266/256 | ignored |
| `$F5` `$F6` `$FE` | loop start, end, exit on the last pass | same | same |
| `$F7` | FM3 special mode offsets | n: take the noise channel; 8: give it back | skipped |
| `$F8 n` | pan (1 R, 2 L, 3 C) | noise rate (n 0-11, 12-22, more: 0, 1, 2) | pan |
| `$F9` | skipped (1 operand) | same | same |
| `$FA` | a YM write, queued (up to 8) | six bytes: an ADSR envelope | a YM write |
| `$FC` | LFO | refused | refused |
| `$FD` | tie | same | same |
| `$FF $F3 n` / `$FF $F5 x` | soft release at level n / on or off | same | skipped |

## How it plays

`mucom/md/variant.py`: Sonic 1's `TrackRules` with what differs replaced.

**Volume** (`$41C02`).  The step kept is v − 2, at most 19 (`volume.step_range`); TL = table[step] on
the carriers.  A volume write (`$41A9A`) rewrites all four TLs, a modulator's from the voice, so a
`$FA` TL write holds to the next one (`volume.tl_writes_hold`).  Every track starts at step 8: a PSG
track at attenuation 7, an FM track at TL 0 until its first `$F1` (every song sets one before a note).

**Gate.**  FM: a note no longer than its gate keys off after one frame (`key_off.gate_floor`,
`$40F72`); PSG: it plays whole.  The gate spares a note the next byte ties.

**Soft release** (18 songs; `SoftRelease`, `key_off.soft_release`).  Nothing keys off; at the gate the
FM note drops to TL = table[(step + n) >> 1] and rings through the rests; the next note keys off, then
on.  PSG: att = ((v − n) >> 1) ^ 15.
- `$FF $F5 0` ends it: a note still ringing is keyed on alone (the next slides on).
- A voice set keys a ringing note off (`$410F4`); a PSG envelope's note clears it (`$420FE`).

**Ties.**  A `$FD` read after a flag sets the tie bit after the note's end keyed it off: the next note
attacks (`ties.late_keys_off`, `$41C9C`).  The loop jump and `$FE` keep the tie bit
(`ties.exit_clears` False).

**Vibrato.**  The counter starts at count >> 1, the turn moving too (`modulation.turn_pause` False).

**PSG pitch.**  Divider = word >> octave, clamped 1-1023 (`pitch.divider_clamp`); (detune + vibrato) >>
octave (`pitch.octave_shift_from`: a constant interval).  The playable run is A2-B8; octave 1's C-G#
clamp to 1023, read as A2 (12 cents sharp; one note, `$10`).

**PSG envelopes** (`core/smps/adsr.py`).  Level 0-255 from AL, + AR to 255, − DR to SL, − SR to 0 (SR 0
holds), at the gate − RR a frame; att = ((level × (v + 1)) >> 8) ^ 15.  Each (envelope, volume) pair is
one instrument (`adsr_ffffffbe000a_v12`); the presets used are 0 (flat) to 4.

**PSG noise.**  `$F7 n` moves the track onto the noise channel (`$E4`: white, a fixed rate), its tone
left unwritten; `$F7 8` gives it back (`PsgForm(None)`).  Fixed-rate noise plays on a `NOISE` channel
of its own.

**Drum track.**  A note plays sample `$80 + n`; a tie asks for none (`ties.drum_restarts` False,
`$4277A`); rests and gates do not cut.

## DAC

`mucom/md/dac.py`: Streets of Rage's player, rewritten.

- Table at Z80 `$01BF` (start, size, pitch); the run-DPCM format (`core/rom/pcm/sor.py`).
- Samples `$81`-`$84`; songs play `$81`-`$83`.  From `$86`: voice clips, banked, not read.
- Rate: a nibble 186 / 246 cycles (high / low), a run's step 219, + 13 a pitch step; the rips play at
  that count.  The 68k holds the Z80's bus 1.5-3.1 % of the time by song: the median 1.8 % is used
  (`RUN_DPCM.stall`; pitch 4 plays at 12,894-13,040 Hz).

## How it converts

```bash
python convert.py configs/streets_of_rage_2
python tools/vgm/vgm_pitch_audit.py --all --configs configs/streets_of_rage_2 --convert
python tools/vgm/vgm_frames.py --all --configs configs/streets_of_rage_2
python tools/vgm/measure_volumes.py --configs configs/streets_of_rage_2
```

**Sample slots.**  7 songs need 33-53 instruments as derived (many voices, `$FA` copies, an envelope at
each volume) — more than a MOD's 31.  A [bank](../glossary.md#bank) (`9xx`: several sounds in one
sample, picked by offset) holds one looped sound, and these sustain or fade over seconds, so the plain
build does not bank.  Instead a minimal config gives way in order: an envelope plays at its most played
volume, then voice copies, merged windows, special-mode copies last
([../guide/song_configs.md](../guide/song_configs.md)).  All 26 fit.

**Quality.**  Every config states `window_root: highest`: each window's sample rooted as high as its
range and the 64 KB limit allow (the notes' mean rate ~12 kHz → ~21 kHz; the samples about twice the
bytes).

**Against the rips** (`rips.yaml`: 23 of the pack's 24; Walking Bottom, the beta's, matches no song;
`$04`, `$12`, `$17` have none):
- `vgm_frames`: every FM attack's level, voice and pan exact in all 23, its pitch in 21; in 19 songs
  every attack (FM, PSG, noise) exact.
- `vgm_lift`: FM onsets and lengths exact in 20 of 23; DAC onsets and lengths in all 23 (its starts
  found by the ROM's sample bytes: the rips' banks are the stream as heard, and a drum cutting into
  another shows no seek).
- `vgm_pitch_audit`: 14 of 23 clean (the rest below).
- Volumes measured in one `measure_volumes` pass: 376.

## Known limits

- **Sweeps:** modulation sweeps a MOD's vibrato cannot play (depths −150 … 598: Never Return Alive,
  Player Select).
- **PSG detune on high notes:** a divider's −1 is 37 cents there, and PSG detunes take no variant (SOR
  Super Mix, Dreamer, Back to the Industry).
- **Loop state:** state the loop's jump carries into the replay (a gate, a soft release, a detune word:
  Go Straight's FM5) is not carried.
- **Wave 131 FM3:** a late `$FD` keeps a same-pitch note's vibrato running.
- Not converted: the SFX, the voice clips.
