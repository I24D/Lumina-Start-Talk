"""Unit tests for the plugin start() hook.

Run from the project root: python -m unittest tests.test_plugin_loader
"""

import pathlib
import sys
import tempfile
import textwrap
import unittest

from core import plugin_loader

_PLUGIN = """
PLUGIN = {{"name": "{name}", "description": "Test plugin."}}
started = []

def run(parameters):
    return "ok"
"""


class StartAllTests(unittest.TestCase):
    def discover(self, **extra_code):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = pathlib.Path(tmp.name)
        for name, code in extra_code.items():
            source = _PLUGIN.format(name=name) + textwrap.dedent(code)
            (folder / f"{name}.py").write_text(source, encoding="utf-8")
            self.addCleanup(sys.modules.pop, f"plugins.{name}", None)
        return plugin_loader.discover_plugins(folder, core_tool_names=set(), logger=lambda _: None)

    def test_start_receives_the_player_once_and_plugins_without_it_are_fine(self):
        registry = self.discover(
            loader_test_watch="def start(player=None):\n    started.append(player)\n",
            loader_test_plain="",
        )
        registry.start_all(player="ui")
        self.assertEqual(sys.modules["plugins.loader_test_watch"].started, ["ui"])
        self.assertTrue(registry.has("loader_test_plain"))

    def test_a_start_that_raises_does_not_stop_the_others(self):
        registry = self.discover(
            loader_test_broken="def start(player=None):\n    raise RuntimeError('boom')\n",
            loader_test_fine="def start(player=None):\n    started.append(player)\n",
        )
        registry.start_all(player="ui")
        self.assertEqual(sys.modules["plugins.loader_test_fine"].started, ["ui"])


if __name__ == "__main__":
    unittest.main()
