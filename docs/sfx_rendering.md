# SFX → WAV rendering (`sonic2wav`)

Renders the 49 Sonic 1 sound effects in `reference/smps_drivers/sonic_1/sfx/*.asm` (or a ROM's) to
16-bit stereo WAV.

Unlike the MOD path, which flattens events onto a tracker row grid, this runs a **tick-accurate
software copy of the Sonic 1 sound driver** against the YM2612 (Nuked-OPN2) and SN76489
emulators: SFX are built almost entirely from per-frame vibrato sweeps and volume ramps that a row
grid destroys.  What the driver does is `docs/smps_driver.md`; this page is how `sfx/` copies it.

```bash
python sonic2wav.py --all                       # all 49 → output/sfx/
python sonic2wav.py --rom input/roms/sonic_rev01.bin   # the same 49 read from the ROM
python sonic2wav.py "reference/smps_drivers/sonic_1/sfx/SndB5 - Ring.asm"
python sonic2wav.py --all --dry-run             # parse + render, report, write nothing
python sfx/validate.py                          # self-check
```

## Module map

| Module | Role |
|---|---|
| `sfx/tables.py` | Re-exports Sonic 1's tables (`core/drivers/reference.py`: frequency tables, PSG envelopes) and the SMPS register and channel maps (`core/smps/driver_tables.py`); they live in `core/` so the converter can reach them without importing the SFX driver |
| `sfx/track.py` | `SfxTrack` — mirrors the `SMPS_Track` RAM struct field for field |
| `sfx/chips.py` | Register writes mirroring `SetVoice`, `SendVoiceTL`, `FMUpdateFreq`, `PSGUpdateFreq` … |
| `sfx/driver.py` | `SfxDriver` — the per-frame state machine |
| `sfx/render.py` | Frame loop, FM + PSG mix at 53267 Hz, tail detection |
| `sfx/resample.py` | Re-exports `core/audio/resample.py` (polyphase windowed-sinc) |
| `sfx/wav.py` | 16-bit stereo WAV writer (stdlib `wave`) |
| `sfx/amiga.py` | 8-bit Paula export — period grid, DC blocker, FFT rate selection, dither |
| `sfx/batch.py` | Discovery, naming, global normalisation, 8-bit export |

## Things that are easy to get wrong

**The driver's tables, not equal temperament.**  The FM word is already the $A4/$A0 pair: write
`word >> 8`, then `word & 0xFF`, no fnum/block search.  Transposition wraps modulo 128 (driver
§ Note tables); SndBC Teleport's "invalid" `$90` transpose lands on the same index as the fixed
`$10`.

**One tick per V-int.**  `TempoWait` only holds the music tracks, so the music's
`fps × (m−1)/m` must not be applied.  Every SFX has `smpsHeaderTempoSFX $01`: 1 tick = 1/60 s.

**Modulation halves its step count on arm only** (driver § smpsModSet); 22 of the 49 SFX use
`smpsModSet`.  The frequency is rewritten only when modulation steps: the driver skips the
caller's write with `addq.w #4,sp`, `_do_modulation()` returns a bool.

**A duration byte with no note keeps the frequency** (driver § Note reads): SndA8 SS Goal re-keys
after each `smpsAlterPitch` at the old pitch.  The parser marks these `SmpsNote.is_retrigger`.

**`smpsNoAttack` skips the key-off and the re-arm**, so a tied note carries its PSG envelope and
modulation sweep on (driver § smpsNoAttack).

**PSG envelopes hold their last value** at `$80`.  `sn76489/renderer.py` ramps to silence unless
told to `hold` — right for a one-shot MOD sample, wrong here.

**`SetVoice` and `SendVoiceTL` differ** (driver § Carriers by algorithm).  `ym2612/voice.py::program_voice`
does `SetVoice`'s sum only and writes $B4 centred, so `sfx/chips.py` mirrors both routines instead.

**`OPN2.write_reg` costs two samples of chip time** and normally discards their audio; on a
continuous timeline that pulls the FM out of step with the PSG.  `OPN2.begin_capture()` keeps them
for the frame loop to prepend (off by default: the MOD path is unaffected).

**One timeline.**  The SN76489's output rate is fixed at `SN76489_Init`, so it is built at the
OPN2's native 53267 Hz and the mix is resampled once at the end.

## Mix levels

One channel at full volume measures **YM2612 ±789** (the same for 1 and 4 carriers: the channel
accumulator clamps), **SN76489 tone ±4096**, noise ±2048.  Each chip is divided by its own full
scale, so `--psg-gain 1.0` makes a full-scale PSG channel match a full-scale FM one (eight SFX mix
both chips).

Gain is one **global** factor across all 49 files (`--peak-dbfs`), not per-file normalisation: the
composed balance stays (the ring sits well below the death jingle).

## 8-bit Amiga export (`--8bit`)

```bash
python sonic2wav.py --all --8bit                      # -> output/sfx8/ + manifest.yaml
python sonic2wav.py --all --8bit --max-rate 16574     # A500 target, about half the size
python sonic2wav.py --all --8bit --flat-rate 8287     # one rate for everything
```

Headerless **signed 8-bit mono** `.raw` files and a `manifest.yaml` with what a raw file cannot
hold: rate, the note to trigger it at, the MOD volume, the repeat points.

Eight bits wants nearly the opposite of the WAV path:

- **Per-sample peak normalisation, the level restored by the MOD volume.**  Paula scales after the
  DAC, so the quantisation noise falls with the volume; storing a quiet effect quietly only
  discards bits.
- **DC removal first.**  The PSG-noise effects carry real offset (B8: a quarter of its peak), which
  wastes headroom and clicks on trigger.  A one-pole 30 Hz blocker, not mean subtraction: many SFX
  open with a rest, which a mean would push off zero.
- **Volumes measured after DC removal and resampling**, on the audio that plays.
- **Shaped TPDF dither** (`--shape 1`, first order, the default): the long `smpsAlterVol` ramps
  turn granular when truncated.  `--shape 2` helps at 28 kHz and hurts at 8 kHz, where the whole
  band is audible.  The RNG is seeded: runs are byte-identical.
- **One resampling stage**, chip rate straight to the target.

### Rate selection

Rates come from ProTracker's period table (`856 / 2^(n/12)`, C-1 to B-3), so each plays at true
pitch with finetune 0.  The lowest rate whose Nyquist holds 99 % of the effect's energy wins.  It
saves less than expected: Paula tops out near period 124 (~28.6 kHz) and two thirds of the effects —
square waves, LFSR noise, bright FM — want that ceiling.  Filter quality therefore matters more
than rate, hence the sharp polyphase sinc.

The target machine matters more than the material: an A500's fixed ~4.4 kHz filter makes rates
above ~11 kHz largely academic (`--max-rate 16574`); an A1200 hears them, and also hears Paula's
zero-order-hold imaging, so low rates sound grittier there.

### MOD mechanics

Lengths are padded even (MOD stores words) and a silent word is appended, the manifest's
`repeat_offset` / `repeat_length` pointing at it: Paula loops the repeat region once a sample ends,
and silence there stops a one-shot buzzing.  Samples are mono: the ring pair's panning is a channel
choice (`B5_Ring` right, `CE_Ring_Left_Speaker` left).  A MOD holds 31 instruments, so the 49 never
fit one module.

No tail trimming beyond the WAV path's: 44 of the 45 SFX voices release every operator at `$0F`.

## Known deviations from stock hardware

1. **The `SendVoiceTL` bug is not reproduced.**  Stock Sonic 1 reads the SFX voice pointer from
   `(a6)` instead of `(a5)`, so every `smpsAlterVol` in an SFX (12 of the 49) uploads TLs from a
   garbage pointer.  sonic2wav does the `FixBugs` version: those ramps are cleaner than hardware.
2. **PSG table overrun is extrapolated.**  SndB6 Spikes Move (`nG6`, index 79) and the unused SndA2
   (`nCs6`, index 73) read past the 70-entry table into code; the twelve-tone sequence is continued
   from the last musical entry instead.  `--strict` clamps to the table's top.  Index 69 (`nMaxPSG`)
   keeps its divider 1.
3. **FM table overrun caps the octave.**  SndA8 SS Goal reaches index 96 — one past the table —
   after 37 `smpsAlterPitch $02` steps; the block field saturates at 7 anyway, so whole octaves are
   dropped until the note fits, keeping the pitch class.
4. **PSG3's noise stops at `smpsStop`** (the `FixBugs` behaviour); stock leaves it ringing, which
   would poison the rest of the render.
