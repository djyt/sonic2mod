"""Which regression cases a change can move, from what each case ran.  tests/regression.py and
tests/tool_regression.py select their cases with it.

    record   --generate-baselines runs each case under coverage.py with the chip render caches
             off (a cached render runs no emulator code) and keeps what it ran, in the suite's
             coverage.yaml:   case: commit, argv, inputs (path: hash), files (the project's, executed)

    select   a run diffs the working tree with each case's commit (changed and untracked files)
             and runs the case when
                 a file it executed changed              core/drivers/smpsz80/...  -> Golden Axe's cases
                 an input's hash changed                 its config, a ROM, a rip, a baseline MOD
                 its arguments changed                   tests/cases.yaml
                 a non-Python file changed beside code it executed     ym2612/ym3438_batch.c
             and every case when a change cannot be placed: the runner's own code,
             configs/settings.yaml, any other file no rule covers.  A Python file no case executed
             moves none (a new module moves only through the file that imports it); docs, notes
             and baselines move none; data in configs/ and tests/ moves only the cases it is an
             input of.

    A case with no record runs.  --all runs every case.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# core/render_cache.py reads it: "off" keeps every chip render cache shut for one process
_RENDER_CACHE_ENV = "SONIC2MOD_RENDER_CACHE"
_CACHE_OFF = "off"

# Changes that move no case: docs, notes, baselines, lint settings, the case lists (argv is recorded)
_MOVES_NONE = ("docs/", "tests/baselines/", "tests/tool_baselines/", ".claude/", ".gitignore",
               "pyproject.toml", "vulture_whitelist.py", "tests/cases.yaml")
_NOTES = ".md"
_INPUT_ONLY = ("configs/", "tests/")       # data read only as a case's declared input ...
_LIVE_SETTINGS = "configs/settings.yaml"   # ... but every tool reads this one
_PYTHON = ".py"

_HASH_CHARS = 12
_HEADER = "# Written by --generate-baselines (tests/selection.py): what each case ran, to select it by.\n"


@dataclass(frozen=True)
class CaseRun:
    """What a suite runs for one case: `argv` after the interpreter, from the project root."""

    name: str
    argv: tuple[str, ...]
    inputs: tuple[Path, ...] = ()


@dataclass
class Selection:
    names: set[str]
    reasons: dict[str, str] = field(default_factory=dict)    # case -> why it runs
    everything: str = ""                                     # why every case runs ("" when not)


# --- recording -------------------------------------------------------------------------------

def run_recorded(case: CaseRun, **run_args) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run `case` under coverage.py: its result and the project files it executed."""
    try:
        import coverage
    except ImportError:
        raise SystemExit("--generate-baselines records what each case runs: pip install coverage") from None

    with tempfile.TemporaryDirectory(prefix="coverage_") as tmp:
        data = Path(tmp) / ".coverage"
        rc = Path(tmp) / "coveragerc"
        rc.write_text(f"[run]\ndata_file = {data.as_posix()}\nparallel = true\n"
                      f"concurrency = thread,multiprocessing\nsource = {ROOT.as_posix()}\n", encoding="utf-8")
        argv = [sys.executable, "-m", "coverage", "run", f"--rcfile={rc}", *case.argv]
        env = {**os.environ, _RENDER_CACHE_ENV: _CACHE_OFF}
        result = subprocess.run(argv, cwd=ROOT, env=env, check=False, **run_args)

        cov = coverage.Coverage(data_file=str(data), config_file=str(rc))
        cov.combine([tmp], keep=True)
        return result, sorted(_relative(f) for f in cov.get_data().measured_files() if _inside(f))


def save_record(path: Path, ran: dict[str, tuple[CaseRun, list[str]]]) -> None:
    """Add (or replace) each case's record: the commit, its argv and inputs, the files it ran."""
    record = load_record(path)
    commit = head_commit()
    for name, (case, files) in ran.items():
        record[name] = {"commit": commit, "argv": list(case.argv), "inputs": _hashes(case.inputs), "files": files}
    text = yaml.safe_dump(record, sort_keys=True, default_flow_style=False, width=120)
    path.write_text(_HEADER + text, encoding="utf-8", newline="\n")


