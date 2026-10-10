"""Shared files (core/files.py): written whole or not at all.

    python -m pytest tests/core/test_files.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.files import write_atomic, write_shared


class SharedFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_shared_file_is_replaced_only_when_its_bytes_change(self):
        path = self.dir / "dac81.raw"
        write_shared(path, b"\x01\x02")
        before = path.stat().st_mtime_ns
        write_shared(path, b"\x01\x02")
        self.assertEqual(path.stat().st_mtime_ns, before)        # same bytes: not written
        write_shared(path, b"\x03")
        self.assertEqual(path.read_bytes(), b"\x03")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["dac81.raw"])

    def test_a_failed_write_leaves_the_old_file_and_no_temp(self):
        path = self.dir / "dac81.raw"
        path.write_bytes(b"old")

        def fail(tmp: Path) -> None:
            tmp.write_bytes(b"pa")
            raise OSError("disk full")

        with self.assertRaises(OSError):
            write_atomic(path, fail)
        self.assertEqual(path.read_bytes(), b"old")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["dac81.raw"])


if __name__ == "__main__":
    unittest.main()
