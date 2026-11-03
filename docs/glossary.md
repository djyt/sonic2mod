# Glossary

Terms used across these docs, in plain words.  Standard tracker and chip terms come first, then the words
this project coined (mostly config keys).  Each entry links to its home page.

- [Hardware and formats](#hardware-and-formats)
- [The sound driver](#the-sound-driver)
- [Configs and conversion](#configs-and-conversion)
- [Samples](#samples)
- [The merged (Amiga) build](#the-merged-amiga-build)
- [Checking against a rip](#checking-against-a-rip)

---

## Hardware and formats

### YM2612
The Mega Drive's FM synthesis chip: six channels, each built from four **operators** (sine oscillators that
modulate each other).  Rendered here by the Nuked-OPN2 emulator.  → [synthesis/fm.md](synthesis/fm.md)

### SN76489
The Mega Drive's PSG (programmable sound generator): three square-wave tone channels and one noise channel.
→ [synthesis/psg.md](synthesis/psg.md)

### FM / PSG / DAC
Shorthand for the three kinds of channel: YM2612 FM channels, SN76489 channels, and YM2612 channel 6 playing
8-bit samples (the **DAC**, used for drums).

### fnum / block
The YM2612's pitch registers.  `block` is the octave, `fnum` the frequency within it:
`f = fnum × (clock / 144) × 2^block / 2^21`.  A4 (440 Hz) is fnum 1083 at block 4 by the formula;
Sonic 1's table writes 1084.  → [drivers/sonic_1.md](drivers/sonic_1.md#note-tables)

### channel 3 special mode
A YM2612 mode in which channel 3's four operators each take a frequency of their own, set by an offset
from the note.  Streets of Rage's drums use it; the converter renders such a voice on channel 3.
→ [conversion/detune.md](conversion/detune.md#channel-3s-special-mode)

### hardware LFO
The YM2612's one low-frequency oscillator, shared by every channel: register `$22` sets its speed, each
channel's `$B4` how far it moves that channel's pitch (FMS) and level (AMS).
→ [conversion/detune.md](conversion/detune.md#hardware-lfo)

### TL (total level)
An operator's attenuation register, 0.75 dB a step.  Turning down the **carrier** operators' TL turns the
note down; the drivers' volume commands do this.

### MOD / ProTracker
The Amiga's tracker music format: up to 31 samples, 64-row patterns, one note and one effect per cell.
Effects are written as hex, e.g. `C20` (set volume $20).  → [reference/mod_effects.md](reference/mod_effects.md)

### Paula
The Amiga's sound chip: four channels that play 8-bit samples.  The reason the [merged build](#merged-build)
exists.

### finetune / period
A MOD sample's pitch trim (±⅛ semitone steps), and the Amiga timer value that sets a note's playback rate.

### V-int
The vertical interrupt (60 Hz NTSC, 50 Hz PAL).  Most sound drivers run once per V-int, so a **frame** is
the driver's unit of time (OutRun's runs on a timer instead: see [update clock](#update-clock)).

---

## The sound driver

### sound driver
The game's code that reads a song's bytes once a frame and writes the sound chips.  Each game's driver is
read as data.  → [drivers/README.md](drivers/README.md#what-a-sound-driver-is)

### SMPS
Sega's Sample Music Playback System: the sound driver family Sonic 1 and most of the supported games use.
Songs are bytecode: note bytes, duration bytes and **coordination flags**.  → [drivers/README.md](drivers/README.md)

### driver family
Drivers sharing a code base: `smps68k`, `smpsz80`, `mucom`, `timetrax`, a folder each under `core/drivers/`.
→ [drivers/README.md](drivers/README.md#what-every-driver-shares)

### coordination flag
An SMPS command byte ($E0–$F9 in Sonic 1) in a channel's data: set the voice, detune, loop, jump and so on.
→ [drivers/sonic_1.md](drivers/sonic_1.md)

### TempoWait
Sonic 1's tempo mechanism: every *m*-th frame no track advances (see [tempo modifier](#tempo-modifier)).
→ [drivers/sonic_1.md](drivers/sonic_1.md#tempowait)

### tick
One step of the driver's song clock.  Note durations count ticks.  A tick is a frame, except where the
[tempo modifier](#tempo-modifier) skips frames.

### tempo modifier
The SMPS header's tempo value *m*: the driver holds every *m*-th frame, so ticks run at
fps × (m − 1) / m.  → [conversion/timing.md](conversion/timing.md#bpm)

### tempo divider
A multiplier on every note duration (song header, or per channel).
→ [conversion/timing.md](conversion/timing.md#ticks-rows-patterns)

### tempo segment
A stretch of the song at one tempo modifier; a mid-song tempo change starts a new one.
→ [conversion/timing.md](conversion/timing.md#mid-song-tempo-changes-smpssettempomod-ea)

### note fill
`smpsNoteFill`: cut each note a fixed number of frames after it starts, for a staccato part.
→ [conversion/effects.md](conversion/effects.md#note-fill-smpsnotefill)

### PlaybackRules
The driver facts a song carries with it (`song.rules`): frequency tables, PSG envelopes, drum names, timing.
Code reads these, never one driver's tables directly.
→ [drivers/README.md](drivers/README.md#what-every-driver-shares)

### TrackRules
Part of PlaybackRules: how one kind of track (FM, PSG, drums) handles volume, key-off, ties, modulation
and pitch, where a driver differs from Sonic 1.
→ [drivers/README.md](drivers/README.md#what-every-driver-shares)

### song IR
`SmpsSong`, the one in-memory form every song is read into, whatever its driver or source (assembly, ROM,
rip): channels of notes, rests, ties and effects, plus the song's PlaybackRules.
→ [drivers/README.md](drivers/README.md#what-every-driver-shares)

### walk
Following a channel's events in order while keeping the driver's state (voice, volume, transposition,
detune ...), to learn what each note plays.  One walk (`walk_channel`, `resolve_note`) serves every pass.
→ [developer/architecture.md](developer/architecture.md#one-state-machine-decides-what-a-note-plays)

### update clock
What times a driver's update, and so the length of a frame: the V-int, or the YM2612's Timer A
(OutRun's, 126.22 Hz).  → [drivers/outrun.md](drivers/outrun.md#how-it-plays)

### data fix
A byte edit that corrects a known error in a game's song data (Sonic 1 has three), applied when the song
is read.  `--shipped` reads the game as sold.  → [drivers/sonic_1.md](drivers/sonic_1.md#where-things-are)

### voice copy
A copy of an FM voice with some registers changed by the song (a register write, channel 3
special-mode offsets, the LFO), rendered as an instrument of its own.
→ [drivers/streets_of_rage.md](drivers/streets_of_rage.md#how-it-plays)

### pitch envelope
A driver's per-frame pitch offsets from each note's start (a scoop, a vibrato that deepens), rendered into
the sample.  → [drivers/space_harrier_2.md](drivers/space_harrier_2.md#how-it-plays)

### run-out
A driver keying a long note off after a fixed number of frames with no new attack (Golden Axe: 256).
→ [drivers/golden_axe.md](drivers/golden_axe.md#how-it-differs-from-sonic-1)


### SmpsVariant
A driver's description the ROM readers follow (any family, not only SMPS): where its songs are, its
flag tables, header and voice layout, and its PlaybackRules (`core/rom/variant.py`).
→ [drivers/README.md](drivers/README.md#what-every-driver-shares)

### drum track
The track whose notes trigger drums: DAC samples, or FM drum programs.
→ [drivers/golden_axe.md](drivers/golden_axe.md#drums)

### gate
MUCOM's key-off a number of frames before a note's end (the opposite way round from note fill).
→ [drivers/streets_of_rage.md](drivers/streets_of_rage.md#track-code)

### soft release
Mucom-MD's key-off replaced by a drop in level, so notes ring on through rests.
→ [drivers/streets_of_rage_2.md](drivers/streets_of_rage_2.md#how-it-plays)

---

## Configs and conversion

### song config
The YAML file per song (`configs/<game>/<song>.yaml`): what to read and how channels become MOD channels and
instruments.  → [guide/song_configs.md](guide/song_configs.md)

### minimal config
A song config with no `channels:` key.  Everything else is derived from the song.
→ [guide/song_configs.md](guide/song_configs.md#a-minimal-config)

### full config
A song config that states its `channels:` and instrument maps by hand.
→ [guide/song_configs.md](guide/song_configs.md#building-up-a-full-config)

### variant
A `variants: {NAME: ...}` block in a config that overrides a few keys for an alternative build, chosen with
`--variant NAME` (e.g. `lofi`: smaller samples).  → [guide/song_configs.md](guide/song_configs.md#variants)

### reference build
The plain conversion: every chip channel on a MOD channel of its own.  The ground truth the
[merged build](#merged-build) is made from.  → [guide/amiga_builds.md](guide/amiga_builds.md#terms)

### range_space
Whether a `voice_map` entry's `low` / `high` / `root` compare with the note byte in the song data (`source`)
or with the pitch the chip actually plays after transposition and pitch offsets (`chip`).
→ [conversion/notes.md](conversion/notes.md#range_space-chip)

### root path
How a `voice_map` entry with `root` places notes: MOD note = `root + (note − low)`.
→ [guide/song_configs.md](guide/song_configs.md#instruments-how-a-note-finds-its-sample)

### transpose path
How an entry without `root` places notes: MOD note = song note + pitch offset + transposition + the
channel's `transpose`.  → [guide/song_configs.md](guide/song_configs.md#instruments-how-a-note-finds-its-sample)

### window
A span of one voice's pitches served by one sample, with its own `root`.  A derived config makes one entry
per window.  → [guide/song_configs.md](guide/song_configs.md#windows)

### row_grid
How rows are laid out: `accurate` (the finest grid the notes need) or `bar` (a composer's layout from the
song's meter: a 16th note a row, phrases a pattern).  → [conversion/timing.md](conversion/timing.md#row_grid-bar)

### baked level
An instrument's most common note level becomes its `sample_list` volume; notes at other levels get a `Cxx`.
Keeps the pattern free of volume commands where the song does not change level.
→ [conversion/levels.md](conversion/levels.md#fm-fm_volume_scaling-baked)

### detune variant
An extra copy of an instrument's sample rendered at another detune (`smpsAlterNote`) offset.  A detune is a
raw frequency offset, not a fraction of a semitone, so finetune cannot stand in for it.
→ [conversion/detune.md](conversion/detune.md)

### own detune
The detune most of an instrument's notes play at; its main sample is rendered at it.
→ [conversion/detune.md](conversion/detune.md#how-variants-are-planned)

### loop extension
Replaying each channel's loop body up to the song's last tick (to a common period when channels loop at
different lengths): SMPS channels loop on their own, a MOD has one song position for all of them.
→ [conversion/song_structure.md](conversion/song_structure.md#loop-extension)

### leading rest
Silence at a channel's start before its first note.  It gets a `C00` on row 0, so a song that loops back
there does not ring its last note through the rest.
→ [conversion/song_structure.md](conversion/song_structure.md#leading-rests)

### end trigger
An `E8x` command marking the song's end for a game engine to read (`end_trigger`).
→ [conversion/song_structure.md](conversion/song_structure.md#end-trigger-end_trigger)

### pattern break
A `mod_pattern_breaks` entry: end a pattern early (`D00`) so a phrase starts on a new pattern.
→ [conversion/song_structure.md](conversion/song_structure.md#pattern-breaks-mod_pattern_breaks)

---

## Samples

### catalogue
The list of instruments the converter synthesises, each with the config entry it is rendered for.
→ [synthesis/fm.md](synthesis/fm.md#what-is-rendered)

### synth_root
The pitch an instrument's sample is rendered at on the emulated chip.  It changes the sample's rate, never
where notes land.  Derived from the song.
→ [synthesis/fm.md](synthesis/fm.md#where-the-sample-is-rendered-synth_root)

### synth_shift
The semitones between `synth_root` and the pitch the sample's `root` note must sound, folded into the
sample's playback rate.  → [synthesis/fm.md](synthesis/fm.md#where-the-sample-is-rendered-synth_root)

### render level
The volume (TL offset and pan) most of an instrument's notes play at; the sample is rendered at it.
→ [synthesis/fm.md](synthesis/fm.md#level)

### render cache
Chip renders saved on disk, keyed on their inputs and the code, so a re-conversion skips them.
→ [synthesis/fm.md](synthesis/fm.md#threads-and-the-render-cache)

### tone-2 divider
The divider of tone channel 2 (PSG3; the chip counts from 0) that clocks the PSG's "rate 3" noise
(`tone2_n`), derived from the song.
→ [synthesis/psg.md](synthesis/psg.md#rate-3-the-tone-2-divider)

### FM drum
A drum the driver plays as a frame-by-frame FM program (Golden Axe, Space Harrier II), rendered whole.
→ [synthesis/fm.md](synthesis/fm.md#fm-drums-a-frame-by-frame-render)


### baked sample
A pitch sweep or volume envelope rendered into a sample of its own, instead of written as MOD effects
(`baked_slots`).  → [drivers/time_trax.md](drivers/time_trax.md#how-it-converts)

### dac_stall
The share of the DAC's time the driver's update holds its sample loop, lowering the playback rate.
→ [drivers/outrun.md](drivers/outrun.md#dac)

### sustain loop
A sample cut where its envelope settles, plus one seamless loop, so a long note costs no more bytes than a
short one.  → [conversion/samples.md](conversion/samples.md#sustain-loops-samplessustain_loops)

### flat point
Where a render's level stays within `loop_drift_db` of where it settles; a sustain loop may start there.
→ [conversion/samples.md](conversion/samples.md#sustain-loops-samplessustain_loops)

### release slide
Volume-slide rows (`A0y`) that fade a looped note at the voice's release rate, instead of cutting it dead.
→ [conversion/samples.md](conversion/samples.md#release-slides)

### sliding loop
`loop_decay: slide`: a fading voice is looped flat and its fade written as volume slides.
→ [conversion/samples.md](conversion/samples.md#sliding-loops-loop_decay-slide)

---

## The merged (Amiga) build

### merged build
The 4-channel Amiga build `convert.py --merged` writes by folding channels together.
→ [guide/amiga_builds.md](guide/amiga_builds.md)

### fold
Playing two or more Mega Drive channels on one MOD channel.
→ [guide/amiga_builds.md](guide/amiga_builds.md#terms)

### primary
The channel a fold lands on.  Its MOD channel, vibrato, note fill and volume carry the result.
→ [guide/amiga_builds.md](guide/amiga_builds.md#terms)

### follower
A channel folded onto a primary.  It has no MOD channel of its own; its notes sound inside the primary's.
→ [guide/amiga_builds.md](guide/amiga_builds.md#terms)

### composite
One sample holding a primary note plus the follower notes that start with it (a chord, a kick plus hi-hat).
Rendered on the chip (**chip composite**) or mixed from rendered samples (**mix**).
→ [guide/amiga_builds.md](guide/amiga_builds.md#composite-instruments)

### mix
A composite made by adding rendered samples together, rather than rendering the voices on the chip at once.
→ [guide/amiga_builds.md](guide/amiga_builds.md#mixed-composites)

### solo note
A follower note played on the primary's channel by itself, while the primary is silent.
→ [guide/amiga_builds.md](guide/amiga_builds.md#terms)

### orphan
A follower note that starts while the primary is still sounding, not with one of its notes.  Lost unless
`cut_primary` or the fill pool takes it.  → [guide/amiga_builds.md](guide/amiga_builds.md#groups)

### fill pool
Notes placed on whichever output channel is silent when they start.
→ [guide/amiga_builds.md](guide/amiga_builds.md#the-fill-pool)

### vacate
Moving a channel's notes onto other channels, where nothing is lost, to free its channel (e.g. for game
sound effects).  → [guide/amiga_builds.md](guide/amiga_builds.md#vacating-a-column)

### bank
One MOD sample slot holding several short sounds, each started part-way in with `9xx` (sample offset).
Saves slots.  → [guide/amiga_builds.md](guide/amiga_builds.md#sample-banks)

### slot
One of a MOD's 31 instrument numbers.  → [guide/amiga_builds.md](guide/amiga_builds.md#slots)

### twin
Composites of the same notes differing only in fill or level; one gives up its slot first when slots run out.
→ [guide/amiga_builds.md](guide/amiga_builds.md#slots)

### stand-in
A surviving composite of the same shape played in place of one that lost its slot.
→ [guide/amiga_builds.md](guide/amiga_builds.md#slots)

---

## Checking against a rip

### frame log
A rip decoded frame by frame: each channel's key state, frequency, levels and voice at every driver
update (`core.vgm.frame_log`).  → [guide/verifying.md](guide/verifying.md#pitch-level-voice-per-frame-vgm_frames)

### rip
A VGM/VGZ file: a recording of every register write a game made to its sound chips, frame by frame (`.vgz` is gzipped
`.vgm`).  The reference the conversion is checked against.  → [guide/verifying.md](guide/verifying.md)

### lift
Reading a rip's register log back into a song (notes, durations, ticks) to compare with the song as read
from the ROM or assembly.  → [guide/verifying.md](guide/verifying.md#does-the-song-read-right-vgm_lift)

### glitch
A V-int the ripped run lost or gained, putting every channel a frame off from there on.  Listed in
`rips.yaml` and undone by every rip tool.  → [guide/verifying.md](guide/verifying.md#the-rips-own-faults-ripsyaml)
