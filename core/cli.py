"""Shared Rich console chrome for the three CLI entry points.

`convert.py`, `analyze.py` and `sonic2wav.py` all print the same branding panel and
the same right-aligned label column.  They get it from here rather than each keeping
its own copy.

    from core.cli import LABEL_W, branding, cli_console, error_printer, row_printer

    console = cli_console()
    _row    = row_printer(console)
    _error  = error_printer(console)

    branding(console, "SONIC2MOD", get_version())
    _row("Parse", "input.asm", "9 channels")
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from typing import NoReturn

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

LABEL_W = 9                        # right-aligned label column width
INDENT = " " * (LABEL_W + 4)       # indentation for continuation lines

def _force_utf8_stdout() -> None:
    """Re-wrap stdout as UTF-8 on Windows so Rich can render Unicode symbols.

    Idempotent: a stdout that already encodes UTF-8 is left alone, so calling this
    more than once (or under PYTHONUTF8) costs nothing.
    """
    if sys.platform != "win32" or not hasattr(sys.stdout, "buffer"):
        return
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "")
    if encoding == "utf8":
        return
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def cli_console(highlight: bool = False) -> Console:
    """A Console configured the way every sonic2mod CLI wants it."""
    _force_utf8_stdout()
    return Console(highlight=highlight, legacy_windows=False)


def branding(console: Console, product: str, version: str) -> None:
    """Print the boxed product banner."""
    t = Text(justify="center")
    t.append(product, style="bold bright_yellow")
    t.append(f"  v{version}", style="bold cyan")
    t.append("  ·  reassembler", style="dim white")
    console.print(Panel(Align.center(t), border_style="yellow", padding=(0, 2)))
    console.print()


def row_printer(console: Console) -> Callable[..., None]:
    """Return a `row(label, *lines)` that prints a labelled section with aligned continuations."""

    def row(label: str, *lines: str) -> None:
        for i, line in enumerate(lines):
            if not line:
                continue
            if i == 0:
                console.print(f"  [bold]{label:>{LABEL_W}}[/bold]   {line}")
            else:
                console.print(f"  {' ' * LABEL_W}   {line}")

    return row


def error_printer(console: Console) -> Callable[[str], NoReturn]:
    """Return an `error(msg)` that prints in the label column and exits 1."""

    def error(msg: str) -> NoReturn:
        console.print(f"\n  [bold red]{'Error':>{LABEL_W}}[/bold red]   {msg}\n")
        sys.exit(1)

    return error
