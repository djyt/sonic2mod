#!/usr/bin/env python3
"""YM2612 + SN76489 pitch/event analyzer for VGM/VGZ files.

Parses YM2612 register writes and SN76489 register writes, and outputs a
table of key-on events showing channel, frequency data, and nearest note
name.  Use this to verify synth_root values against the actual chip output
from a game recording, or to count PSG noise events.

Usage::

    python tools/vgm_analyze.py "reference/vgm/02 - Green Hill Zone.vgz"
    python tools/vgm_analyze.py file.vgz --chip fm --channel FM3 FM4 FM5
    python tools/vgm_analyze.py file.vgz --chip psg --channel NOISE
    python tools/vgm_analyze.py file.vgz --chip all --max-rows 0
    python tools/vgm_analyze.py file.vgz --max-rows 500 --clock 7670454

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
    time_ms   — milliseconds from track start
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
    —         — (freq_hz column unused)
    note      — decoded noise type string (e.g. white/N/512)
    smps      — "—" (no pitch meaning for noise)
"""

from __future__ import annotations

import argparse
import gzip
import math
import struct
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Frequency math
# ---------------------------------------------------------------------------

# VGM files use 44100 Hz as the sample clock for wait commands.
_VGM_SAMPLE_RATE = 44100

# Sonic 1 NTSC YM2612 master clock (Hz).  Override with --clock if needed.
_DEFAULT_FM_CLOCK = 7_670_454

# Sonic 1 NTSC SN76489 clock (Hz).  Override with --psg-clock if needed.
_DEFAULT_PSG_CLOCK = 3_579_545

_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def _fnum_to_hz(fnum: int, block: int, clock: int) -> float:
    """Convert YM2612 fnum/block pair to frequency in Hz.

    Formula: freq = clock x fnum / (144 x 2^(20 - block))

    From OPN2 datasheet: Fnum = f0 x 2^(20-B) / (fM/144)
    => f0 = Fnum x fM / (144 x 2^(20-B))
    Verified: A4=440 Hz -> fnum=541, block=4 with clock=7670454.
    """
    return clock * fnum / (144 * (1 << (20 - block)))


def _psg_period_to_hz(period: int, clock: int) -> float:
    """Convert SN76489 10-bit tone period to frequency in Hz.

    Formula: freq = clock / (32 * period)
    """
    if period <= 0:
        return 0.0
    return clock / (32 * period)


def _nearest_note(freq: float) -> str:
    """Return the nearest note name (e.g. 'G3', 'C#2') for a frequency in Hz."""
    if freq <= 0:
        return "---"
    midi = round(69 + 12 * math.log2(freq / 440.0))
    name = _NOTE_NAMES[midi % 12]
    octave = midi // 12 - 1
    return f"{name}{octave}"


