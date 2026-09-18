# Soundtrack survey: pitch and levels for all 18 configs (2026-09-18)

Every config was converted and checked against its recording in `reference/vgz/` — first
symbolically (`tools/vgm_pitch_audit.py`: is every note right?), then rendered
(`tools/vgm_compare.py`: per-instrument levels).  This is a survey, not 18 full audits: it fixed
what the tools could establish unambiguously and lists what needs a closer look per song.
Title Screen and Green Hill Zone have their own audits (`01_…`, `02_…`).

Reproduce for one song (`NN` = config number = recording number; `13 - Staff Roll` has no config):

```bash
python convert.py configs/NN_name.yaml
python tools/vgm_pitch_audit.py configs/NN_name.yaml "reference/vgz/NN - Name.vgz"
python tools/vgm_compare.py    configs/NN_name.yaml "reference/vgz/NN - Name.vgz" --max-rows 0
```

---

## 1. Pitch: samples synthesised in the wrong octave

An instrument whose notes are **all** out by the same interval is not a note problem — its sample
is synthesised at the wrong pitch, i.e. its `synth_root` is off by exactly that interval.  The
pitch audit now reports this per instrument.  Ten instruments in seven configs:

| Config | Instrument | `synth_root` | Error on every note |
|--------|-----------|--------------|---------------------|
| Marble Zone | 8, 9 (PSG `fTone_08` lo/hi) | F5 → F4, C7 → C6 | +1 octave (294 notes) |
| Spring Yard Zone | 11 (PSG `fTone_06` hi) | C5 → C4 | +1 octave |
| Star Light Zone | 10 (PSG `fTone_05`) | E6 → E4 | **+2 octaves** (312 notes) |
| Scrap Brain Zone | 10, 14 / 12 | G3 → G2, E6 → E5 / G4 → G5 | +1 octave / −1 octave |
| Special Stage | 6 (PSG) | C8 → A7 | +3 semitones (235 notes) |
| Robotnik | 8 (PSG) | Fs6 → Fs5 | +1 octave |
| Ending Theme | 9 (PSG) | E5 → E4 | +1 octave |

Confirmed in the rendered audio before changing anything: the MOD/VGM ratio of the strongest
partial on long PSG1 notes was 1.99 in Marble Zone, 3.99 in Star Light Zone and 1.00 in GHZ.
Each changed line carries `# pitch audit: was …`.

Wrong notes (chip segments ≥ 60 ms more than 35 cents off), whole soundtrack: **1623 → 395**.

| Config | Before | After | What is left |
|--------|--------|-------|--------------|
| Marble Zone | 294 | 0 | |
| Robotnik | 115 | 0 | |
| Special Stage | 241 | 6 | |
| Scrap Brain Zone | 166 | 10 | instrument 9: +100 c ×4 |
| Spring Yard Zone | 96 | 6 | instrument 10: +1200 c ×4, +300 c ×2 |
| Ending Theme | 30 | 6 | |
| Star Light Zone | 360 | 48 | instrument 4: +100/+200/+300 c ×8 each (a run transposed in steps?); 10: 14 scattered |
| Drowning | 200 | 200 | the song changes tempo (`smpsSetTempoMod`), which the converter does not track: the MOD drifts against the recording, so most "wrong" notes are the neighbouring note.  Converter work, not config |
| Invincibility | 64 | 64 | instrument 3: 44 notes right, 64 exactly +1 octave — one instrument serving two channels at different transpositions (`root` ignores `smpsChangeTransposition`); needs a `channel_instrument_map` variant or a range split |
| Stage Clear | 22 | 22 | instruments 9/10: same pattern (+1 octave on about half) |
| Continue Screen | 31 | 31 | instruments 3/5: −100/−200 c on most notes — looks like pitch slides or a detune the symbolic model does not know |
| Title, GHZ, Labyrinth, Final Zone, 1-Up, Chaos Emerald, Game Over | 0–1 | 0–1 | |

---

## 2. Levels: `sample_list` volumes set from measurement

With the baked volume modes every channel sharing an instrument shows the same level error, so
one volume per instrument fixes it.  `vgm_compare.py --write-volumes` applied the suggestions;
a second render verified them.  Converged in one pass.

