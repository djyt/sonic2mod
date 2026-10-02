"""Channel-by-channel comparison of two MODs (read with core.mod.read_mod), with the ability to
skip specified channel indices.
"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.mod import read_mod


def compare_mods(path_a, path_b, ignore_channels=None) -> list:
    """Compare two MODs musically (position-order traversal).

    Returns list of human-readable difference strings.
    Channels in ignore_channels (0-based) are silently skipped.
    """
    ignore = set(ignore_channels or [])
    a = read_mod(path_a)
    b = read_mod(path_b)

    diffs = []

    # The sample table and data: a mix that lost a layer, a loop that moved into the attack, a
    # composite's volume - none of it shows in the cells (2026-09-29: all three happened)
    for i, (sa, sb) in enumerate(zip(a.samples, b.samples, strict=True), 1):
        for key, label in (("length", "length"), ("volume", "volume"), ("finetune", "finetune"),
                           ("loop_start", "loop start"), ("loop_len", "loop length")):
            if getattr(sa, key) != getattr(sb, key):
                diffs.append(f"Sample {i}: {label} {getattr(sa, key)} vs {getattr(sb, key)}")
        if sa.length == sb.length and hashlib.md5(sa.data).digest() != hashlib.md5(sb.data).digest():
            diffs.append(f"Sample {i}: data differs ({sa.length} bytes)")

    if a.tag != b.tag:
        diffs.append(f"Format: {a.tag} vs {b.tag}")
    if a.channels != b.channels:
        diffs.append(f"Channels: {a.channels} vs {b.channels}")
        return diffs  # Can't compare cells meaningfully

    num_ch = a.channels

    # Traverse position list in order
    pos_a = a.order
    pos_b = b.order

    if pos_a != pos_b:
        diffs.append(f"Position list differs: {pos_a} vs {pos_b}")

    # Compare patterns referenced in A's position list
    for order_idx, pat_idx in enumerate(pos_a):
        if pat_idx >= len(a.patterns):
            continue
        rows_a = a.patterns[pat_idx]
        if pat_idx >= len(b.patterns):
            diffs.append(f"Pattern {pat_idx} (order {order_idx}): missing in B")
            continue
        rows_b = b.patterns[pat_idx]

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
