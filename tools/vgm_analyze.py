#!/usr/bin/env python3
"""YM2612 + SN76489 pitch/event analyzer for VGM/VGZ files.

Parses YM2612 register writes and SN76489 register writes, and outputs a
table of key-on events showing channel, frequency data, and nearest note
name.  Use this to verify synth_root values against the actual chip output
from a game recording, or to count PSG noise events.

Usage::

    python tools/vgm_analyze.py "reference/vgz/sonic_1/02 - Green Hill Zone.vgz"
    python tools/vgm_analyze.py file.vgz --chip fm --channel FM3 FM4 FM5
    python tools/vgm_analyze.py file.vgz --chip psg --channel NOISE
    python tools/vgm_analyze.py file.vgz --chip all --max-rows 0
    python tools/vgm_analyze.py file.vgz --max-rows 500 --clock 7670454
    python tools/vgm_analyze.py file.vgz --frames --chip all --channel FM1 PSG1   # frame by frame

Output columns (FM):
    time_ms   — milliseconds from track start (VGM 44100 Hz sample clock)
    chan      — FM1..FM6
    fnum      — raw frequency number written to YM2612
    blk       — block (octave shift), 0–7
    freq_hz   — computed frequency using chip formula
    note      — nearest semitone name in standard pitch (e.g. G3, C#2)
    smps      — same note in SMPS-assembly convention (FM label is one octave
                lower than chip output, so A6 chip → A5 SMPS label)

Output columns (PSG tone):
    time_ms   — milliseconds from track start (a row = the channel becoming audible, or its
                period leaving the current note by more than --psg-mod-cents; smaller moves
                are smpsModSet vibrato steps and do not start a row)
    chan      — PSG1..PSG3
    period    — 10-bit SN76489 period register value
    —         — (blk column unused, shown as —)
    freq_hz   — computed frequency: clock / (32 * period)
    note      — nearest semitone name in standard pitch
    smps      — same note in SMPS-assembly convention (PSG label is one octave
                higher than chip output, so E4 chip → E5 SMPS label)

Output columns (PSG noise):
    time_ms   — milliseconds from track start
    chan      — NOISE
    noise_reg — 3-bit noise register (bit2=type, bits[1:0]=rate)
    —         — (blk column unused)
    freq_hz   — LFSR shift rate: clock/512, /1024, /2048 for rates 0–2, or
                clock/(32·N) when rate 3 follows tone ch2 (N=0 treated as 1,
                as the Sega VDP PSG does)
    note      — decoded noise type string (e.g. white/N/512, white/tone2 N=0)
    smps      — "—" (no pitch meaning for noise)

Output columns (DAC, --chip dac / all):
    time_ms   — milliseconds from track start
    chan      — DAC
    data      — PCM bank offset the stream seeks to (0xE0 command); a seek
                marks a sample start, so the offset identifies the sample
                (kick/snare/…).  Some loggers merge back-to-back restarts of
                the same sample, so counts can be lower than the SMPS data.
    note      — "seek <offset>"
"""

from __future__ import annotations

import argparse
import contextlib
import statistics
import sys
from collections.abc import Iterator
from pathlib import Path

from core.chips import MD_FM_CLOCK, MD_PSG_CLOCK, fm_frequency_hz, psg_frequency_hz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audio import hz_to_midi, midi_name, pitch_name
from core.vgm import (
    DAC_NAME,
    DEFAULT_MOD_CENTS,
    NOISE_CHANNEL,
    PSG_NAMES,
    ChipState,
    DacFrame,
    FmFrame,
    Frame,
    NoteStart,
    PsgFrame,
    VgmError,
    VgmLog,
    frame_log,
    noise_rate,
    noise_white,
    note_starts,
    read_vgm,
)

# ---------------------------------------------------------------------------
# Note names
# ---------------------------------------------------------------------------

# The recording's chip clocks, Sonic 1 on an NTSC Mega Drive.  Override with --clock / --psg-clock.
DEFAULT_FM_CLOCK = MD_FM_CLOCK
DEFAULT_PSG_CLOCK = MD_PSG_CLOCK

_NOISE_RATES = ('N/512', 'N/1024', 'N/2048')
_NOISE_TONE2_RATE = 3
_ALL_SLOTS = 0xF


