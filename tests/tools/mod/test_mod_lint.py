"""mod_lint's cuts (tools/mod/mod_lint.py): a C00 that silences nothing is a redundant_cut, one that
ends a sound is not.

    python -m pytest tests/tools/mod/test_mod_lint.py -q
"""

from __future__ import annotations

import unittest

from core.mod import ModFile, ModNote
from tools.mod.mod_lint import lint_mod


def _mod() -> bytes:
    """Column 1: a note at row 0 cut at row 2, then a rest's C00 again at row 4; column 2: a C00
    before anything played (row 1); the song ends with a jump back to its own pattern."""
    mod = ModFile(4)
    s = mod.samples[0]
    s.data, s.length = bytes(64), 32
    s.repeat, s.repeat_length = 0, 32              # looped: plays until cut
    s.set_volume(64)
    mod.put_note(0, 0, 0, ModNote.C2, 1)
    mod.put_effect(0, 2, 0, 0xC, 0)
    mod.put_effect(0, 4, 0, 0xC, 0)
    mod.put_effect(0, 1, 1, 0xC, 0)
    mod.put_effect(0, 63, 2, 0xB, 0)
    return bytes(mod.get_bytes())


class Cuts(unittest.TestCase):
    def test_a_cut_on_a_silent_channel_is_redundant(self):
        found = {(i["row"], i["channel"]) for i in lint_mod(_mod()) if i["type"] == "redundant_cut"}
        self.assertEqual(found, {(4, 0), (1, 1)})       # row 2's cut ends the note: not reported


if __name__ == "__main__":
    unittest.main()
