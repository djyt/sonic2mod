# SFX → WAV rendering (`sonic2wav`)

Renders the 49 Sonic 1 sound effects in `sonic_1/sfx/*.asm` to 16-bit stereo WAV.

Unlike the MOD path, which flattens events onto a tracker row grid, this runs a **tick-accurate
software reimplementation of the Sonic 1 sound driver** against the cycle-accurate YM2612
(Nuked-OPN2) and SN76489 emulators. SFX are 60 Hz tick-driven, mostly sub-second, and built almost
entirely from per-tick vibrato sweeps and volume ramps that a row grid destroys.

```bash
python sonic2wav.py --all                       # all 49 → output/sfx/
python sonic2wav.py "sonic_1/sfx/SndB5 - Ring.asm"
python sonic2wav.py --all --dry-run             # parse + render, report, write nothing
python sfx/validate.py                          # self-check
```

## Module map

| Module | Role |
|---|---|
| `sfx/tables.py` | Driver frequency tables, PSG envelopes, register/channel maps. Self-checking at import |
| `sfx/track.py` | `SfxTrack` — mirrors the `SMPS_Track` RAM struct field-for-field |
| `sfx/chips.py` | Register writes mirroring `SetVoice`, `SendVoiceTL`, `FMUpdateFreq`, `PSGUpdateFreq` … |
| `sfx/driver.py` | `SfxDriver` — the per-tick state machine |
| `sfx/render.py` | Frame loop, FM+PSG mixing at 53267 Hz, tail detection |
| `sfx/resample.py` | Polyphase windowed-sinc 53267 → 44100 |
| `sfx/wav.py` | 16-bit stereo WAV writer (stdlib `wave`) |
| `sfx/amiga.py` | 8-bit Paula export — period grid, DC blocker, FFT rate selection, dither |
| `sfx/batch.py` | Discovery, naming, global normalisation, 8-bit export |

## Things that are easy to get wrong

**Use the driver's frequency tables, not equal temperament.** `s1.sounddriver.asm:1820` and `:2083`
generate the real tables. The stored FM word *is* `block<<11 | fnum`, i.e. the `$A4`/`$A0` register
pair layout — write `word >> 8` then `word & 0xFF`, no fnum/block search. The PSG octave rows are
**not** exact doublings (row 3 starts at 522.71, not 130.98×4) and the last row has 10 entries, not
12, so they must be transcribed rather than generated.

**FM and PSG derive their note index differently.** FM subtracts `$80` (so `$80` becomes index 0,
the rest sentinel, and the table starts on B to compensate); PSG subtracts `$81`. Both then
`add.b Transpose` and `andi.w #$7F` — transposition **wraps modulo 128, it does not clamp**. SndBC
Teleport depends on this: its "invalid" `$90` transpose lands on the same index as the fixed `$10`.

**SFX run one tick per V-int, unconditionally.** `TempoWait` only walks the *music* track RAM, so
the music `fps × (modifier−1)/modifier` correction must not be applied. All 49 SFX use
`smpsHeaderTempoSFX $01`, and the divider is a duration multiplier, so 1 tick = 1/60 s (NTSC).

**Modulation's step count is halved on arm but not on reversal.** `smpsModSet` and every note re-arm
do `lsr.b #1` on the step byte, but the reload at a direction change uses the **raw** value. So the
first half-swing is half length and every later swing is full length. Getting this wrong detunes
every vibrato SFX — and 27 of them use `smpsModSet`.

**The frequency register is only rewritten when modulation actually steps.** The driver expresses
this with an `addq.w #4,sp` stack trick that skips the caller's write; here `_do_modulation()`
returns a bool.

**A standalone duration byte does not recompute the frequency.** It takes the `.gotduration` path,
which skips `FMSetFreq`/`PSGSetFreq` entirely, so the channel re-keys at whatever frequency it
already holds. That differs from re-deriving it whenever a `smpsChangeTransposition` landed in
between — SndA8 SS Goal does exactly this. The parser marks these with `SmpsNote.is_retrigger`.