def _smps_note(freq: float, chan_type: str) -> str:
    """Return the SMPS-convention note name for a chip output frequency.

    In Sonic 1 SMPS the note byte labels are offset from standard pitch:
      FM  — label is one octave *lower* than what the chip outputs
            e.g. nA5 in the assembly → chip plays A6 (standard)
            Conversion: subtract 12 from MIDI number
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
    midi = round(69 + 12 * math.log2(freq / 440.0))
    if chan_type == "fm":
        midi -= 12
    elif chan_type == "psg":
        midi += 12
    else:
        return "—"
    if midi < 0 or midi > 127:
        return "—"
    name = _NOTE_NAMES[midi % 12]
    octave = midi // 12 - 1
    return f"{name}{octave}"


# ---------------------------------------------------------------------------
# VGM parser
# ---------------------------------------------------------------------------

def _parse_vgm(
    data: bytes,
    fm_clock: int,
    psg_clock: int,
    channel_filter: set[str] | None,
    chip: str,
) -> list[tuple]:
    """Parse VGM binary data and return a list of key-on event rows.

    FM rows:       (time_ms, chan_name, fnum, block, freq_hz, note_name)
    PSG tone rows: (time_ms, chan_name, period, 0, freq_hz, note_name)
    PSG noise rows:(time_ms, "NOISE",  noise_reg, 0, 0.0, noise_desc)
    """
    # Version at 0x08
    version = struct.unpack_from('<I', data, 0x08)[0]

    # Data start: version >= 1.50 uses a relative offset at 0x34
    if version >= 0x150:
        rel = struct.unpack_from('<I', data, 0x34)[0]
        pos = (0x34 + rel) if rel else 0x40
    else:
        pos = 0x40

    # Override FM clock from file header (offset 0x2C); strip T6/bit-30 flags
    if chip in ('fm', 'all'):
        file_fm_clock = struct.unpack_from('<I', data, 0x2C)[0]
        if file_fm_clock:
            fm_clock = file_fm_clock & 0x3FFF_FFFF

    # Override PSG clock from file header (offset 0x0C); strip flags
    if chip in ('psg', 'all'):
        file_psg_clock = struct.unpack_from('<I', data, 0x0C)[0]
        if file_psg_clock:
            psg_clock = file_psg_clock & 0x3FFF_FFFF

    # Per-channel FM state: bank 0 = FM1-3, bank 1 = FM4-6
    fnum_lo: list[list[int]] = [[0, 0, 0], [0, 0, 0]]
    fnum_hi: list[list[int]] = [[0, 0, 0], [0, 0, 0]]

    # Per-channel PSG state
    psg_vol       = [0xF, 0xF, 0xF, 0xF]  # 4-bit attenuation; 0xF = silent
    psg_freq      = [0, 0, 0]              # 10-bit tone period (channels 0-2)
    psg_prev_freq = [0, 0, 0]              # period at last key-on (period-change detection)
    psg_noise     = 0                      # 3-bit noise register
    psg_latch_ch   = None                # last latched channel (0-3)
    psg_latch_type = None                # 0 = freq, 1 = vol

    sample_count = 0
    rows: list[tuple] = []

    def _emit_fm_keyon(bank: int, ch_idx: int) -> None:
        ch_name = f"FM{bank * 3 + ch_idx + 1}"
        if channel_filter and ch_name not in channel_filter:
            return
        lo    = fnum_lo[bank][ch_idx]
        hi    = fnum_hi[bank][ch_idx]
        block = (hi >> 3) & 0x7
        fnum  = ((hi & 0x7) << 8) | lo
        if fnum == 0:
            return
        freq    = _fnum_to_hz(fnum, block, fm_clock)
        note    = _nearest_note(freq)
        smps    = _smps_note(freq, "fm")
        time_ms = sample_count * 1000.0 / _VGM_SAMPLE_RATE
        rows.append((time_ms, ch_name, fnum, block, freq, note, smps))

    def _emit_psg_keyon(ch: int) -> None:
        """Emit a PSG key-on event.

        Triggered either by a volume transition (0xF → audible) or by a tone
        period change while the channel is already audible (portamento / arpeggio
        without intervening silence).
        """
        time_ms = sample_count * 1000.0 / _VGM_SAMPLE_RATE
        if ch < 3:
            ch_name = f"PSG{ch + 1}"
            if channel_filter and ch_name not in channel_filter:
                return
            period  = psg_freq[ch]
            freq    = _psg_period_to_hz(period, psg_clock)
            note    = _nearest_note(freq)
            smps    = _smps_note(freq, "psg")
            psg_prev_freq[ch] = period   # remember period so we don't double-emit
            rows.append((time_ms, ch_name, period, 0, freq, note, smps))
        else:
            if channel_filter and "NOISE" not in channel_filter:
                return
            noise_type = "white" if (psg_noise >> 2) & 1 else "periodic"
            rate_idx   = psg_noise & 0x3
            rate_str   = ("N/512", "N/1024", "N/2048", "psgtone")[rate_idx]
            noise_desc = f"{noise_type}/{rate_str}"
            rows.append((time_ms, "NOISE", psg_noise, 0, 0.0, noise_desc, "—"))

    end = len(data)
    while pos < end:
        cmd = data[pos]
        pos += 1

        if cmd in (0x52, 0x53) and chip in ('fm', 'all'):
            # YM2612 port 0 (FM1-3) or port 1 (FM4-6) write
            if pos + 1 >= end:
                break
            bank = cmd - 0x52
            reg  = data[pos]
            val  = data[pos + 1]
            pos += 2

            if 0xA0 <= reg <= 0xA2:
                # F-number low byte, ch 0-2
                fnum_lo[bank][reg - 0xA0] = val
            elif 0xA4 <= reg <= 0xA6:
                # Block + F-number high bits, ch 0-2
                fnum_hi[bank][reg - 0xA4] = val
            elif reg == 0x28 and bank == 0:
                # Key-on/off — reg 0x28 is always via port 0
                ch_raw  = val & 0x07
                op_mask = (val >> 4) & 0x0F
                if ch_raw == 3:
                    continue   # unused slot
                if op_mask == 0:
                    continue   # key-off, not key-on
                # Map raw ch field → (bank, ch_idx)
                if ch_raw >= 4:
                    _emit_fm_keyon(1, ch_raw - 4)
                else:
                    _emit_fm_keyon(0, ch_raw)

        elif cmd == 0x52 and chip not in ('fm', 'all'):
            # Skip FM write (2 bytes) when not analyzing FM
            pos += 2

        elif cmd == 0x53 and chip not in ('fm', 'all'):
            pos += 2

        elif cmd == 0x50 and chip in ('psg', 'all'):
            # SN76489 write
            if pos >= end:
                break
            b = data[pos]; pos += 1
            if b & 0x80:
                # Latch byte: identifies channel and type
                ch  = (b >> 5) & 3
                typ = (b >> 4) & 1
                nib = b & 0xF
                psg_latch_ch   = ch
                psg_latch_type = typ
                if typ == 1:
                    # Volume latch
                    old = psg_vol[ch]
                    psg_vol[ch] = nib
                    if old == 0xF and nib < 0xF:
                        # Transition from silent → audible = key-on
                        _emit_psg_keyon(ch)
                elif ch == 3:
                    # Noise frequency latch
                    psg_noise = nib & 0x7
                else:
                    # Tone frequency low nibble
                    psg_freq[ch] = (psg_freq[ch] & 0x3F0) | nib
                    # Secondary key-on: period changed while channel is audible
                    if psg_vol[ch] < 0xF and psg_freq[ch] != psg_prev_freq[ch]:
                        _emit_psg_keyon(ch)
            elif psg_latch_ch is not None and psg_latch_type == 0 and psg_latch_ch < 3:
                # Data byte: high 6 bits of tone period
                psg_freq[psg_latch_ch] = (b & 0x3F) << 4 | (psg_freq[psg_latch_ch] & 0xF)
                # Secondary key-on: period changed while channel is audible
                ch = psg_latch_ch
                if psg_vol[ch] < 0xF and psg_freq[ch] != psg_prev_freq[ch]:
                    _emit_psg_keyon(ch)

        elif cmd == 0x50 and chip not in ('psg', 'all'):
            # Skip PSG write when not analyzing PSG
            pos += 1

        elif cmd == 0x61:
            # Wait N samples
            if pos + 1 >= end:
                break
            n = struct.unpack_from('<H', data, pos)[0]
            pos += 2
            sample_count += n

        elif cmd == 0x62:
            sample_count += 735    # 1 NTSC frame

        elif cmd == 0x63:
            sample_count += 882    # 1 PAL frame

        elif cmd == 0x66:
            break   # end of data

        elif cmd == 0x67:
            # Data block: 0x66 (compat byte) + 1-byte type + 4-byte size + data
            if pos + 5 >= end:
                break
            pos += 2   # skip compat byte (always 0x66) and type byte
            size = struct.unpack_from('<I', data, pos)[0]
            pos += 4 + size

        elif 0x70 <= cmd <= 0x7F:
            sample_count += (cmd & 0x0F) + 1

        elif 0x80 <= cmd <= 0x8F:
            # YM2612 DAC write from PCM bank, then wait
            sample_count += cmd & 0x0F

        elif cmd == 0xE0:
            # PCM seek
            pos += 4

        elif 0x90 <= cmd <= 0x95:
            # DAC stream control commands (various fixed lengths)
            _DAC_LENGTHS = {0x90: 4, 0x91: 4, 0x92: 5, 0x93: 10, 0x94: 1, 0x95: 4}
            pos += _DAC_LENGTHS.get(cmd, 1)

        # Unknown commands: skip 1 byte and continue (best-effort)

    return rows


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
        "--chip", choices=("fm", "psg", "all"), default="fm",
        help="Which chip to analyze: fm (default), psg, or all",
    )
    ap.add_argument(
        "--channel", nargs="+", metavar="CH",
        help="Show only these channels (e.g. --channel FM3 FM4 / --channel PSG1 NOISE)",
    )
    ap.add_argument(
        "--clock", type=int, default=_DEFAULT_FM_CLOCK,
        help=f"YM2612 clock in Hz (default {_DEFAULT_FM_CLOCK}; read from file if present)",
    )
    ap.add_argument(
        "--psg-clock", type=int, default=_DEFAULT_PSG_CLOCK,
        dest="psg_clock",
        help=f"SN76489 clock in Hz (default {_DEFAULT_PSG_CLOCK}; read from file if present)",
    )
    ap.add_argument(
        "--max-rows", type=int, default=200,
        help="Maximum rows to print (default 200; use 0 for unlimited)",
    )
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    raw = path.read_bytes()

    # Detect and decompress VGZ (gzip-compressed VGM)
    if path.suffix.lower() == ".vgz" or raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)

    if raw[:4] != b'Vgm ':
        print("ERROR: not a valid VGM file (bad magic bytes)", file=sys.stderr)
        sys.exit(1)

    channel_filter = set(args.channel) if args.channel else None
    rows = _parse_vgm(
        raw,
        fm_clock=args.clock,
        psg_clock=args.psg_clock,
        channel_filter=channel_filter,
        chip=args.chip,
    )

    limit = args.max_rows if args.max_rows > 0 else len(rows)

    print(f"File   : {path}")
    print(f"Chip   : {args.chip}")
    if args.chip in ('fm', 'all'):
        print(f"FM clk : {args.clock} Hz")
    if args.chip in ('psg', 'all'):
        print(f"PSG clk: {args.psg_clock} Hz")
    if channel_filter:
        print(f"Filter : {', '.join(sorted(channel_filter))}")
    print(f"Events : {len(rows)} key-on events found")
    print()
    print(f"{'time_ms':>10}  {'chan':<5}  {'data':>6}  {'blk':>3}  {'freq_hz':>10}  {'note':<8}  smps")
    print("-" * 68)

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
        print()
        print("Unique notes per channel  (standard -> SMPS label):")
        print("-" * 68)
        for ch_name in sorted(seen_keys.keys()):
            pairs = sorted(seen_keys[ch_name].items(), key=lambda kv: kv[1] if kv[1] > 0 else float('inf'))
            if all(smps == "—" for (_, smps), _ in pairs):
                # Noise channel — just list descriptors
                parts = [note for (note, _), _ in pairs]
            else:
                parts = [f"{note}->{smps}" for (note, smps), _ in pairs]
            print(f"  {ch_name:<6}: {',  '.join(parts)}")


if __name__ == "__main__":
    main()
