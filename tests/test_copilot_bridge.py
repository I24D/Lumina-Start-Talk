"""Unit tests for the Copilot bridge: the question is handed over at once, the
answer is spoken when it arrives, and a finished reply is noticed quickly.

Run from the project root: python -m unittest tests.test_copilot_bridge
"""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from plugins import copilot_bridge as bridge


class _Info:
    def __init__(self, control_type, name):
        self.control_type = control_type
        self._name = name

    @property
    def name(self):
        return self._name() if callable(self._name) else self._name


class _Element:
    """Just enough of a pywinauto UIA element for the bridge to walk."""

    def __init__(self, control_type, name="", children=(), visible=lambda: True):
        self.element_info = _Info(control_type, name)
        self._children = list(children)
        self._parent = None
        self.visible = visible
        for child in self._children:
            child._parent = self

    def children(self):
        return [child for child in self._children if child.visible()]

    def parent(self):
        return self._parent


def _chat(states, step):
    """A Copilot window whose newest reply moves through `states` over time.

    Each state is (text, toolbar shown), one every `step` seconds."""
    began = time.monotonic()

    def state():
        return states[min(len(states) - 1, int((time.monotonic() - began) / step))]

    previous = _Element("Group", "Copilot said: Hola", [
        _Element("Text", "Copilot said:"), _Element("Text", "Hola"), _Element("ToolBar"),
    ])
    reply = _Element("Group", lambda: "Copilot said: " + state()[0], [
        _Element("Text", "Copilot said:"),
        _Element("Text", lambda: state()[0]),
        _Element("ToolBar", visible=lambda: state()[1]),
    ])
    conversation = _Element("Group", "Chat conversation", [
        _Element("Group", "", [_Element("Group", "You said: hola")]),
        _Element("Group", "", [previous]),
        _Element("Group", "", [_Element("Group", "You said: capital")]),
        _Element("Group", "", [reply]),
    ])
    return _Element("Window", "Copilot", [_Element("Document", "Microsoft Copilot", [conversation])])


class AwaitReplyTests(unittest.TestCase):
    def test_a_reply_with_its_toolbar_is_taken_as_soon_as_it_holds_still(self):
        window = _chat([
            ("Putting it together…", False),
            ("La capital", False),
            ("La capital de Australia es Canberra.", False),
            ("La capital de Australia es Canberra.", True),
        ], step=0.1)
        with mock.patch.object(bridge, "_POLL_SECONDS", 0.02), \
             mock.patch.object(bridge, "_FINISHED_STEADY_SECONDS", 0.05), \
             mock.patch.object(bridge, "_SETTLE_SECONDS", 5.0):
            started = time.monotonic()
            answer = bridge._await_reply(window, "Hola", 10)
        self.assertEqual(answer, "La capital de Australia es Canberra.")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_without_a_toolbar_it_still_waits_for_the_reply_to_settle(self):
        window = _chat([("La capital", False), ("La capital es Canberra.", False)], step=0.1)
        with mock.patch.object(bridge, "_POLL_SECONDS", 0.02), \
             mock.patch.object(bridge, "_FINISHED_STEADY_SECONDS", 0.05), \
             mock.patch.object(bridge, "_SETTLE_SECONDS", 0.4):
            started = time.monotonic()
            answer = bridge._await_reply(window, "Hola", 10)
        self.assertEqual(answer, "La capital es Canberra.")
        self.assertGreaterEqual(time.monotonic() - started, 0.5)


class AskTests(unittest.TestCase):
    def setUp(self):
        self.said = []
        self.player = SimpleNamespace(request_say=self.said.append, write_log=lambda *_: None)

    def wait_for_speech(self):
        deadline = time.monotonic() + 3.0
        while not self.said and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_the_question_returns_at_once_and_the_answer_is_spoken_when_it_arrives(self):
        release = threading.Event()

        def answer_later(window, baseline, timeout):
            release.wait(5)
            return "La capital de Australia es Canberra."

        with mock.patch.object(bridge, "_window", return_value=object()), \
             mock.patch.object(bridge, "_composer", return_value=object()), \
             mock.patch.object(bridge, "_reply_turns", return_value=[]), \
             mock.patch.object(bridge, "_send") as send, \
             mock.patch.object(bridge, "_await_reply", side_effect=answer_later):
            started = time.monotonic()
            reply = bridge._act_ask("¿Cuál es la capital de Australia?", 90, self.player)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertIn("Copilot", reply)
            self.assertIn("still waiting", bridge._act_ask("Otra pregunta", 90, self.player))
            self.assertEqual(self.said, [])
            release.set()
            self.wait_for_speech()

        send.assert_called_once()
        self.assertEqual(len(self.said), 1)
        self.assertTrue(self.said[0].startswith("[DELAYED_ANSWER]"))
        self.assertIn("[READ_IN_FULL]\nLa capital de Australia es Canberra.", self.said[0])
        self.assertEqual(bridge._asking, "")

    def test_a_question_that_cannot_be_sent_is_reported(self):
        with mock.patch.object(bridge, "_window", return_value=None):
            bridge._act_ask("¿Hola?", 90, self.player)
            self.wait_for_speech()
        self.assertEqual(len(self.said), 1)
        self.assertIn("would not open", self.said[0])
        self.assertEqual(bridge._asking, "")


if __name__ == "__main__":
    unittest.main()
