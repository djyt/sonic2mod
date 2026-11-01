"""Claude Code PostToolUse hook (.claude/settings.json): ruff on the Python file just edited.

Its findings go back to Claude at once (exit 2), so a rule is fixed where it was broken, not at commit.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
_FEEDBACK = 2                       # Claude Code: exit 2 hands stderr to the model


def main() -> int:
    event = json.load(sys.stdin)
    path = Path(event.get("tool_input", {}).get("file_path", ""))

    # Only this project's Python: a scratch file elsewhere has no rules to keep
    if path.suffix != ".py" or not path.resolve().is_relative_to(ROOT):
        return 0

    # --force-exclude: a file the project excludes (reference/, gitignored) stays unchecked
    result = subprocess.run([sys.executable, "-m", "ruff", "check", "--force-exclude", "--quiet", str(path)],
                            cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return 0
    sys.stderr.write(result.stdout + result.stderr)
    return _FEEDBACK


if __name__ == "__main__":
    sys.exit(main())
