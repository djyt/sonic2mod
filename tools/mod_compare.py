"""MOD binary parser and channel comparator.

Parses ProTracker MOD binary into structured data and compares two MODs
channel-by-channel, with the ability to skip specified channel indices.
"""

import struct
from pathlib import Path

_FORMAT_CHANNELS = {
    "M.K.": 4, "M!K!": 4, "FLT4": 4, "FLT8": 8,
    "6CHN": 6, "8CHN": 8,
    "2CHN": 2, "3CHN": 3, "5CHN": 5, "7CHN": 7, "9CHN": 9,
    "10CH": 10, "11CH": 11, "12CH": 12, "16CH": 16, "32CH": 32,
    "OCTA": 8,
}


def parse_mod(path) -> dict:
    """Return dict with keys: format, num_channels, song_length,
    position_list, samples, patterns.
    patterns[p][row][ch] = (period, inst, effect_cmd, effect_param)
    """
    data = Path(path).read_bytes()

    format_id = data[1080:1084].decode("ascii", errors="replace")
    num_channels = _FORMAT_CHANNELS.get(format_id, 4)

    # Parse sample headers (31 × 30 bytes starting at offset 20)
    samples = []
    for i in range(31):
        off = 20 + i * 30
        name = data[off:off+22].rstrip(b"\x00").decode("ascii", errors="replace")
        length = struct.unpack_from(">H", data, off + 22)[0] * 2  # words → bytes
        finetune = data[off + 24] & 0x0F
        volume = data[off + 25]
        samples.append({"name": name, "length": length, "finetune": finetune, "volume": volume})

    song_length = data[950]
    position_list = list(data[952:952 + song_length])
    num_patterns = max(position_list) + 1 if position_list else 0

    # Parse patterns
    pat_offset = 1084
    cell_size = 4
    row_size = cell_size * num_channels
    pattern_size = row_size * 64

    patterns = []
    for p in range(num_patterns):
        rows = []
        for r in range(64):
            cells = []
            for c in range(num_channels):
                off = pat_offset + p * pattern_size + r * row_size + c * cell_size
                if off + 4 > len(data):
                    cells.append((0, 0, 0, 0))
                    continue
                b0, b1, b2, b3 = data[off], data[off+1], data[off+2], data[off+3]
                period = ((b0 & 0x0F) << 8) | b1
                inst   = (b0 & 0xF0) | (b2 >> 4)
                eff    = b2 & 0x0F
                param  = b3
                cells.append((period, inst, eff, param))
            rows.append(cells)
        patterns.append(rows)

    return {
        "format": format_id,
        "num_channels": num_channels,
        "song_length": song_length,
        "position_list": position_list,
        "samples": samples,
        "patterns": patterns,
    }


def compare_mods(path_a, path_b, ignore_channels=None) -> list:
    """Compare two MODs musically (position-order traversal).

    Returns list of human-readable difference strings.
    Channels in ignore_channels (0-based) are silently skipped.
    """
    ignore = set(ignore_channels or [])
    a = parse_mod(path_a)
    b = parse_mod(path_b)

    diffs = []

    if a["format"] != b["format"]:
        diffs.append(f"Format: {a['format']} vs {b['format']}")
    if a["num_channels"] != b["num_channels"]:
        diffs.append(f"Channels: {a['num_channels']} vs {b['num_channels']}")
        return diffs  # Can't compare cells meaningfully

    num_ch = a["num_channels"]

    # Traverse position list in order
    pos_a = a["position_list"]
    pos_b = b["position_list"]

    if pos_a != pos_b:
        diffs.append(f"Position list differs: {pos_a} vs {pos_b}")

    # Compare patterns referenced in A's position list
    for order_idx, pat_idx in enumerate(pos_a):
        if pat_idx >= len(a["patterns"]):
            continue
        rows_a = a["patterns"][pat_idx]
        if pat_idx >= len(b["patterns"]):
            diffs.append(f"Pattern {pat_idx} (order {order_idx}): missing in B")
            continue
        rows_b = b["patterns"][pat_idx]

        for row_idx in range(64):
            row_a = rows_a[row_idx] if row_idx < len(rows_a) else [(0,0,0,0)] * num_ch
            row_b = rows_b[row_idx] if row_idx < len(rows_b) else [(0,0,0,0)] * num_ch
            for ch in range(num_ch):
                if ch in ignore:
                    continue
                ca = row_a[ch]
                cb = row_b[ch]
                if ca != cb:
                    diffs.append(
                        f"pat={pat_idx} row={row_idx:02d} ch={ch}: "
                        f"A={ca} vs B={cb}"
                    )
                    if len(diffs) > 200:
                        diffs.append("... (truncated at 200 differences)")
                        return diffs

    return diffs
