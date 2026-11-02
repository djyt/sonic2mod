"""Every text file git knows (tracked, or new and not ignored) is UTF-8, none garbled.

A script that reads UTF-8 as Windows' cp1252 and writes it back doubles each non-ASCII character:
`§` (bytes C2 A7) reads back as `Â` then `§`.  Such a run is found by undoing it: it encodes
to cp1252 bytes that decode as UTF-8.

    python tools/dev/text_encoding.py        each file:line, exit 1 if any (tools/dev/check.py runs it)
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SNIFF = 8192                       # bytes read for a NUL: a binary file
_BAD = 1

# A UTF-8 lead byte and its continuation bytes ($80-$BF), as cp1252 shows them
_LEADS = ((0xC2, 0xDF, 1), (0xE0, 0xEF, 2), (0xF0, 0xF4, 3))       # lead bytes, continuations after
_TAIL = re.escape(bytes(range(0x80, 0xC0)).decode("cp1252", errors="ignore"))
_GARBLED = re.compile("|".join(f"[{chr(lo)}-{chr(hi)}][{_TAIL}]{{{n}}}" for lo, hi, n in _LEADS))


def garbled(text: str) -> list[str]:
    """The runs of `text` that are UTF-8 read as cp1252."""
    found = []
    for m in _GARBLED.finditer(text):
        try:
            m.group().encode("cp1252").decode("utf-8")
        except UnicodeError:
            continue
        found.append(m.group())
    return found


def _files() -> list[Path]:
    listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                            cwd=ROOT, capture_output=True, check=True).stdout
    return [ROOT / name for name in listed.decode("utf-8").split("\0") if name]


def _problems(path: Path) -> list[str]:
    """What is wrong with one file's text: nothing for a binary or a deleted file."""
    if not path.is_file():
        return []
    data = path.read_bytes()
    if b"\0" in data[:_SNIFF]:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        return [f"not UTF-8 at byte {e.start}"]
    return [f"{n}: {' '.join(runs)}" for n, line in enumerate(text.splitlines(), 1) if (runs := garbled(line))]


def main() -> int:
    bad = 0
    for path in _files():
        for problem in _problems(path):
            print(f"{path.relative_to(ROOT).as_posix()}:{problem}")
            bad = _BAD
    return bad


if __name__ == "__main__":
    sys.exit(main())
