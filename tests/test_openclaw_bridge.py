"""Unit tests for the OpenClaw bridge's WebSocket plumbing. No Gateway needed.

Run from the project root: python -m unittest tests.test_openclaw_bridge
"""

import importlib.util
import json
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import Future
from contextlib import closing
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "openclaw_bridge_under_test", ROOT / "plugins" / "openclaw_bridge.py"
)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

CRLF = chr(13) + chr(10)


class ReadGatewayTokenTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GATEWAY_AUTH_TOKEN", None)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name)
        self.dotenv = self.home / ".env"
        for patcher in (
            mock.patch.object(bridge, "_project_dotenv_path", return_value=self.dotenv),
            mock.patch.object(bridge.pathlib.Path, "home", return_value=self.home),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_environment_wins_over_dotenv(self):
        self.dotenv.write_text("GATEWAY_AUTH_TOKEN=from-dotenv", encoding="utf-8")
        os.environ["GATEWAY_AUTH_TOKEN"] = "from-env"
        self.assertEqual(bridge._read_gateway_token(), "from-env")

    def test_reads_project_dotenv_with_crlf_quotes_and_export(self):
        self.dotenv.write_text(
            "OTHER=1" + CRLF + 'export GATEWAY_AUTH_TOKEN="abc123"' + CRLF, encoding="utf-8"
        )
        self.assertEqual(bridge._read_gateway_token(), "abc123")

    def test_ignores_keys_that_merely_start_with_the_name(self):
        self.dotenv.write_text("GATEWAY_AUTH_TOKEN_OLD=stale", encoding="utf-8")
        self.assertIsNone(bridge._read_gateway_token())

    def test_returns_none_without_any_source(self):
        self.assertIsNone(bridge._read_gateway_token())


class WsIsOpenTests(unittest.TestCase):
    def test_none_is_closed(self):
        self.assertFalse(bridge._ws_is_open(None))

    @unittest.skipIf(bridge._WsState is None, "websockets without protocol.State")
    def test_uses_state_and_never_the_removed_attributes(self):
        class ModernConnection:
            def __init__(self, state):
                self.state = state

            def __getattr__(self, name):
                raise AttributeError(name)

        self.assertTrue(bridge._ws_is_open(ModernConnection(bridge._WsState.OPEN)))
        self.assertFalse(bridge._ws_is_open(ModernConnection(bridge._WsState.CLOSED)))


class DispatchTests(unittest.TestCase):
    def setUp(self):
        bridge._ws_pending.clear()
        bridge._ws_runs.clear()
        self.addCleanup(bridge._ws_pending.clear)
        self.addCleanup(bridge._ws_runs.clear)

    def test_chat_send_receipt_is_not_the_answer(self):
        receipt, run = Future(), Future()
        bridge._ws_pending["req-1"] = receipt
        bridge._ws_runs["run-1"] = run
        bridge._dispatch_ws_message(
            {"type": "res", "id": "req-1", "ok": True, "payload": {"runId": "run-1", "status": "started"}}
        )
        self.assertTrue(receipt.done())
        self.assertFalse(run.done())

    def test_only_the_terminal_chat_event_resolves_its_run(self):
        run = Future()
        bridge._ws_runs["run-1"] = run
        bridge._dispatch_ws_message(
            {"type": "event", "event": "chat", "payload": {"runId": "run-1", "state": "delta"}}
        )
        self.assertFalse(run.done())
        final = {
            "runId": "run-1",
            "state": "final",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hola"}]},
        }
        bridge._dispatch_ws_message({"type": "event", "event": "chat", "payload": final})
        self.assertEqual(run.result(timeout=1), final)

    def test_other_runs_and_internal_event_names_are_ignored(self):
        run = Future()
        bridge._ws_runs["run-1"] = run
        bridge._dispatch_ws_message(
            {"type": "event", "event": "chat", "payload": {"runId": "run-2", "state": "final"}}
        )
        bridge._dispatch_ws_message({"type": "event", "event": "run.completed", "payload": {"runId": "run-1"}})
        self.assertFalse(run.done())

    def test_a_lost_connection_fails_every_waiter(self):
        receipt, run = Future(), Future()
        bridge._ws_pending["req-1"] = receipt
        bridge._ws_runs["run-1"] = run
        bridge._ws_fail_waiters("gone")
        for future in (receipt, run):
            with self.assertRaises(ConnectionError):
                future.result(timeout=1)


class AnswerFromChatEventTests(unittest.TestCase):
    def test_final_text_parts(self):
        payload = {"state": "final", "message": {"content": [{"type": "text", "text": "hola"}]}}
        self.assertEqual(bridge._answer_from_chat_event(payload), ("hola", "", True))

    def test_final_string_content(self):
        payload = {"state": "final", "message": {"content": " hola "}}
        self.assertEqual(bridge._answer_from_chat_event(payload), ("hola", "", True))

    def test_error_event(self):
        payload = {"state": "error", "errorMessage": "boom"}
        self.assertEqual(bridge._answer_from_chat_event(payload), ("", "boom", True))

    def test_aborted_without_text_still_counts_as_started(self):
        answer, error, started = bridge._answer_from_chat_event({"state": "aborted"})
        self.assertEqual(answer, "")
        self.assertTrue(error)
        self.assertTrue(started)


class WaitForAnswerTests(unittest.TestCase):
    def test_no_cli_retry_once_the_turn_may_be_running(self):
        with (
            mock.patch.object(bridge, "_ensure_connection", return_value=(True, "")),
            mock.patch.object(bridge, "_transport", "websocket"),
            mock.patch.object(bridge, "_ws_send_message", return_value=("", "still working", True)),
            mock.patch.object(bridge, "_agent_answer") as cli,
        ):
            self.assertEqual(bridge._wait_for_answer("q", 30), ("", "still working"))
        cli.assert_not_called()

    def test_cli_fallback_when_the_gateway_rejected_the_question(self):
        with (
            mock.patch.object(bridge, "_ensure_connection", return_value=(True, "")),
            mock.patch.object(bridge, "_transport", "websocket"),
            mock.patch.object(bridge, "_ws_send_message", return_value=("", "rejected", False)),
            mock.patch.object(bridge, "_gateway_health", return_value=(True, "")),
            mock.patch.object(bridge, "_agent_answer", return_value=("from cli", "")) as cli,
        ):
            self.assertEqual(bridge._wait_for_answer("q", 30), ("from cli", ""))
        cli.assert_called_once()


class ChatWatcherTests(unittest.TestCase):
    """The watcher that announces answers from OpenClaw's own chat."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = pathlib.Path(tmp.name) / "openclaw-agent.sqlite"
        with closing(sqlite3.connect(self.store)) as db:
            db.executescript(
                """
                create table transcript_events (
                    session_id text, seq integer, event_json text, created_at integer);
                create table session_windows (session_id text primary key, session_key text);
                create table conversations (conversation_id text, channel text);
                create table session_conversations (session_id text, conversation_id text);
                insert into conversations values ('contact', 'telegram');
                insert into session_conversations values ('telegram', 'contact');
                """
            )
            db.executemany(
                "insert into session_windows values (?, ?)",
                [
                    ("chat", "agent:main:main"),
                    ("bridge", bridge._SESSION_SUBSCRIPTION_KEY),
                    ("cron", "agent:main:cron:daily-news"),
                    ("telegram", "agent:main:telegram:direct:1"),
                ],
            )
            db.commit()

        for patcher in (
            mock.patch.object(bridge, "_session_store", return_value=self.store),
            mock.patch.object(bridge, "_job_question", ""),
            mock.patch.object(bridge, "_last_answer", ""),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        bridge._bridge_run_ids.clear()
        self.addCleanup(bridge._bridge_run_ids.clear)

        self.started = time.time() - 1
        self.seq = 0
        self.after, _ = None, None
        _, self.after = bridge._finished_since(None, self.started)

    def add(self, session, text, stop="stop", when_ms=None, **message):
        self.seq += 1
        now_ms = int(time.time() * 1000)
        body = {
            "role": "assistant",
            "stopReason": stop,
            "timestamp": when_ms or now_ms,
            "content": [{"type": "text", "text": text}],
            **message,
        }
        with closing(sqlite3.connect(self.store)) as db:
            db.execute(
                "insert into transcript_events values (?, ?, ?, ?)",
                (session, self.seq, json.dumps({"type": "message", "message": body}), now_ms),
            )
            db.commit()

    def look(self):
        answers, self.after = bridge._finished_since(self.after, self.started)
        return answers

    def test_answers_already_in_the_store_are_not_news(self):
        self.add("chat", "vieja")
        _, after = bridge._finished_since(None, self.started)
        self.after = after
        self.add("chat", "nueva")
        self.assertEqual(self.look(), ["nueva"])
        self.assertEqual(self.look(), [])

    def test_contacts_scheduled_jobs_voice_and_tool_steps_are_not_announced(self):
        self.add("telegram", "Hola, Dal")
        self.add("cron", "Noticias del día")
        self.add("chat", "dicho por la voz", api="realtime")
        self.add("chat", "buscando…", stop="toolUse")
        self.add("chat", "de antes de arrancar", when_ms=int((self.started - 3600) * 1000))
        self.add("chat", "Ya está listo.")
        self.assertEqual(self.look(), ["Ya está listo."])

    def test_questions_the_bridge_sent_are_not_announced_twice(self):
        bridge._bridge_run_ids.append("run-1")
        self.add("bridge", "respuesta por WebSocket", __openclaw={"runId": "run-1"})
        self.assertEqual(self.look(), [])

        with mock.patch.object(bridge, "_job_question", "pregunta pendiente"):
            self.add("bridge", "respuesta por la CLI")
            self.add("chat", "respuesta en su chat")
            self.assertEqual(self.look(), ["respuesta en su chat"])

        with mock.patch.object(bridge, "_last_answer", "ya  leída\n"):
            self.add("bridge", "ya leída")
            self.assertEqual(self.look(), [])

    def test_announcement_asks_for_an_in_depth_summary_and_can_be_repeated(self):
        player = mock.Mock()
        bridge._announce_chat_answer("Terminé.", player)
        instruction = player.request_announce.call_args.args[0]
        self.assertTrue(instruction.startswith("[CHAT_FINISHED] OpenClaw"))
        self.assertIn("[ANSWER_IN_DEPTH]\nTerminé.", instruction)
        self.assertEqual(bridge._last_answer, "Terminé.")


if __name__ == "__main__":
    unittest.main()