def _smps_note(freq: float, chan_type: str) -> str:
    """Return the SMPS-convention note name for a chip output frequency.

    In Sonic 1 SMPS the note byte labels relate to standard pitch like this:
      FM  — label IS the standard pitch name (driver table: nC0 -> 16.35 Hz = C0), so the
            effective note (byte + pitch_offset) is simply the chip's note
            e.g. nA4 with pitch_offset 0 → chip plays A4 = 440 Hz
      PSG — label is one octave *higher* than what the chip outputs
            e.g. nE5 in the assembly → chip plays E4 (standard)
            Conversion: add 12 to MIDI number
      NOISE — no pitch; returns "—"

    Args:
        freq:      chip output frequency in Hz (> 0)
        chan_type: "fm", "psg", or "noise"
    """
    if freq <= 0 or chan_type == "noise":
        return "—"
    midi = round(hz_to_midi(freq))
    if chan_type == "psg":
        midi += 12
    elif chan_type != "fm":
        return "—"
    if midi < 0 or midi > 127:
        return "—"
    return midi_name(midi)


# ---------------------------------------------------------------------------
# Key-on rows
# ---------------------------------------------------------------------------

def _noise_desc(register: int, tone2: int) -> str:
    """The noise register as words: 'white/N/512', 'white/tone2 N=0'.

    At rate 3 the LFSR is clocked by tone ch2.  The Sonic 1 driver writes PSG3's own note divider
    there ($C0); nMaxPSG maps to N=0, which the Sega VDP PSG treats as N=1 (maximum shift rate,
    near-white hiss)."""
    noise_type = "white" if noise_white(register) else "periodic"
    rate = noise_rate(register)
    if rate != _NOISE_TONE2_RATE:
        return f"{noise_type}/{_NOISE_RATES[rate]}"
    return f"{noise_type}/tone2 N={tone2}"


def _row(n: NoteStart) -> tuple:
    """A note start as a printed row (see parse_vgm)."""
    if n.chip == "fm":
        return (n.ms, n.channel, n.data, n.block, n.hz, pitch_name(n.hz), _smps_note(n.hz, "fm"))
    if n.channel == DAC_NAME:
        return (n.ms, DAC_NAME, n.data, 0, 0.0, f"seek {n.data}", "—")
    if n.channel == PSG_NAMES[NOISE_CHANNEL]:
        return (n.ms, n.channel, n.data, 0, n.hz, _noise_desc(n.data, n.tone2), "—")
    return (n.ms, n.channel, n.data, 0, n.hz, pitch_name(n.hz), _smps_note(n.hz, "psg"))


def parse_vgm(
    log: VgmLog,
    fm_clock: int,
    psg_clock: int,
    channel_filter: set[str] | None,
    chip: str,
    psg_mod_cents: float = DEFAULT_MOD_CENTS,
) -> tuple[list[tuple], dict[str, list[float]], dict[str, list[float]]]:
    """The key-on rows of a VGM log (`fm_clock` / `psg_clock` where its header gives none): one
    per note start (core.vgm.note_starts, its rules with `psg_mod_cents`; 0 = a row for every
    PSG period write).

    FM rows:       (time_ms, chan_name, fnum, block, freq_hz, note_name, smps_note)
    PSG tone rows: (time_ms, chan_name, period, 0, freq_hz, note_name, smps_note)
    PSG noise rows:(time_ms, "NOISE",  noise_reg, 0, lfsr_shift_hz, noise_desc, "—")
    DAC rows:      (time_ms, "DAC",    offset, 0, 0.0, "seek <offset>", "—")

    Returns (rows, fm_amp_samples, psg_amp_samples): the linear level of every FM / PSG note
    start, whatever `channel_filter` keeps.
    """
    state = ChipState.for_log(log, fm_clock, psg_clock)
    rows: list[tuple] = []
    amps: dict[str, dict[str, list[float]]] = {"fm": {}, "psg": {}}
    for n in note_starts(log, state, psg_mod_cents):
        if chip not in ("all", n.chip):
            continue
        if n.chip in amps:
            amps[n.chip].setdefault(n.channel, []).append(n.gain)
        if not channel_filter or n.channel in channel_filter:
            rows.append(_row(n))
    return rows, amps["fm"], amps["psg"]


# ---------------------------------------------------------------------------
# Volume suggestion
# ---------------------------------------------------------------------------

_CHANNEL_ORDER = ["FM1", "FM2", "FM3", "FM4", "FM5", "FM6", "PSG1", "PSG2", "PSG3", "NOISE"]