def load_record(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# --- selecting -------------------------------------------------------------------------------

def select(cases: Iterable[CaseRun], record_path: Path, runner_files: Iterable[str]) -> Selection:
    """The cases the working tree's changes can move (module docstring)."""
    cases = list(cases)
    record = load_record(record_path)
    runner = set(runner_files)
    code_dirs = {_parent(f) for entry in record.values() for f in entry.get("files", ())}
    picked = Selection(set())
    changed_by_commit: dict[str, set[str] | None] = {}

    for case in cases:
        was = record.get(case.name)
        if was is None:
            picked.reasons[case.name] = "no record"
            continue
        if was.get("argv") != list(case.argv):
            picked.reasons[case.name] = "arguments changed"
            continue
        moved = [p for p, h in _hashes(case.inputs).items() if was.get("inputs", {}).get(p) != h]
        if moved:
            picked.reasons[case.name] = f"input {moved[0]} changed"
            continue

        commit = was.get("commit", "")
        if commit not in changed_by_commit:
            changed_by_commit[commit] = changed_since(commit)
        changed = changed_by_commit[commit]
        if changed is None:
            picked.reasons[case.name] = f"commit {commit} not found"
            continue
        why, everything = _placed(changed, was, code_dirs, runner)
        if everything:
            picked.everything = why
            break
        if why:
            picked.reasons[case.name] = why

    if picked.everything:
        picked.reasons = {c.name: picked.everything for c in cases}
    picked.names = set(picked.reasons)
    return picked


def _placed(changed: set[str], was: dict, code_dirs: set[str], runner: set[str]) -> tuple[str, bool]:
    """Why a case with record `was` runs ("" when no change moves it), and whether every case
    runs.  `code_dirs`: the directories any case executed code in."""
    files = set(was.get("files", ()))
    inputs = set(was.get("inputs", {}))
    own_dirs = {_parent(f) for f in files}

    for path in sorted(changed):
        if path in runner or path == _LIVE_SETTINGS:
            return f"{path} changed", True
        if path in files:
            return f"{path} changed", False
        if path in inputs or path.startswith(_MOVES_NONE + _INPUT_ONLY) or path.endswith((_NOTES, _PYTHON)):
            continue
        if _parent(path) in own_dirs:
            return f"{path} changed (beside code it ran)", False
        if _parent(path) not in code_dirs:
            return f"{path} changed (no rule places it)", True
    return "", False


def changed_since(commit: str) -> set[str] | None:
    """Files that differ from `commit` in the working tree (staged or not), and untracked ones;
    None when the commit is not in the repository."""
    base = commit.removesuffix("-dirty")
    diff = _git("diff", "--name-only", "--relative", base, "--")
    if diff is None:
        return None
    untracked = _git("ls-files", "--others", "--exclude-standard") or ""
    return {line for line in (diff + "\n" + untracked).splitlines() if line}


def head_commit() -> str:
    """HEAD's short hash, -dirty with uncommitted changes to tracked files."""
    head = (_git("rev-parse", "--short", "HEAD") or "unknown").strip()
    return f"{head}-dirty" if (_git("status", "--porcelain", "--untracked-files=no") or "").strip() else head


def report(selection: Selection, total: int) -> None:
    """One line: how many cases run and why the rest do not."""
    if selection.everything:
        print(f"  all {total} cases: {selection.everything}")
        return
    print(f"  {len(selection.names)} of {total} cases moved by changes since their baselines (--all runs every one)")
    for name, why in sorted(selection.reasons.items()):
        print(f"    {name}: {why}")


# --- helpers ---------------------------------------------------------------------------------

def _git(*args: str) -> str | None:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False)
    return r.stdout if r.returncode == 0 else None


def _hashes(paths: Iterable[Path]) -> dict[str, str]:
    """Each input by its project path; "missing" for one not there."""
    out = {}
    for p in paths:
        key = _relative(str(p))
        out[key] = hashlib.sha256(p.read_bytes()).hexdigest()[:_HASH_CHARS] if p.exists() else "missing"
    return out


def _parent(path: str) -> str:
    return Path(path).parent.as_posix()


def _inside(path: str) -> bool:
    return Path(path).resolve().is_relative_to(ROOT)


def _relative(path: str) -> str:
    p = Path(path)
    return (p.resolve().relative_to(ROOT) if p.is_absolute() else p).as_posix()
