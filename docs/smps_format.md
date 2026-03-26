# SMPS Assembly Format Reference

Reference for the Sonic 1 SMPS assembly format as parsed by `smps_parser.py`.

For runtime driver behavior (what each byte does in hardware, timing system, FM operator order),
see `docs/smps_driver.md`. For the SMPS→MOD conversion pipeline, see `docs/pipeline.md`.

## Song Structure

An SMPS assembly file contains:
1. A **song header** with voice pointer, channel counts, tempo, and per-channel headers
2. **Channel data** sections (labels followed by `dc.b` note data and effect macros)
3. **Voice definitions** (`smpsVc*` macros defining FM instrument parameters)

## Header Macros

### `smpsHeaderStartSong <version>`
Declares driver version. Sonic 1 uses version `1`.

### `smpsHeaderVoice <label>`
Points to the voice (FM instrument) definition block.

### `smpsHeaderChan $<fm_count>, $<psg_count>`
Number of FM and PSG channels. The total channel count in the header includes the DAC channel separately.

Example: `smpsHeaderChan $06, $03` = 6 FM channels (including DAC as FM6), 3 PSG channels.

### `smpsHeaderTempo $<divider>, $<modifier>`
- **divider**: Clock divider (typically $01)
- **modifier**: Tempo modifier (e.g. $05)

### `smpsHeaderDAC <label>`
Declares DAC channel data location.

### `smpsHeaderFM <label>, $<pitch>, $<volume>`
Declares FM channel data location with:
- **pitch**: Signed byte pitch offset (e.g. $F4 = -12 semitones)
- **volume**: Initial volume attenuation

### `smpsHeaderPSG <label>, $<pitch>, $<volume>, $<mod>, <voice>`
Declares PSG channel with additional:
- **mod**: Frequency envelope byte (typically $00 in Sonic 1)
- **voice**: PSG tone envelope (e.g. `fTone_05`)

## Note Data (`dc.b` Lines)

Channel data is encoded in `dc.b` (define constant byte) lines with comma-separated tokens.

### Token Types

| Token | Range | Meaning |
|-------|-------|---------|
| `nRst` | $80 | Rest (silence) |
| `nC0`–`nAs7` | $81–$DF | Chromatic notes, 12 per octave |
| `nMaxPSG` | $C6 | Maximum PSG frequency (= nA5 in Sonic 1) |
| `dKick`, `dSnare`, etc. | $81–$8B | DAC drum samples |
| `$xx` (< $80) | $01–$7F | Duration in ticks |
| `smpsNoAttack` / `$E7` | $E7 | Tie — next note plays without re-attack |

### Enharmonic Note Aliases

Each octave provides aliases:
- `nDb` = `nCs`, `nEb` = `nDs`, `nFb` = `nE`
- `nF` = `nEs`, `nGb` = `nFs`, `nAb` = `nGs`
- `nBb` = `nAs`, `nCb` = previous `nB`, `nBs` = next `nC`

### Duration Persistence

The last explicitly stated duration carries forward to subsequent notes that don't specify one.

```asm
dc.b  nC3, $0C, nD3, nE3    ; C3 for $0C ticks, then D3 for $0C, then E3 for $0C
```

### Standalone Duration Bytes

A duration byte not preceded by a note **implicitly re-triggers the last note** for that duration.
It is not a wait or sustain — it is identical to writing the previous note byte again.

> *"Once either a note or a duration value is defined, you can omit repetition of those values;
> however, you must always define a note before you can define a duration for the first time."*
> — Sonic Retro SCHG: Music Hacking / Voice and Note Editing

The driver calls `PSGDoNoteOn + PSGDoVolFX` (and the FM equivalent) on **every** `DurationTimeout`
expiry regardless of whether the next data byte is a note or a duration. For PSG noise, this
restores the channel volume from `$FF` (silenced by the previous note-cut) back to audible,
producing a new hit.

```asm
smpsNoteFill  $03
dc.b  nMaxPSG, $0C    ; trigger nMaxPSG for 12 ticks (fill fires at tick 3)
smpsNoteFill  $0C
dc.b  $0C              ; re-trigger nMaxPSG for 12 ticks (fill=duration → never fires)
smpsNoteFill  $03
dc.b  $0C              ; re-trigger nMaxPSG for 12 ticks (fill fires at tick 3)
```

This is equivalent to:

```asm
smpsNoteFill  $03
dc.b  nMaxPSG, $0C
smpsNoteFill  $0C
dc.b  nMaxPSG, $0C    ; same note repeated explicitly
smpsNoteFill  $03
dc.b  nMaxPSG, $0C
```