def print_volume_suggestions(
    fm_amp: dict[str, list[float]],
    psg_amp: dict[str, list[float]],
) -> None:
    """Print per-channel amplitude stats and suggested sample_list volumes (0–64)."""
    all_channels: dict[str, float] = {}
    for name, samples in {**fm_amp, **psg_amp}.items():
        if samples:
            all_channels[name] = statistics.median(samples)

    if not all_channels:
        print("No key-on events found.")
        return

    peak = max(all_channels.values())

    print(f"\n{'Channel':<8}  {'Median amp':>12}  {'Rel %':>7}  {'MOD vol (0-64)':>14}")
    print("-" * 50)
    for ch in _CHANNEL_ORDER:
        if ch not in all_channels:
            continue
        amp = all_channels[ch]
        rel = amp / peak
        vol = round(rel * 56)   # max → 56, leaving headroom for Cxx boosts
        print(f"{ch:<8}  {amp:>12.4f}  {rel * 100:>6.1f}%  {vol:>14}")

    print()
    print("# Suggested volumes for sample_list entries:")
    for ch in _CHANNEL_ORDER:
        if ch not in all_channels:
            continue
        vol = round(all_channels[ch] / peak * 56)
        print(f"# {ch}: volume ~{vol}")


# ---------------------------------------------------------------------------
# Frame dump
# ---------------------------------------------------------------------------

def _key_text(keys: tuple[int, ...]) -> str:
    """Key writes as signs, '+' on / '-' off, with the slot mask where not all four: '-+', '+(3)'."""
    return "".join("-" if not k else "+" if k == _ALL_SLOTS else f"+({k:X})" for k in keys)


def _fm_line(f: FmFrame, clock: int) -> str:
    freq = fm_frequency_hz(f.fnum, f.block, clock)
    text = f"{f.fnum:>4}/{f.block}  {pitch_name(freq):<4}"
    if f.keys:
        text += f"  key {_key_text(f.keys):<3}"
    tls = ",".join(f"{tl:02X}" for tl in f.carrier_tls)
    return text + f"  tl {tls}  alg {f.feedback_algorithm & 7}  pan {f.pan:02X}"


def _psg_line(p: PsgFrame, clock: int) -> str:
    if p.noise is not None:
        text = f"noise {p.noise:X}"
    else:
        text = f"N={p.period:<4}  {pitch_name(psg_frequency_hz(p.period, clock)):<4}"
    if p.attenuations:
        text += f"  att {','.join(f'{a:X}' for a in p.attenuations)}"
    return text


def _dac_line(d: DacFrame) -> str:
    gaps = " ".join(f"{gap}x{n}" for gap, n in d.gaps)
    return f"start {','.join(str(s.offset) for s in d.starts)}  {d.writes} bytes  gaps {gaps}"


def _frame_lines(frame: Frame, chip: str, state: ChipState) -> Iterator[tuple[str, str]]:
    """(channel, text) for each channel written to in `frame` that `chip` covers."""
    if chip in ('fm', 'all'):
        for ch, f in enumerate(frame.fm):
            if f.keys or f.frequency_writes:
                yield f"FM{ch + 1}", _fm_line(f, state.fm_clock)
    if chip in ('psg', 'all'):
        for ch, p in enumerate(frame.psg):
            if p.attenuations or p.period_writes:
                yield ("NOISE" if ch == NOISE_CHANNEL else f"PSG{ch + 1}"), _psg_line(p, state.psg_clock)
    if chip in ('dac', 'all') and frame.dac.starts:
        yield "DAC", _dac_line(frame.dac)


