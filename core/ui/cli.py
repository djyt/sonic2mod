"""Shared Rich console chrome for the three CLI entry points.

`convert.py`, `analyze.py` and `sonic2wav.py` all print the same branding panel and
the same right-aligned label column.  They get it from here rather than each keeping
its own copy.

    from core.ui.cli import LABEL_W, branding, cli_console, error_printer, row_printer

    console = cli_console()
    _row    = row_printer(console)
    _error  = error_printer(console)

    branding(console, "SONIC2MOD", get_version())
    _row("Parse", "input.asm", "9 channels")
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ..audit import CONFIG_ROOT, RIPS_MAP, RipShelf

LABEL_W = 9                        # right-aligned label column width
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


def add_variant_argument(parser: argparse.ArgumentParser) -> None:
    """`--variant NAME`, the same on every CLI that reads a song config (core.config.apply_variant)."""
    parser.add_argument("--variant", metavar="NAME",
                        help="read the config as its variant NAME: each `variants: {NAME: ...}` block laid "
                             "over the keys beside it (default output_file: <stem>_NAME.mod)")


def add_shelf_arguments(parser: argparse.ArgumentParser) -> None:
    """`--configs`, `--vgz-dir`, `--rips`: which configs and rips a rip tool pairs (rip_shelf)."""
    parser.add_argument("--configs", metavar="DIR",
                        help=f"the configs (default {CONFIG_ROOT.name}/, or the rips' mirror)")
    parser.add_argument("--vgz-dir", metavar="DIR",
                        help="the rips (default: the folder rips.yaml names, else the configs' mirror)")
    parser.add_argument("--rips", metavar="FILE",
                        help=f"YAML {{config stem: rip file}} (default: {RIPS_MAP} beside the configs, else by number)")


def rip_shelf(args: argparse.Namespace, configs: Path | None = None, rips: Path | None = None) -> RipShelf:
    """The shelf add_shelf_arguments' arguments name (`configs` / `rips`: a folder in their place)."""
    try:
        return RipShelf.around(configs or args.configs, rips or args.vgz_dir, args.rips)
    except ValueError as e:
        raise SystemExit(f"{e} (--vgz-dir / --configs)") from e


def error_printer(console: Console) -> Callable[[str], NoReturn]:
    """Return an `error(msg)` that prints in the label column and exits 1."""

    def error(msg: str) -> NoReturn:
        console.print(f"\n  [bold red]{'Error':>{LABEL_W}}[/bold red]   {msg}\n")
        sys.exit(1)

    return error
