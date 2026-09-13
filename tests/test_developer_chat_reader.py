"""Unit tests for announcing finished Codex and Claude Code answers. No VS Code needed.

Run from the project root: python -m unittest tests.test_developer_chat_reader
"""

import importlib.util
import json
import pathlib
import sys
import tempfile
import time
import unittest
from collections import deque
from datetime import datetime, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "developer_chat_reader_under_test", ROOT / "plugins" / "developer_chat_reader.py"
)
reader = importlib.util.module_from_spec(_spec)
# @dataclass looks its module up in sys.modules, as the plugin loader arranges.
sys.modules[_spec.name] = reader
_spec.loader.exec_module(reader)


def _stamp(offset_seconds: float = 0.0) -> str:
    moment = datetime.fromtimestamp(time.time() + offset_seconds, tz=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def _claude(text, reason="end_turn", message_id="msg_1", when=None, blocks=None, **extra):
    row = {
        "type": "assistant",
        "timestamp": when or _stamp(),
        "message": {
            "id": message_id,
            "role": "assistant",
            "stop_reason": reason,
            "content": blocks if blocks is not None else [{"type": "text", "text": text}],
        },
    }
    row.update(extra)
    return row


def _codex(text, phase="final_answer"):
    return {
        "type": "response_item",
        "timestamp": _stamp(),
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": phase,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _codex_meta(source="vscode", thread_source="user"):
    return {"type": "session_meta", "payload": {"source": source, "thread_source": thread_source}}


class FinishedAnswerWatcherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        self.claude = home / "claude"
        self.codex = home / "codex"
        self.claude.mkdir()
        self.codex.mkdir()
        for name, root in (("_claude_roots", self.claude), ("_codex_roots", self.codex)):
            patcher = mock.patch.object(reader, name, return_value=(root,))
            patcher.start()
            self.addCleanup(patcher.stop)

        self.started = time.time() - 1
        self.tails = {"claude": reader._TranscriptTail(), "codex": reader._TranscriptTail()}
        self.announced = deque(maxlen=50)
        self.codex_sessions = {}

    def look(self):
        return reader._finished_since_last_look(
            self.tails, self.started, self.announced, self.codex_sessions
        )

    def write(self, path, *rows):
        with path.open("a", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def test_answers_already_on_disk_are_not_news(self):
        chat = self.claude / "chat.jsonl"
        self.write(chat, _claude("antigua", message_id="msg_old"))
        self.assertEqual(self.look(), [])

        self.write(chat, _claude("nueva", message_id="msg_new"))
        self.assertEqual(self.look(), [("Claude Code", "nueva")])
        self.assertEqual(self.look(), [])

    def test_only_the_final_answer_of_a_turn_is_announced(self):
        chat = self.claude / "chat.jsonl"
        chat.touch()
        self.look()

        self.write(
            chat,
            _claude("Déjame revisar", reason="tool_use", message_id="msg_a"),
            _claude("", message_id="msg_b", blocks=[{"type": "thinking", "thinking": "..."}]),
            _claude("de un subagente", message_id="msg_c", isSidechain=True),
            _claude("Listo, quedó hecho.", message_id="msg_d"),
        )
        self.assertEqual(self.look(), [("Claude Code", "Listo, quedó hecho.")])

    def test_a_line_still_being_written_waits_for_its_newline(self):
        chat = self.claude / "chat.jsonl"
        chat.touch()
        self.look()

        line = json.dumps(_claude("completa"))
        with chat.open("a", encoding="utf-8") as stream:
            stream.write(line[:25])
        self.assertEqual(self.look(), [])

        with chat.open("a", encoding="utf-8") as stream:
            stream.write(line[25:] + "\n")
        self.assertEqual(self.look(), [("Claude Code", "completa")])

    def test_resumed_history_and_rewritten_answers_are_not_repeated(self):
        self.look()
        resumed = self.claude / "resumed.jsonl"
        self.write(
            resumed,
            _claude("de ayer", message_id="msg_yesterday", when=_stamp(-86_400)),
            _claude("de ahora", message_id="msg_now"),
        )
        self.assertEqual(self.look(), [("Claude Code", "de ahora")])

        self.write(resumed, _claude("de ahora", message_id="msg_now"))
        self.assertEqual(self.look(), [])

    def test_codex_announces_only_the_users_own_chats(self):
        self.look()
        self.write(
            self.codex / "chat.jsonl",
            _codex_meta(),
            _codex("Voy a inspeccionar", phase="commentary"),
            _codex("Terminé la tarea."),
        )
        self.write(
            self.codex / "guardian.jsonl",
            _codex_meta(source={"subagent": {"other": "guardian"}}, thread_source="subagent"),
            _codex("approved"),
        )
        self.write(self.codex / "exec.jsonl", _codex_meta(source="exec"), _codex("ok"))

        self.assertEqual(self.look(), [("Codex", "Terminé la tarea.")])

    def test_announcement_asks_for_an_in_depth_summary(self):
        player = mock.Mock()
        reader._announce(player, "Claude Code", "## Hecho\nMira [main.py](main.py#L1).")

        instruction = player.request_announce.call_args.args[0]
        self.assertTrue(instruction.startswith("[CHAT_FINISHED] Claude Code"))
        self.assertIn("[ANSWER_IN_DEPTH]\nHecho\nMira main.py.", instruction)


if __name__ == "__main__":
    unittest.main()