**`smpsNoAttack` suppresses the key-off, not the key-on**, and skips the whole `FinishTrackUpdate`
re-arm, which is what lets a tied note carry its PSG envelope and modulation sweep forward. A
YM2612 key-on to already-on slots is a no-op, so the key-on still fires.

**PSG envelopes hold their last value forever.** The `$80` terminator makes `VolEnvHold` rewind the
index by one and write *no* volume that tick. `sn76489/renderer.py::_render_with_envelope` instead
ramps to silence — correct for a one-shot MOD sample, wrong here.

**`SetVoice` and `SendVoiceTL` differ.** `SetVoice` writes all four TLs and lets `TL + Volume` wrap
modulo 256; `SendVoiceTL` writes only carriers, aborts entirely on negative volume, and skips
individual writes that carry past `$FF`. `ym2612/voice.py::program_voice` does neither (it clamps at
127 and hardcodes `$B4` to centre), which is why `sfx/chips.py` mirrors the driver instead.

**`OPN2.write_reg` consumes two samples of chip time per call** and normally discards that audio.
On a continuous timeline that desynchronises the FM against the PSG. `OPN2.begin_capture()` retains
those samples so the frame loop can prepend them; it is off by default, so the MOD path is
unaffected.

**Both chips must share one timeline.** The SN76489's output rate is fixed at `SN76489_Init`, so it
is constructed at the OPN2 native rate (53267 Hz). The mix is resampled to 44100 once at the end.

## Mix levels

Measured full-scale output of one channel at maximum volume: **YM2612 ±789** (identical for
1-carrier and 4-carrier algorithms — the per-channel ladder clamps), **SN76489 tone ±4096**, **noise
±2048**. Each chip is divided by its own full scale before mixing, so `--psg-gain 1.0` means one
full-scale PSG channel matches one full-scale FM channel. Only eight SFX mix both chips, and global
normalisation means only the *ratio* matters.

Gain is a single **global** scale factor across all 49 files, not per-file peak normalisation — that
preserves the composed relative loudness (the ring collect is meant to sit well below the death
jingle).

## 8-bit Amiga export (`--8bit`)

```bash
python sonic2wav.py --all --8bit                      # -> output/sfx8/ + manifest.yaml
python sonic2wav.py --all --8bit --max-rate 16574     # A500 target, roughly half the size
python sonic2wav.py --all --8bit --flat-rate 8287     # one rate for everything
```

Writes headerless **signed 8-bit mono** `.raw` files plus a `manifest.yaml` carrying what a raw
file cannot: per-sample rate, the note to trigger it at, the suggested MOD volume, and the repeat
points.

Eight bits wants almost the opposite treatment to the 16-bit WAV path:

**Per-sample peak normalisation, level restored by the MOD volume column.** Paula applies channel
volume after the sample DAC, so quantisation noise scales down with the signal — storing a quiet
effect quietly just discards bits. Measured across this set that is a median **1.5 bits**, and
**3.1 bits** for `D0_Waterfall` at −18.6 dBFS.

**DC removal first.** The PSG-noise effects carry real offset — `B8` at 24.4% of its own peak,
`C8_Burning` 20.5%, `B6_Spikes_Move` 12.9%, median 1.8% — which wastes headroom and clicks on
trigger. A one-pole 30 Hz blocker is used rather than mean subtraction, because many SFX open with
a rest and mean subtraction would push that leading silence off zero. After processing: median
0.01% of peak, worst 0.26%.

**Volumes are measured after DC removal and resampling**, so the balance is right for the audio
that actually plays. Two consequences: high-DC effects drop (their offset was inflating the peak),
and because band-limiting costs broadband effects more than narrow ones — and the loudest effect
here is broadband — every other volume rises slightly relative to it. Both are correct, not drift.

**Shaped TPDF dither.** These effects are full of long `smpsAlterVol` decay ramps, which is exactly
where flat truncation turns granular. First-order shaping `(1 − z⁻¹)` is the default; `--shape 2`
concentrates the noise harder at the top of the band, which helps at 28 kHz but is counterproductive
at 8 kHz where the whole band is audible. The RNG is seeded, so runs are byte-identical.

