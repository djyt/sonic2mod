"""The reference render's cache key (core/audit/render.py): the VGZ and VGMPlay.ini it was made from.

    python -m pytest tests/core/audit/test_render.py -q
"""

from __future__ import annotations

import unittest


class ReferenceKey(unittest.TestCase):
    def test_the_key_follows_the_log_and_the_ini(self):
        from core.audit import reference_render_key
        base = reference_render_key(b"vgz", "[General]\nMuteMask = 0x7E")
        self.assertEqual(base, reference_render_key(b"vgz", "[General]\nMuteMask = 0x7E"))
        self.assertNotEqual(base, reference_render_key(b"vgz2", "[General]\nMuteMask = 0x7E"))
        self.assertNotEqual(base, reference_render_key(b"vgz", "[General]\nMuteMask = 0x7D"))
