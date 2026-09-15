"""Unit tests for how long-term memory reaches the disk.

Run from the project root: python -m unittest tests.test_memory_manager
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory import memory_manager


class MemoryFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "long_term.json"
        self.path.write_text(json.dumps({"notes": "before"}), encoding="utf-8")
        patcher = mock.patch.object(memory_manager, "MEMORY_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_write_that_dies_halfway_leaves_the_previous_memory(self):
        real_write_text = Path.write_text

        def dies_halfway(path, text, encoding=None):
            real_write_text(path, text[:5], encoding=encoding)
            raise OSError("disk full")

        with mock.patch.object(Path, "write_text", dies_halfway):
            with self.assertRaises(OSError):
                memory_manager._write_local_memory({"notes": "after"})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"notes": "before"})

    def test_a_write_replaces_the_file_and_leaves_nothing_beside_it(self):
        memory_manager._write_local_memory({"notes": "after"})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"notes": "after"})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["long_term.json"])


if __name__ == "__main__":
    unittest.main()
