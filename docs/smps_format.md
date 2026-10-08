# SMPS Assembly Format

The Sonic 1 disassembly's SMPS2ASM songs and SFX as `SmpsParser` (`core/smps/parser.py`) reads
them.  The parser turns the lines into ops (`SmpsCode`); a ROM's bytecode (`core/rom/`) becomes the
same ops, and one walk turns either into a song (`core/smps/code.py`).  What each byte does when
the driver plays it: `docs/smps_driver.md`.

## Song structure

1. A **header**: voice pointer, channel counts, tempo, one line per track.
2. **Tracks**: labels followed by `dc.b` lines and effect macros.
3. **Voices**: `smpsVc*` blocks after the voice label.

`if FixMusicAndSFXDataBugs` blocks hold the disassembly's data fixes.  The parser takes the fixed
branch by default; `SmpsParser(fix_data_bugs=False)` reads the game as shipped, as the VGZ rips
play it.  Comments (`;`) are stripped.

## Header macros

| Macro | Operands |
|-------|----------|
| `smpsHeaderStartSong` | source driver (1 = Sonic 1)[, the file's SMPS2ASM version] — see § Voices |
| `smpsHeaderVoice` | the voice label |
| `smpsHeaderChan` | FM tracks **including the DAC**, PSG tracks: `$06, $03` = DAC + FM1–FM5, PSG1–3 |
| `smpsHeaderTempo` | divider, modifier (`docs/smps_driver.md` § Timing System) |
| `smpsHeaderDAC` | the DAC track's label |
| `smpsHeaderFM` | label, pitch (signed semitones: the track's starting transposition, `$F4` = −12), volume (attenuation on the carriers) |
| `smpsHeaderPSG` | label, pitch, volume, a byte the driver ignores, starting envelope (`fTone_05`) |
| `smpsHeaderTempoSFX` | divider (SFX have no modifier) |
| `smpsHeaderChanSFX` | track count |
| `smpsHeaderSFXChannel` | channel (`cFM5`, `cPSG3` …), label, pitch, volume |

The three SFX macros set `SmpsSongHeader.is_sfx` and each track's `hw_channel`.

## Track data (`dc.b`)

| Token | Byte | Meaning |
|-------|------|---------|
| `$01`–`$7F` | $01–$7F | Duration in ticks |
| `nRst` | $80 | Rest |
| `nC0`–`nAs7` | $81–$DF | Notes, 12 an octave; `nMaxPSG` = `nA5` ($C6) |
| `dKick`, `dSnare` … | $81–$8B | DAC samples, on the DAC track |
| `smpsNoAttack` | $E7 | The one flag written inside `dc.b` (an `EQU`) |

Enharmonic names alias: `nDb` = `nCs`, `nEb` = `nDs`, `nGb` = `nFs`, `nAb` = `nGs`, `nBb` = `nAs`,
`nFb` = `nE`, `nEs` = `nF`, `nCb` = the octave below's `nB`, `nBs` = the next `nC`.

### Durations

A duration byte after a note is that note's.  A note without one plays the last duration read:

```asm
dc.b  nC3, $0C, nD3, nE3    ; three notes of $0C
```

The note waits for its duration across lines.  Anything else that comes next — a flag, `smpsCall`,
`smpsReturn`, a loop — completes it with the saved duration, and applies from the next note
(`FMDoNext` puts the non-duration byte back).  A label emits no byte, so a label between a note and
its duration byte leaves the duration the note's (SndA3 Death: `nAb3` / label / `dc.b $01`).

### A duration with no note

It re-keys the channel (`docs/smps_driver.md` § Note reads), so the parser makes it a note:
`SmpsNote(note_value=<last note>, is_retrigger=True)`.  After `smpsNoAttack` it is the last note
held instead (`is_rest=True, is_no_attack=True`).  On the DAC track it re-hits the last sample, and
after a rest it is a rest.

On FM and PSG tracks the parser re-keys the last note even after a rest, where the driver keeps
resting (Credits PSG3: 32 loops of hi-hats the game does not play).

### smpsNoAttack

It marks the next read: a note becomes `is_no_attack`, a duration a held note (above).  Every read
clears it, a held duration too.

## Walking a track

A label emits no byte, so a track runs on through the next label.  It ends at `smpsStop`,
`smpsFade` or `smpsStopSpecial` (both end the track in the driver), or at an `smpsJump` back into
code it has walked: its loop, which starts where **this** track's walk first reached the target
(`loop_tick`, `loop_event_index`).

- **Fall-through.**  Title Screen and Invincibility FM5 are only `smpsAlterNote $03` above FM1's
  label: a detuned double of FM1.  Star Light FM3 and FM4 run on into PSG1's and PSG2's code.
- **`smpsJump` forward** into code not walked yet is followed (Labyrinth FM4 into FM3's).
- **`smpsLoop slot, count, label`** is unrolled: the body (label to loop) is replayed `count − 1`
  more times.
- **`smpsCall label`** is inlined up to its `smpsReturn`.
- A track that is only `smpsStop` (Title Screen PSG1, PSG2) has no events.

## Effect macros

What each does: `docs/smps_driver.md` § Coordination flags.  What the parser keeps:

| Macro (alias) | Bytes | Parser |
|---------------|-------|--------|
| `smpsSetvoice` (`smpsFMvoice`) | $EF xx | `SET_VOICE` |
| `smpsAlterVol` | $E6 xx | `ALTER_VOL`, signed |
| `smpsPSGAlterVol` | $EC xx | `ALTER_VOL` too |
| `smpsAlterNote` (`smpsDetune`) | $E1 xx | `DETUNE`, signed |
| `smpsAlterPitch` (`smpsChangeTransposition`) | $E9 xx | `CHANGE_TRANSPOSITION`, signed |
| `smpsPan direction, amsfms` | $E0 xx | `PAN`, the $B4 byte (`panLeft` … `panCentre` + AMS/FMS) |
| `smpsModSet wait, speed, change, step` | $F0 w s c n | `MOD_SET` |
| `smpsModOn` / `smpsModOff` | $F1 / $F4 | `MOD_ON` / `MOD_OFF` |
| `smpsNoteFill xx` | $E8 xx | `NOTE_FILL` (frames) |
| `smpsChanTempoDiv xx` | $E5 xx | `CHAN_TEMPO_DIV`; the durations after it are multiplied |
| `smpsSetTempoMod xx` | $EA xx | `SET_TEMPO_MOD` |
| `smpsSetTempoDiv xx` | $EB xx | `SET_TEMPO_DIV` |
| `smpsPSGform xx` | $F3 xx | `PSG_FORM` |
| `smpsPSGvoice fTone_xx` | $F5 xx | `PSG_VOICE`, by name |
| `smpsNop xx` | $E2 xx | `NOP` |
| `smpsStop` / `smpsFade` / `smpsStopSpecial` | $F2 / $E4 / $EE | end the track |
| `smpsJump` / `smpsLoop` / `smpsCall` / `smpsReturn` | $F6 / $F7 / $F8 / $E3 | § Walking a track |
| `smpsClearPush`, `smpsWeirdD1LRR` (`smpsMaxRelRate`) | $ED / $F9 | not kept |

## Voices

```asm
smpsVcAlgorithm     $02
smpsVcFeedback      $07
smpsVcUnusedBits    $00
smpsVcDetune        $00, $05, $00, $05      ; operands: SMPS operators 1, 2, 3, 4
smpsVcCoarseFreq    $02, $01, $08, $01
smpsVcRateScale     $00, $00, $00, $00
smpsVcAttackRate    $10, $1E, $1E, $1E
smpsVcAmpMod        $00, $00, $00, $00
smpsVcDecayRate1    $0F, $1F, $1F, $1F
smpsVcDecayRate2    $02, $00, $00, $00
smpsVcDecayLevel    $01, $00, $00, $00
smpsVcReleaseRate   $0F, $0F, $0F, $0F
smpsVcTotalLevel    $01, $22, $24, $18      ; assembles the voice's 25 bytes
```

Each `smpsVcAlgorithm` after the voice label starts the next voice (index 0, 1, …); the parser keeps
the operands as written, and `SMPS_OP_TO_REG_OFFSET` places them (byte layout and operator order:
`docs/smps_driver.md` § FM voices).  The SMPS2ASM version in `smpsHeaderStartSong` changes two
encodings: version 0 (the default) assembles `smpsVcAmpMod` into bit 5 and sets bit 7 of the
carriers' TL, version 1 (`smpsHeaderStartSong 1, 1`, six Sonic 1 files) uses bit 7 and sets no TL
bits.  Neither matters here: every Sonic 1 voice has `smpsVcAmpMod $00`, and the chip reads 7 bits
of TL.