| Config | Instruments | ≥ 2 dB off | Weighted RMS error dB | Drums vs rest dB |
|--------|------------:|-----------:|----------------------:|-----------------:|
| Title Screen | 7 | 0 → 0 | 0.6 → 0.3 | −0.7 → −0.2 |
| Green Hill Zone | 17 | 0 → 0 | 0.4 → 0.4 | −0.9 → −0.9 |
| Marble Zone | 10 | 9 → 0 | 9.7 → 0.3 | −11.3 → −0.2 |
| Spring Yard Zone | 12 | 8 → 0 | 3.8 → 0.3 | −2.3 → −0.1 |
| Labyrinth Zone | 10 | 7 → 0 | 3.9 → 0.2 | −4.1 → +0.1 |
| Star Light Zone | 11 | 7 → 1 | 9.2 → 0.6 | −8.8 → −0.2 |
| Scrap Brain Zone | 19 | 11 → 0 | 6.3 → 0.5 | −5.3 → −0.1 |
| Special Stage | 6 | 3 → 0 | 2.8 → 0.1 | no DAC |
| Robotnik | 8 | 4 → 0 | 4.1 → 0.2 | −3.6 → +0.1 |
| Final Zone | 8 | 1 → 0 | 2.4 → 1.0 | −1.8 → −1.9 |
| Stage Clear | 10 | 5 → 0 | 3.8 → 0.7 | −1.6 → +0.5 |
| Ending Theme | 10 | 5 → 0 | 3.5 → 0.4 | −3.4 → −0.1 |
| Invincibility | 5 | 2 → 0 | 2.8 → 0.1 | −2.6 → +0.1 |
| 1-Up | 7 | 5 → 0 | 4.2 → 0.2 | −5.5 → −0.2 |
| Chaos Emerald | 4 | 3 → 0 | 7.1 → 1.2 | no DAC |
| Drowning | 3 | 1 → 1 | 3.2 → 3.2 | not applied (tempo drift) |
| Continue Screen | 5 | 0 → 0 | 0.6 → 0.6 | −0.1 → −0.1 |
| Game Over | 5 | 3 → 0 | 2.3 → 0.5 | no DAC |

Instruments 2 dB or more off: **74 → 2**.  "Drums vs rest" is the DAC's level error against the
song's median synthesised note: in the un-audited songs everything else had been up to 11 dB too
loud for the drums (the DAC samples sit at 64 and cannot be turned up, so the rest came down).
Each changed line carries `# VGZ: +x.x dB at N`.

The measurements agree with the volume formula fitted to the two audited songs *before* this
survey was run (`76 × carriers × 10^(−(0.75·TL + pan)/20)`, PSG base 16): it predicted Marble Zone
and Star Light Zone PSG ~12 dB hot and Star Light's FM spread over 16 dB; measured +13.5/+14.1,
+12.1/+13.3 and −10.7…+8.6.  So the formula is a sound starting point for a new config, and a
measurement is what settles it.

How the suggestions are made (so they can be trusted or overridden):

- only notes without a `Cxx` say what an instrument's own volume should be;
- errors are relative to the song's median note; the DAC becomes the anchor only when it is 2 dB
  or more *quieter* than that (then the rest must come down to it) — a DAC that is too loud is
  itself turned down, and a smaller gap is measurement noise;
- a volume that would exceed 64 by less than 2 dB is clamped; by more, all suggestions are scaled
  down together (Star Light −3.0 dB, Chaos Emerald −3.0 dB, Stage Clear −2.1 dB);
- no suggestion when channels sharing the instrument disagree by more than 3 dB, the error is
  beyond 18 dB, or fewer than 4 notes were measured;
- alignment comes from note starts in the register log — the envelope cross-correlation was 0.9 s
  out on Chaos Emerald and 1.2 s on Drowning.

---

## 3. Still open per song

- **Drowning** — needs mid-song `smpsSetTempoMod` in the converter before it can be measured at all.
- **Invincibility, Stage Clear** — one instrument shared across transpositions (above).
- **Continue Screen, Star Light Zone** — the scattered pitch errors above.
- ~~Six configs (eight entries) warn about `synth_root: A8` on rate-3 noise~~ — done the same day (todo item 6): the
  divider is now derived from the song, the hi-hats' 4–8 kHz band went from about −11 dB to −5.2…−5.3 dB
  (hardware −4.6…−5.7 dB), and the volumes were re-measured afterwards (two 1-step changes).
- **Marble Zone noise** (todo item 12): resolved by measurement — instrument 10 is now 13; the
  orphaned line for instrument 11 can go.
- Not looked at in this survey: note fills, vibrato, grace notes, DAC rates, per-song timing.

## Credits (added 2026-09-18)

The medley had no config when the survey ran.  `configs/13_credits.yaml` (generated by
`tools/make_credits_config.py`) audits at **1623 of 1635 notes** against `13 - Staff Roll.vgz`,
with 0 unmatched key-ons on every FM and PSG channel and every channel within ±1 dB of the
recording except NOISE (−1.8 dB).  It needed `range_space: chip`, `smpsSetTempoDiv` re-timing,
permanent PSG noise mode and 31 FM voices folded into 25 instruments — see
`docs/todo/user_improvements.md` item 13 and `docs/pipeline.md` § `range_space: chip`.  Left:
FM4's `smpsAlterNote` detune scoops (7 notes) and PSG1's notes transposed below the PSG table
(5 notes; the driver reads code bytes there).
