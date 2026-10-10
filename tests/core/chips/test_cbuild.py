"""Where a chip device's library comes from (core/chips/cbuild.py): build/ when it can be built,
prebuilt/ on a machine with no compiler.

    python -m pytest tests/core/chips/test_cbuild.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.chips import cbuild


class LibraryPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.source = tmp / "chip.c"
        self.source.write_text("int x;", encoding="utf-8")
        self.lib = cbuild.CLibrary(name="chip", out_dir=tmp / "build", sources=[self.source])
        self.prebuilt = tmp / "prebuilt"
        self.prebuilt.mkdir()
        self.patch = mock.patch.object(cbuild, "PREBUILT_DIR", self.prebuilt)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self._tmp.cleanup()

    def _without_compiler(self):
        return mock.patch.object(cbuild.shutil, "which", return_value=None)     # no gcc, no cl

    def test_no_compiler_takes_the_prebuilt_copy(self):
        (self.prebuilt / self.lib.lib_name).write_bytes(b"lib")
        with self._without_compiler():
            self.assertEqual(self.lib.get_lib_path(), self.prebuilt / self.lib.lib_name)

    def test_no_compiler_and_no_prebuilt_copy_says_so(self):
        with self._without_compiler(), self.assertRaises(RuntimeError):
            self.lib.get_lib_path()

    def test_a_fresh_build_is_used_as_it_is(self):
        self.lib.out_dir.mkdir()
        self.lib.lib_path.write_bytes(b"built")
        (self.prebuilt / self.lib.lib_name).write_bytes(b"lib")
        with self._without_compiler():
            self.assertEqual(self.lib.get_lib_path(), self.lib.lib_path)


if __name__ == "__main__":
    unittest.main()
