"""The import rules in pyproject.toml's [tool.importlinter] (agents.md layering): each layer
imports only those below it, the drivers only through core.source, no driver another.

    python -m pytest tests/test_layers.py -q          (or: lint-imports)
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from importlinter import configuration
    from importlinter.application.use_cases import lint_imports

    configuration.configure()        # what the lint-imports CLI sets up first
except ImportError:                  # a dev dependency: pip install import-linter
    lint_imports = None


@unittest.skipIf(lint_imports is None, "needs import-linter (pip install import-linter)")
class Layers(unittest.TestCase):
    def test_every_contract_is_kept(self):
        cwd = os.getcwd()
        os.chdir(ROOT)
        try:
            kept = lint_imports(config_filename=str(ROOT / "pyproject.toml"), no_logo=True)
        finally:
            os.chdir(cwd)
        self.assertTrue(kept, "an import breaks a layer rule: the report above names it")


if __name__ == "__main__":
    unittest.main()
