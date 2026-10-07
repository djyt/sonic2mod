"""What the CLIs print.

    report.py       convert.py's report
    pitch_audit.py  the symbolic pitch audit's report (vgm_pitch_audit.py, vgm_compare.py)
    song_diff.py    compare_songs' result as text (vgm_lift.py, rom_import.py)
    cli.py          the console chrome all three CLIs share
"""

from .cli import LABEL_W, add_variant_argument, branding, cli_console, error_printer, row_printer
from .pitch_audit import print_audit, verdict_text
from .report import Report, print_report
from .song_diff import diff_counts, song_diff_lines

__all__ = [
    "LABEL_W", "Report", "add_variant_argument", "branding", "cli_console", "diff_counts", "error_printer", "print_audit", "print_report",
    "row_printer", "song_diff_lines", "verdict_text"
]
