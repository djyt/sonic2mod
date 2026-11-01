"""tests/functions.py: which functions a case ran, which a change touched, the index.

    python -m pytest tests/test_functions.py -q
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.functions import decode_index, encode_index, ran_functions, touched_functions
from tests.selection import _placed

_OLD = '''"""A module."""
LIMIT = 4


def play(n):
    """Play n."""
    return n + LIMIT


class Track:
    rate = 2

    def step(self):
        def inner():
            return 1
        return inner() * self.rate
'''


class Touched(unittest.TestCase):
    def test_a_body_changed(self):
        self.assertEqual(touched_functions(_OLD, _OLD.replace("n + LIMIT", "n - LIMIT")), {"play"})

    def test_a_nested_function_moves_its_outer_too(self):
        self.assertEqual(touched_functions(_OLD, _OLD.replace("return 1", "return 2")), {"Track.step", "Track.step.inner"})

    def test_docstrings_comments_and_new_functions_touch_nothing(self):
        new = _OLD.replace('"""Play n."""', '"""Play n, louder."""').replace("LIMIT = 4", "LIMIT = 4  # frames")
        self.assertEqual(touched_functions(_OLD, new + "\n\ndef helper():\n    return 0\n"), set())

    def test_code_outside_a_function_is_none(self):
        self.assertIsNone(touched_functions(_OLD, _OLD.replace("LIMIT = 4", "LIMIT = 5")))
        self.assertIsNone(touched_functions(_OLD, _OLD.replace("rate = 2", "rate = 3")))
        self.assertIsNone(touched_functions(_OLD, None))
        self.assertIsNone(touched_functions(_OLD, "def broken(:\n"))


class Ran(unittest.TestCase):
    def test_a_line_belongs_to_the_innermost_function_holding_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.py"
            path.write_text(_OLD, encoding="utf-8")
            # 2 LIMIT, 7 play's return, 13 step's def line (the class body), 15 inner's return, 16 step's return
            self.assertEqual(ran_functions(path, [2, 7, 13, 15, 16]), ["Track.step", "Track.step.inner", "play"])


class Index(unittest.TestCase):
    def test_round_trip(self):
        ran = {"b": {"core/x.py": {"f", "g"}}, "a": {"core/x.py": {"f"}, "core/y.py": {"h"}}}
        self.assertEqual(decode_index(encode_index(ran)), ran)


_CASE = {"files": ["convert.py", "core/smps/code.py"]}


class Placement(unittest.TestCase):
    def _moves(self, touched, ran):
        return _placed({"core/smps/code.py"}, _CASE, {"", "core/smps"}, set(), lambda _path: touched, ran)

    def test_only_cases_that_ran_a_touched_function(self):
        self.assertEqual(self._moves({"_Walk.note"}, {"core/smps/code.py": {"_Walk.note"}}),
                         ("core/smps/code.py: _Walk.note changed", False))
        self.assertEqual(self._moves({"_Walk.note"}, {"core/smps/code.py": {"_Walk.rest"}}), ("", False))

    def test_outside_a_function_every_case_that_loaded_it(self):
        self.assertEqual(self._moves(None, {}), ("core/smps/code.py changed outside its functions", False))

    def test_no_function_record_falls_back_to_the_file(self):
        self.assertEqual(self._moves({"_Walk.note"}, None), ("core/smps/code.py changed", False))


if __name__ == "__main__":
    unittest.main()
