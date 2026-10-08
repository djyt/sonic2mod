"""The render cache (core/render_cache.py): what goes in comes back, and nothing stale does.
Shared files (core/files.py): written whole or not at all.

    python -m pytest tests -q
"""

from __future__ import annotations

import array
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.files import write_atomic, write_shared
from core.render_cache import RenderCache, code_salt


class RoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_int_array_comes_back_as_stored(self):
        cache = RenderCache(self.dir, "fm", "salt")
        cache.put(cache.key(("a",)), array.array("i", [-5, 0, 7]), 16574)
        samples, rate = cache.get(cache.key(("a",)))
        self.assertEqual((samples.typecode, list(samples), rate), ("i", [-5, 0, 7], 16574))

    def test_a_float_list_keeps_every_bit(self):
        # PSG tones come out of the resampler as floats: a hit must be the same doubles
        cache = RenderCache(self.dir, "psg", "salt")
        values = [0.1, -1 / 3, 2.5e-17]
        cache.put(cache.key(("t",)), values, 8287)
        self.assertEqual(list(cache.get(cache.key(("t",)))[0]), values)

    def test_an_int_list_stays_ints(self):
        cache = RenderCache(self.dir, "psg", "salt")
        cache.put(cache.key(("n",)), [3, -4], 8287)
        samples, _ = cache.get(cache.key(("n",)))
        self.assertEqual((samples.typecode, list(samples)), ("i", [3, -4]))

    def test_through_renders_once(self):
        cache = RenderCache(self.dir, "fm", "salt")
        calls = []

        def render():
            calls.append(1)
            return array.array("i", [1, 2]), 100

        for _ in range(3):
            self.assertEqual(list(cache.through(("x", 1.5), render)[0]), [1, 2])
        self.assertEqual((len(calls), cache.hits, cache.misses), (1, 2, 1))

    def test_other_inputs_miss(self):
        cache = RenderCache(self.dir, "fm", "salt")
        cache.put(cache.key(("x", 1.5)), array.array("i", [1]), 100)
        self.assertIsNone(cache.get(cache.key(("x", 1.25))))


class Stale(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_other_code_neither_hits_nor_stays(self):
        old = RenderCache(self.dir, "fm", "old code")
        old.put(old.key(("x",)), array.array("i", [1]), 100)
        new = RenderCache(self.dir, "fm", "new code")
        self.assertIsNone(new.get(new.key(("x",))))
        self.assertFalse((self.dir / "fm" / "old code").exists())

    def test_the_other_chip_is_left_alone(self):
        fm = RenderCache(self.dir, "fm", "a")
        fm.put(fm.key(("x",)), array.array("i", [1]), 100)
        RenderCache(self.dir, "psg", "b")
        self.assertIsNotNone(fm.get(fm.key(("x",))))

    def test_a_damaged_file_is_a_miss(self):
        cache = RenderCache(self.dir, "fm", "salt")
        key = cache.key(("x",))
        cache.put(key, array.array("i", [1]), 100)
        next(self.dir.rglob("*.bin")).write_bytes(b"not zlib")
        self.assertIsNone(cache.get(key))
        self.assertEqual(cache.misses, 1)

    def test_a_changed_file_changes_the_salt(self):
        f = self.dir / "voice.py"
        f.write_text("a = 1")
        before = code_salt([f])
        f.write_text("a = 2")
        self.assertNotEqual(before, code_salt([f]))


class Off(unittest.TestCase):
    def test_off_stores_nothing_and_counts_nothing(self):
        cache = RenderCache(None, "fm", "")
        cache.put(cache.key(("x",)), array.array("i", [1]), 100)
        self.assertIsNone(cache.get(cache.key(("x",))))
        self.assertEqual((cache.enabled, cache.hits, cache.misses), (False, 0, 0))


class Files(unittest.TestCase):
    """Whole files (VGMPlay's reference WAVs) under a key."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_file_comes_back_byte_for_byte(self):
        cache = RenderCache(self.dir / "cache", "vgmplay", "salt")
        src, dest = self.dir / "a.wav", self.dir / "out" / "b.wav"
        src.write_bytes(b"RIFF\x00\x01\x02")
        key = cache.key(("vgz", "ini"))
        self.assertFalse(cache.get_file(key, dest, ".wav"))
        cache.put_file(key, src, ".wav")
        self.assertTrue(cache.get_file(key, dest, ".wav"))
        self.assertEqual(dest.read_bytes(), src.read_bytes())
        self.assertEqual((cache.hits, cache.misses), (1, 1))

    def test_off_never_hits(self):
        cache = RenderCache(None, "vgmplay", "salt")
        src = self.dir / "a.wav"
        src.write_bytes(b"x")
        cache.put_file("k", src, ".wav")
        self.assertFalse(cache.get_file("k", self.dir / "b.wav", ".wav"))

    def test_bytes_come_back_and_a_damaged_file_misses(self):
        cache = RenderCache(self.dir / "cache", "vgm_frames", "salt")
        self.assertIsNone(cache.get_bytes("k", ".frames"))
        cache.put_bytes("k", b"frames", ".frames")
        self.assertEqual(cache.get_bytes("k", ".frames"), b"frames")
        next((self.dir / "cache").rglob("k.frames")).write_bytes(b"not zlib")
        self.assertIsNone(cache.get_bytes("k", ".frames"))


class SharedFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_shared_file_is_replaced_only_when_its_bytes_change(self):
        path = self.dir / "dac81.raw"
        write_shared(path, b"")
        before = path.stat().st_mtime_ns
        write_shared(path, b"")
        self.assertEqual(path.stat().st_mtime_ns, before)        # same bytes: not written
        write_shared(path, b"")
        self.assertEqual(path.read_bytes(), b"")
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


class ReferenceKey(unittest.TestCase):
    def test_the_key_follows_the_log_and_the_ini(self):
        from core.audit import reference_render_key
        base = reference_render_key(b"vgz", "[General]\nMuteMask = 0x7E")
        self.assertEqual(base, reference_render_key(b"vgz", "[General]\nMuteMask = 0x7E"))
        self.assertNotEqual(base, reference_render_key(b"vgz2", "[General]\nMuteMask = 0x7E"))
        self.assertNotEqual(base, reference_render_key(b"vgz", "[General]\nMuteMask = 0x7D"))


if __name__ == "__main__":
    unittest.main()
