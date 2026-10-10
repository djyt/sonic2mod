"""tests/selection.py's placement rules: which changed file moves which case.

    python -m pytest tests/test_selection_units.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.selection import _placed

_GA = {"files": ["convert.py", "core/smps/code.py", "core/drivers/smpsz80/type0fm/variant.py", "ym2612/renderer.py"],
       "inputs": {"configs/golden_axe/89_the_battle.yaml": "x"}}
_SONIC = {"files": ["convert.py", "core/smps/code.py", "core/drivers/smps68k/sonic1/variant.py"]}
_CODE_DIRS = {"", "core/smps", "core/drivers/smpsz80/type0fm", "core/drivers/smps68k/sonic1", "ym2612"}
_RUNNER = {"tests/regression.py"}


def _moves(case: dict, *changed: str) -> tuple[str, bool]:
    return _placed(set(changed), case, _CODE_DIRS, _RUNNER)


class Placement(unittest.TestCase):
    def test_a_driver_moves_only_its_cases(self):
        self.assertTrue(_moves(_GA, "core/drivers/smpsz80/type0fm/variant.py")[0])
        self.assertEqual(_moves(_SONIC, "core/drivers/smpsz80/type0fm/variant.py"), ("", False))

    def test_shared_code_moves_both(self):
        self.assertTrue(_moves(_GA, "core/smps/code.py")[0])
        self.assertTrue(_moves(_SONIC, "core/smps/code.py")[0])

    def test_a_c_source_moves_the_cases_that_ran_beside_it(self):
        self.assertTrue(_moves(_GA, "ym2612/ym3438_batch.c")[0])
        self.assertEqual(_moves(_SONIC, "ym2612/ym3438_batch.c"), ("", False))

    def test_what_moves_nothing(self):
        for path in ("docs/pipeline.md", "core/new_module.py", "configs/moonwalker/81_smooth_criminal.yaml",
                     "tests/baselines/ghz_baseline.mod", "tests/test_rom_units.py", "README.md"):
            self.assertEqual(_moves(_SONIC, path), ("", False), path)

    def test_what_moves_everything(self):
        for path in ("tests/regression.py", "configs/settings.yaml", "sfx/tables.bin"):
            self.assertTrue(_moves(_SONIC, path)[1], path)


if __name__ == "__main__":
    unittest.main()