**One resampling stage**, chip rate straight to target — not via 44.1 kHz.

### Rate selection

Rates come from ProTracker's own period table (`856 / 2^(n/12)`, C-1 to B-3), so every chosen rate
corresponds to a real note and the sample plays at true pitch with finetune 0. Selection picks the
lowest rate whose Nyquist still contains 99% of the effect's energy, via a small pure-Python FFT.

**This saves less than you would expect.** Paula tops out near period 124 (~28.6 kHz, ~14.3 kHz
Nyquist) and 34 of the 49 effects are broadband enough to want the ceiling anyway — they are square
waves, LFSR noise and high-index FM. Auto selection lands at ~1140 KB against ~1400 KB flat at the
ceiling. The saving is concentrated in about ten effects:

```
4144 Hz   A3_Death, AF_Shield, B2_Drown_Death     (f95 as low as 323 Hz — 7x smaller)
8287 Hz   BA_SS_Glass
11084 Hz  B4_Bumper
13964 Hz  A9_SS_Item, B3_Flamethrower, BF_Get_Continue, C2_Drown_Warning, CC_Spring
```

Since band-limiting loss is unavoidable for the rest, filter quality matters more than rate — hence
the sharp polyphase sinc rather than anything cheaper.

**Target machine matters more than the material.** An A500's fixed ~4.4 kHz RC filter makes rates
above ~11 kHz largely academic (`--max-rate 16574` halves the size for that target). An A1200
(~28 kHz filter) hears the difference, but also hears Paula's zero-order-hold imaging, so low rates
sound *grittier* there than on an A500.

### MOD mechanics handled for you

Sample lengths are padded even (MOD stores word counts) and a trailing silent word is appended, with
`repeat_offset`/`repeat_length` in the manifest pointing at it — Paula loops the repeat region
forever once a sample ends, and pointing it at silence is what stops a non-looping sample buzzing.

Samples are mono; the hard-panned ring pair is a channel-assignment decision, not a sample one — put
`B5_Ring` on a right channel and `CE_Ring_Left_Speaker` on a left one.

Note a MOD holds only 31 instruments, so all 49 effects will not fit one module regardless of size.

### What was not worth doing

Aggressive tail trimming. Zero percent of samples sit below −40 dB of their own peak: every SFX
voice uses release rate `$0F`, so decays are near-instant and the 16-bit path's silence trim has
already taken everything there is.

## Known deviations from stock hardware

1. **The `SendVoiceTL` vanilla bug is not reproduced.** Stock Sonic 1 reads `VoicePtr(a6)` instead
   of `(a5)`, so every `smpsAlterVol` inside an SFX uploads TLs through a garbage pointer into
   driver RAM (`:2429`). 15 of the 49 SFX use `smpsAlterVol`. This is not reproducible offline;
   sonic2wav implements the `FixBugs` behaviour, so those volume ramps are *cleaner* than hardware.
2. **PSG frequency table overrun is extrapolated.** SndB6 Spikes Move (`nG6` → index 79) and SndA2
   (`nCs6` → index 73, an unused SFX) index past the 70-entry table; hardware reads adjacent ROM
   opcodes as pitch data. We continue the twelve-tone sequence from the last musical entry instead.
   `--strict` clamps to the top of the table. Index 69 (`nMaxPSG`) is left at its authentic
   degenerate `N = 1`.
3. **FM frequency table overrun caps the octave.** SndA8 SS Goal reaches index 98 of 96 via 38
   cumulative `smpsAlterPitch` steps. Since the FM block field saturates at 7 anyway, whole octaves
   are dropped until the note is representable, preserving the pitch class.
4. **PSG3 noise is silenced at `smpsStop`** (the `FixBugs` behaviour). Vanilla leaves it ringing,
   which would poison the rest of the render.
5. **`configs/settings.yaml`'s `fTone_07` is wrong** — the driver's `PSG7` has six leading zeros
   (27 values), that file has five (26). `sfx/tables.py` transcribes from the driver. The music MOD
   path still uses the settings.yaml copy; fixing it there would change MOD output for any song
   using PSG7, so it has been left alone deliberately.