**`smpsNoteFill` fill = duration edge case:** when fill value equals the duration (e.g. both `$0C`),
`DurationTimeout` expiry takes the `PSGDoNext` path rather than `.notegoing`, so `NoteTimeoutUpdate`
is never reached on that frame and the fill never fires. The note sustains the full duration.

## Effect Macros

### Channel Control

| Macro | Bytes | Description |
|-------|-------|-------------|
| `smpsSetvoice $xx` | $EF, xx | Set FM voice/instrument |
| `smpsAlterVol $xx` | $E6, xx | Add signed value to volume attenuation (FM channels) |
| `smpsPSGAlterVol $xx` | $EC, xx | Add signed value to volume attenuation (PSG channels) — parsed identically to `smpsAlterVol` |
| `smpsAlterNote $xx` | $E1, xx | **FNUM offset** (~10 cents/unit, NOT semitones) — sub-semitone detune only; does NOT affect voice_map range lookup or MOD pitch placement |
| `smpsChangeTransposition $xx` | $E9, xx | **Semitone shift** — add signed value to channel pitch; cumulative; affects all subsequent notes and voice_map routing |
| `smpsPan direction, amsfms` | $E0, xx | Set panning and AMS/FMS |

### Modulation

| Macro | Bytes | Description |
|-------|-------|-------------|
| `smpsModSet $wait, $speed, $change, $steps` | $F0, w, s, c, n | Set modulation parameters |
| `smpsModOn` | $F1 | Enable modulation |
| `smpsModOff` | $F4 | Disable modulation |

### Timing

| Macro | Bytes | Description |
|-------|-------|-------------|
| `smpsNoteFill $xx` | $E8, xx | Set note fill — note cuts after xx ticks |

### Flow Control

| Macro | Bytes | Description |
|-------|-------|-------------|
| `smpsStop` | $F2 | End of channel data |
| `smpsJump <label>` | $F6, addr | Jump to label (song loop point) |
| `smpsLoop $idx, $count, <label>` | $F7, idx, cnt, addr | Loop back to label, count times |
| `smpsCall <label>` | $F8, addr | Call subroutine at label |
| `smpsReturn` | $E3 | Return from smpsCall |

### PSG-Specific

| Macro | Bytes | Description |
|-------|-------|-------------|
| `smpsPSGform $xx` | $F3, xx | Set PSG waveform |
| `smpsPSGvoice <voice>` | $F5, xx | Set PSG tone envelope |

### Ignored

| Macro | Description |
|-------|-------------|
| `smpsNop $xx` | Game synchronization byte (no audio effect) |

## Voice Definitions

FM voices are defined using `smpsVc*` macros that set YM2612 register parameters:

```asm
smpsVcAlgorithm     $02        ; FM synthesis algorithm (0–7)
smpsVcFeedback      $07        ; Operator 1 feedback (0–7)
smpsVcDetune        $00, $05, $00, $05   ; Per-operator detune
smpsVcCoarseFreq    $02, $01, $08, $01   ; Per-operator frequency multiplier
smpsVcRateScale     $00, $00, $00, $00
smpsVcAttackRate    $10, $1E, $1E, $1E
smpsVcAmpMod        $00, $00, $00, $00
smpsVcDecayRate1    $0F, $1F, $1F, $1F
smpsVcDecayRate2    $02, $00, $00, $00
smpsVcDecayLevel    $01, $00, $00, $00
smpsVcReleaseRate   $0F, $0F, $0F, $0F
smpsVcTotalLevel    $01, $22, $24, $18   ; Per-operator volume
```

Four parameters per macro correspond to the four FM operators. These are parsed for reference but not directly mapped to MOD instruments (MOD uses PCM samples, not FM synthesis).

## DAC Samples (Sonic 1)

See `docs/smps_driver.md` §DAC Channel for the full table including native sample rates.

## Edge Cases

### FM5 Fall-Through
FM5 may contain only an `smpsAlterNote` (FNUM detune) or `smpsChangeTransposition` effect, then fall through into FM1's data. The parser handles this by not stopping at label boundaries — only `smpsStop`/`smpsJump` terminate channel parsing.

### Empty Channels
PSG1 and PSG2 in some songs (e.g. Title Screen) contain only `smpsStop`. The parser produces channels with zero events.

### Loop with Nested Effects
Loop bodies may contain `smpsNoteFill` and other effects interleaved with `dc.b` duration bytes. The parser processes effects and data in order, maintaining state across loop iterations.
