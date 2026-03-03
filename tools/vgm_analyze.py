#!/usr/bin/env python3
"""YM2612 pitch analyzer for VGM/VGZ files.

Parses YM2612 register writes and outputs a table of key-on events showing
channel, fnum, block, frequency (Hz), and nearest note name.  Use this to
verify synth_root values against the actual chip output from a game recording.

Usage::

    python tools/vgm_analyze.py "reference/vgm/02 - Green Hill Zone.vgz"
    python tools/vgm_analyze.py file.vgz --channel FM3 FM4 FM5
    python tools/vgm_analyze.py file.vgz --max-rows 500 --clock 7670454

Output columns:
    time_ms   — milliseconds from track start (VGM 44100 Hz sample clock)
    chan      — FM1..FM6
    fnum      — raw frequency number written to YM2612
    blk       — block (octave shift), 0–7
    freq_hz   — computed frequency using chip formula
    note      — nearest semitone name (e.g. G3, C#2)
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
_DEFAULT_CLOCK = 7_670_454

_NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']


def _fnum_to_hz(fnum: int, block: int, clock: int) -> float:
    """Convert YM2612 fnum/block pair to frequency in Hz.

    Formula: freq = clock x fnum / (144 x 2^(20 - block))

    From OPN2 datasheet: Fnum = f0 x 2^(20-B) / (fM/144)
    => f0 = Fnum x fM / (144 x 2^(20-B))
    Verified: A4=440 Hz -> fnum=541, block=4 with clock=7670454.
    """
    return clock * fnum / (144 * (1 << (20 - block)))


def _nearest_note(freq: float) -> str:
    """Return the nearest note name (e.g. 'G3', 'C#2') for a frequency in Hz."""
    if freq <= 0:
        return "---"
    midi = round(69 + 12 * math.log2(freq / 440.0))
    name = _NOTE_NAMES[midi % 12]
    octave = midi // 12 - 1
    return f"{name}{octave}"


# ---------------------------------------------------------------------------
# VGM parser
# ---------------------------------------------------------------------------

def _parse_vgm(
    data: bytes,
    clock: int,
    channel_filter: set[str] | None,
) -> list[tuple]:
    """Parse VGM binary data and return a list of key-on event rows.

    Each row: (time_ms, chan_name, fnum, block, freq_hz, note_name)
    """
    # Version at 0x08
    version = struct.unpack_from('<I', data, 0x08)[0]

    # Data start: version >= 1.50 uses a relative offset at 0x34
    if version >= 0x150:
        rel = struct.unpack_from('<I', data, 0x34)[0]
        pos = (0x34 + rel) if rel else 0x40
    else:
        pos = 0x40

    # Override clock from file header (offset 0x2C); strip T6/bit-30 flags
    file_clock = struct.unpack_from('<I', data, 0x2C)[0]
    if file_clock:
        clock = file_clock & 0x3FFF_FFFF

    # Per-channel frequency state: bank 0 = FM1-3, bank 1 = FM4-6
    # fnum_lo[bank][ch], fnum_hi[bank][ch]  (hi byte encodes block + fnum hi bits)
    fnum_lo: list[list[int]] = [[0, 0, 0], [0, 0, 0]]
    fnum_hi: list[list[int]] = [[0, 0, 0], [0, 0, 0]]

    sample_count = 0
    rows: list[tuple] = []

    def _emit_keyon(bank: int, ch_idx: int) -> None:
        ch_name = f"FM{bank * 3 + ch_idx + 1}"
        if channel_filter and ch_name not in channel_filter:
            return
        lo   = fnum_lo[bank][ch_idx]
        hi   = fnum_hi[bank][ch_idx]
        block = (hi >> 3) & 0x7
        fnum  = ((hi & 0x7) << 8) | lo
        if fnum == 0:
            return
        freq   = _fnum_to_hz(fnum, block, clock)
        note   = _nearest_note(freq)
        time_ms = sample_count * 1000.0 / _VGM_SAMPLE_RATE
        rows.append((time_ms, ch_name, fnum, block, freq, note))

    end = len(data)
    while pos < end:
        cmd = data[pos]
        pos += 1

        if cmd in (0x52, 0x53):
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
                    _emit_keyon(1, ch_raw - 4)
                else:
                    _emit_keyon(0, ch_raw)

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
        description="Analyze YM2612 FM channel pitches from a VGM/VGZ recording",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("file", help="VGM or VGZ file to analyze")
    ap.add_argument(
        "--channel", nargs="+", metavar="FMn",
        help="Show only these channels (e.g. --channel FM3 FM4 FM5)",
    )
    ap.add_argument(
        "--clock", type=int, default=_DEFAULT_CLOCK,
        help=f"YM2612 clock in Hz (default {_DEFAULT_CLOCK}; read from file if present)",
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
    rows = _parse_vgm(raw, clock=args.clock, channel_filter=channel_filter)

    limit = args.max_rows if args.max_rows > 0 else len(rows)

    print(f"File   : {path}")
    print(f"Clock  : {args.clock} Hz")
    if channel_filter:
        print(f"Filter : {', '.join(sorted(channel_filter))}")
    print(f"Events : {len(rows)} key-on events found")
    print()
    print(f"{'time_ms':>10}  {'chan':<5}  {'fnum':>5}  {'blk':>3}  {'freq_hz':>10}  note")
    print("-" * 54)

    for row in rows[:limit]:
        time_ms, ch, fnum, block, freq, note = row
        print(f"{time_ms:>10.1f}  {ch:<5}  {fnum:>5}  {block:>3}  {freq:>10.2f}  {note}")

    if len(rows) > limit:
        print(f"  ... {len(rows) - limit} more rows (use --max-rows to show more)")


if __name__ == "__main__":
    main()