def print_frames(log: VgmLog, state: ChipState, chip: str, channel_filter: set[str] | None, max_rows: int) -> None:
    """Each frame's writes, one line per channel written to: what the lift reads (core.vgm.frames)."""
    fl = frame_log(log, state)
    loop = f", loops to frame {fl.loop_frame}" if fl.loop_frame is not None else ""
    print(f"Frames : {len(fl.frames)} of {fl.frame_samples} samples, bursts at +{fl.phase},"
          f" frame 0 opens at sample {fl.origin}{loop}")
    print()
    print(f"{'frame':>6}  {'time_ms':>9}  {'chan':<5}  state (FM: fnum/block, keys written, carrier TLs; "
          "PSG: period, attenuations written)")
    print("-" * 96)

    shown, total = 0, 0
    for frame in fl.frames:
        for name, text in _frame_lines(frame, chip, state):
            if channel_filter and name not in channel_filter:
                continue
            total += 1
            if max_rows and shown >= max_rows:
                continue
            shown += 1
            print(f"{frame.index:>6}  {fl.seconds(frame.index) * 1000:>9.1f}  {name:<5}  {text}")
    if total > shown:
        print(f"  ... {total - shown} more rows (use --max-rows to show more)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Analyze YM2612 FM and SN76489 PSG channel pitches from a VGM/VGZ recording",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("file", help="VGM or VGZ file to analyze")
    ap.add_argument(
        "--chip", choices=("fm", "psg", "dac", "all"), default="fm",
        help="Which chip to analyze: fm (default), psg, dac (PCM seeks), or all",
    )
    ap.add_argument(
        "--channel", nargs="+", metavar="CH",
        help="Show only these channels (e.g. --channel FM3 FM4 / --channel PSG1 NOISE DAC)",
    )
    ap.add_argument(
        "--clock", type=int, default=DEFAULT_FM_CLOCK,
        help=f"YM2612 clock in Hz (default {DEFAULT_FM_CLOCK}; read from file if present)",
    )
    ap.add_argument(
        "--psg-clock", type=int, default=DEFAULT_PSG_CLOCK,
        dest="psg_clock",
        help=f"SN76489 clock in Hz (default {DEFAULT_PSG_CLOCK}; read from file if present)",
    )
    ap.add_argument(
        "--psg-mod-cents", type=float, default=DEFAULT_MOD_CENTS, dest="psg_mod_cents", metavar="CENTS",
        help="PSG period moves within CENTS of the note's starting pitch are modulation, not a new "
             "note (default 70; 0 = one row per period write)",
    )
    ap.add_argument(
        "--max-rows", type=int, default=200,
        help="Maximum rows to print (default 200; use 0 for unlimited)",
    )
    ap.add_argument(
        "--volumes", action="store_true",
        help="Print per-channel amplitude stats and suggested sample_list volumes instead of event table",
    )
    ap.add_argument(
        "--frames", action="store_true",
        help="Print the log frame by frame (every channel written to in each V-int frame) instead of key-ons",
    )
    args = ap.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    try:
        log = read_vgm(path)
    except (VgmError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    channel_filter = set(args.channel) if args.channel else None
    if args.frames:
        print(f"File   : {path}")
        if "track" in log.tags:
            print(f"Track  : {log.tags['track']} ({log.tags.get('game', '?')})")
        print_frames(log, ChipState.for_log(log, args.clock, args.psg_clock), args.chip, channel_filter, args.max_rows)
        return

    rows, fm_amp, psg_amp = parse_vgm(
        log,
        fm_clock=args.clock,
        psg_clock=args.psg_clock,
        channel_filter=channel_filter,
        chip=args.chip,
        psg_mod_cents=args.psg_mod_cents,
    )

    print(f"File   : {path}")
    print(f"Chip   : {args.chip}")
    if args.chip in ('fm', 'all'):
        print(f"FM clk : {args.clock} Hz")
    if args.chip in ('psg', 'all'):
        print(f"PSG clk: {args.psg_clock} Hz")
    if channel_filter:
        print(f"Filter : {', '.join(sorted(channel_filter))}")
    print(f"Events : {len(rows)} key-on events found")

    if args.volumes:
        print_volume_suggestions(fm_amp, psg_amp)
        return

    print()
    print(f"{'time_ms':>10}  {'chan':<5}  {'data':>6}  {'blk':>3}  {'freq_hz':>10}  {'note':<8}  smps")
    print("-" * 68)

    limit = args.max_rows if args.max_rows > 0 else len(rows)
    for row in rows[:limit]:
        time_ms, ch, data_val, block, freq, note, smps = row
        blk_str  = str(block) if ch.startswith("FM") else "--"
        freq_str = f"{freq:>10.2f}" if freq > 0 else f"{'--':>10}"
        print(f"{time_ms:>10.1f}  {ch:<5}  {data_val:>6}  {blk_str:>3}  {freq_str}  {note:<8}  {smps}")

    if len(rows) > limit:
        print(f"  ... {len(rows) - limit} more rows (use --max-rows to show more)")

    # Per-channel unique note summary (smps convention), sorted by frequency
    seen_keys: dict[str, dict[tuple, float]] = {}   # chan -> {(note,smps): freq}
    for row in rows:
        _, ch, _, _, freq, note, smps = row
        seen_keys.setdefault(ch, {})
        key = (note, smps)
        if key not in seen_keys[ch]:
            seen_keys[ch][key] = freq

    if seen_keys:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row[1]] = counts.get(row[1], 0) + 1
        print()
        print("Unique notes per channel  (standard -> SMPS label):")
        print("-" * 68)
        for ch_name in sorted(seen_keys.keys()):
            pairs = sorted(seen_keys[ch_name].items(), key=lambda kv: kv[1] if kv[1] > 0 else float('inf'))
            if all(smps == "—" for (_, smps), _ in pairs):
                # Noise / DAC channel — just list descriptors
                parts = [note for (note, _), _ in pairs]
            else:
                parts = [f"{note}->{smps}" for (note, smps), _ in pairs]
            print(f"  {ch_name:<6} ({counts[ch_name]:3d} key-ons): {',  '.join(parts)}")


if __name__ == "__main__":
    main()
