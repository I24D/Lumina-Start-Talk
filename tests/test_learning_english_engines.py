"""Local engines, Gemma 4 on Ollama Cloud, and voice turns kept in Supabase."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.server import DashboardServer
from learning_english import engines
from learning_english import service as service_module
from learning_english.ollama_cloud import DEFAULT_MODEL, OllamaCloud, OllamaCloudError
from learning_english.providers import LanguageToolProvider
from learning_english.recordings import BUCKET, TABLE, VoiceRecordings, encode_audio
from learning_english.service import LearningEnglishService
from learning_english.store import LearningProgressStore
from memory.supabase_store import Credentials


class _Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class _NoGrammar:
    def status(self):
        return {"id": "grammar-test", "label": "Test", "state": "optional", "detail": ""}

    def check(self, _text):
        return []


def _analysis(**changes):
    analysis = {
        "assistantText": "Nice!", "speechText": "Nice!", "uiLanguage": "es",
        "exerciseType": "conversation",
        "corrections": [{"original": "I go", "corrected": "I went", "explanation": "Pasado.", "category": "grammar"}],
        "newVocabulary": [], "nextAction": "Sigue.", "lessonProgress": 20,
        "shouldWaitForUser": True, "profileUpdates": {},
    }
    analysis.update(changes)
    return analysis


class _FakeOllama:
    configured = True
    model = DEFAULT_MODEL

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = 0

    def chat_json(self, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


class _GeminiModels:
    def __init__(self):
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        return type("Response", (), {"text": json.dumps(_analysis(nextAction="Gemini"))})()


def _service(ollama, models):
    client = type("Client", (), {"models": models})()
    return LearningEnglishService(
        api_key_loader=lambda: "key", client_factory=lambda _key: client,
        ollama=ollama, grammar_provider=_NoGrammar(),
        # Never the user's real OpenAI key: selecting OpenAI would put it first.
        openai_key_loader=lambda: None,
    )


class OllamaCloudTests(unittest.TestCase):
    def test_settings_come_from_the_env_file(self):
        names = ("OLLAMA_CLOUD_ENABLED", "OLLAMA_CLOUD_BASE_URL", "OLLAMA_CLOUD_API_KEY", "LUMINA_LEARNING_OLLAMA_MODEL")
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {name: "" for name in names}):
            Path(folder, ".env").write_text(
                "OLLAMA_CLOUD_ENABLED=true\n"
                "OLLAMA_CLOUD_BASE_URL=https://api.ollama.com/api\n"
                "OLLAMA_CLOUD_API_KEY=test-key\n",
                encoding="utf-8",
            )
            cloud = OllamaCloud.from_env(Path(folder))
        self.assertTrue(cloud.configured)
        self.assertEqual(cloud.base_url, "https://api.ollama.com")
        self.assertEqual(cloud.model, "gemma4:31b")
        self.assertFalse(OllamaCloud().configured)

    def test_fences_are_removed_and_the_schema_rides_in_the_prompt(self):
        sent = {}

        def fake_post(url, **kwargs):
            sent.update(url=url, body=kwargs["json"])
            return _Response(200, {"message": {"content": '```json\n{"ok": true}\n```'}})

        cloud = OllamaCloud(api_key="k", enabled=True)
        with patch("learning_english.ollama_cloud.requests.post", side_effect=fake_post):
            text = cloud.chat_json(
                system="Evaluate.", request={"a": 1}, schema={"type": "object", "required": ["ok"]},
                temperature=0.1, timeout=5,
            )
        self.assertEqual(json.loads(text), {"ok": True})
        self.assertIn('"required": ["ok"]', sent["body"]["messages"][0]["content"])
        self.assertEqual(sent["body"]["model"], "gemma4:31b")
        self.assertTrue(sent["url"].endswith("/api/chat"))

    def test_http_errors_carry_the_status_but_never_the_body(self):
        cloud = OllamaCloud(api_key="k", enabled=True)
        with patch(
            "learning_english.ollama_cloud.requests.post",
            return_value=_Response(429, {"error": "private details"}),
        ), self.assertRaises(OllamaCloudError) as caught:
            cloud.chat_json(system="", request={}, schema={}, temperature=0, timeout=5)
        self.assertEqual(caught.exception.code, 429)
        self.assertNotIn("private", str(caught.exception))


class HelperModelChainTests(unittest.TestCase):
    def test_gemma_answers_the_turn_analysis_first(self):
        ollama, models = _FakeOllama(reply=json.dumps(_analysis())), _GeminiModels()
        result = _service(ollama, models).analyze_turn(
            user_text="Yesterday I go home.", assistant_text="Nice!", snapshot={}
        )
        self.assertEqual((ollama.calls, models.calls), (1, []))
        self.assertEqual(result.corrections[0].corrected, "I went")

    def test_an_incomplete_gemma_reply_falls_back_to_gemini(self):
        ollama, models = _FakeOllama(reply=json.dumps({"correction": "I went"})), _GeminiModels()
        result = _service(ollama, models).analyze_turn(user_text="I go.", assistant_text="Nice!", snapshot={})
        self.assertEqual(models.calls, [service_module.MODEL])
        self.assertEqual(result.next_action, "Gemini")

    def test_a_rejected_helper_rests_while_gemini_carries_on(self):
        ollama = _FakeOllama(error=OllamaCloudError("Ollama Cloud HTTP 401", 401))
        models = _GeminiModels()
        service = _service(ollama, models)
        service.analyze_turn(user_text="I go.", assistant_text="Nice!", snapshot={})
        service.analyze_turn(user_text="I go.", assistant_text="Nice!", snapshot={})
        self.assertEqual(ollama.calls, 1)
        self.assertEqual(models.calls, [service_module.MODEL, service_module.MODEL])

    def test_activities_ask_gemini_first_and_gemma_last(self):
        self.assertEqual(service_module.ANALYSIS_CHAIN[0][0], "ollama")
        self.assertEqual(service_module.ACTIVITY_CHAIN[0], ("gemini", service_module.MODEL))
        self.assertEqual(service_module.ACTIVITY_CHAIN[-1][0], "ollama")


_CONFIG = Credentials(url="https://example.supabase.co", key="service-key", schema="public", allow_writes=True)


class _Http:
    def __init__(self, fail_insert=False, rows=None):
        self.calls = []
        self.fail_insert = fail_insert
        self.rows = list(rows or [])

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith(f"/rest/v1/{TABLE}"):
            return _Response(500 if self.fail_insert else 201)
        return _Response(200, {})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        rows, self.rows = self.rows, []
        return _Response(200, rows)

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        return _Response(200, [])


def _recordings(http, config=_CONFIG):
    return VoiceRecordings(
        Path("."), session=http, config_loader=lambda _base: config,
        encoder=lambda pcm, rate: (b"OggS" + pcm[:4], "audio/ogg", "ogg"),
    )


class VoiceRecordingTests(unittest.TestCase):
    def test_a_turn_lands_in_the_private_bucket_with_its_row(self):
        http = _Http()
        recordings = _recordings(http)
        saved = recordings.save(b"\x01\x00" * 16000, 16000, {
            "session_id": "abc-123", "transcript": "I study English", "audience": "kids",
            "pronunciation_score": 88.5, "pronunciation": {"score": 88.5},
        })
        self.assertTrue(saved)
        recordings.flush()
        (_, upload_url, upload), (_, _, insert) = http.calls
        self.assertIn(f"/storage/v1/object/{BUCKET}/", upload_url)
        self.assertEqual(upload["headers"]["Content-Type"], "audio/ogg")
        self.assertEqual(upload["headers"]["x-upsert"], "false")
        row = insert["json"]
        self.assertEqual(row["duration_ms"], 1000)
        self.assertEqual(row["session_id"], "abc123")
        self.assertEqual(row["storage_path"], upload_url.split(f"{BUCKET}/", 1)[1])
        self.assertEqual((row["pronunciation_score"], row["audience"]), (88.5, "kids"))
        self.assertEqual(recordings.saved, 1)

    def test_audio_without_its_row_is_removed(self):
        http = _Http(fail_insert=True)
        recordings = _recordings(http)
        recordings.save(b"\x00\x00" * 1600, 16000, {})
        recordings.flush()
        self.assertEqual([call[0] for call in http.calls], ["POST", "POST", "DELETE"])
        self.assertEqual(recordings.failed, 1)

    def test_nothing_is_queued_when_supabase_writes_are_off(self):
        http = _Http()
        read_only = Credentials(url=_CONFIG.url, key=_CONFIG.key, schema="public", allow_writes=False)
        self.assertFalse(_recordings(http, read_only).save(b"\x00\x00" * 1600, 16000, {}))
        self.assertEqual(http.calls, [])

    def test_delete_all_removes_the_audio_then_the_rows(self):
        http = _Http(rows=[
            {"id": "1", "storage_path": "2026/09/a/x.ogg"},
            {"id": "2", "storage_path": "2026/09/a/y.ogg"},
        ])
        self.assertEqual(_recordings(http).delete_all(), 2)
        steps = [(method, url.rsplit("/", 1)[-1]) for method, url, _ in http.calls]
        self.assertEqual(steps, [("GET", TABLE), ("DELETE", BUCKET), ("DELETE", TABLE), ("GET", TABLE)])
        self.assertEqual(http.calls[1][2]["json"], {"prefixes": ["2026/09/a/x.ogg", "2026/09/a/y.ogg"]})

    def test_without_ffmpeg_a_turn_is_kept_as_wav(self):
        with patch("learning_english.recordings.shutil.which", return_value=None):
            audio, content_type, extension = encode_audio(b"\x00\x00" * 1600, 16000)
        self.assertEqual((content_type, extension), ("audio/wav", "wav"))
        self.assertTrue(audio.startswith(b"RIFF"))

    def test_recordings_are_saved_until_the_student_turns_them_off(self):
        store = LearningProgressStore(loader=lambda: {}, saver=lambda _value: None)
        self.assertTrue(store.snapshot()["privacy"]["save_recordings"])
        store.set_save_recordings(False)
        self.assertFalse(store.snapshot()["privacy"]["save_recordings"])


class EngineTests(unittest.TestCase):
    def test_the_worker_runs_without_its_own_folder_on_sys_path(self):
        command = engines.worker_command(Path("python.exe"), "--warmup")
        self.assertEqual(command[1], "-P")
        self.assertTrue(command[2].endswith("pronounce_worker.py"))
        self.assertEqual(command[3], "--warmup")

    def test_engines_count_as_installed_only_when_unpacked(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            self.assertFalse(engines.LanguageToolServer(home).installed)
            java = home / "java" / "jdk-21-jre" / "bin"
            java.mkdir(parents=True)
            (java / "java.exe").write_bytes(b"")
            self.assertFalse(engines.LanguageToolServer(home).installed)
            tool = home / "languagetool" / "LanguageTool-6.6"
            tool.mkdir(parents=True)
            (tool / "languagetool-server.jar").write_bytes(b"")
            self.assertTrue(engines.LanguageToolServer(home).installed)
            self.assertFalse(engines.PronunciationWorker(home).installed)

    def test_the_pronunciation_worker_speaks_json_lines(self):
        fake_worker = textwrap.dedent("""
            import json, sys
            print(json.dumps({"ready": True}), flush=True)
            for line in sys.stdin:
                request = json.loads(line)
                reply = {"id": request["id"], "result": {"score": 88, "expected": request["expected"]}}
                print(json.dumps(reply), flush=True)
        """)
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder, "fake_worker.py")
            script.write_text(fake_worker, encoding="utf-8")
            audio = Path(folder, "turn.wav")
            audio.write_bytes(b"RIFF")
            worker = engines.PronunciationWorker(Path(folder))
            with patch.object(engines, "pronounce_python", return_value=Path(sys.executable)), \
                    patch.object(engines, "WORKER_SCRIPT", script):
                self.assertTrue(worker.start())
                try:
                    self.assertTrue(worker.ready.wait(30))
                    self.assertEqual(
                        worker.analyze(audio, "I study English"),
                        {"score": 88, "expected": "I study English"},
                    )
                finally:
                    worker.stop()
            self.assertFalse(worker.ready.is_set())

    def test_grammar_checks_wait_for_the_local_server(self):
        provider = LanguageToolProvider(server=engines.LanguageToolServer(Path("no-engines-here")))
        with patch("learning_english.providers.requests.post") as post:
            self.assertEqual(provider.check("She go home."), [])
        post.assert_not_called()
        self.assertEqual(provider.status()["state"], "not-installed")


class VoiceEndpointTests(unittest.TestCase):
    def setUp(self):
        self.server = DashboardServer()
        self.server._tokens.add("test-token")
        self.headers = {"Authorization": "Bearer test-token"}
        self.calls = []
        self.server.set_learning_callbacks(
            voice=lambda pcm, rate, details: self.calls.append((pcm, rate, details))
            or {"pronunciation": None, "saved": True},
        )
        self.client = TestClient(self.server.app)

    def test_a_spoken_turn_reaches_lumina_with_its_transcript(self):
        pcm = b"\x01\x00" * 1600
        response = self.client.post("/api/learning/voice", headers=self.headers, json={
            "pcm_base64": base64.b64encode(pcm).decode("ascii"), "sample_rate": 16000,
            "transcript": "I study English", "tutor_text": "Great!", "expected_text": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["saved"])
        self.assertEqual(self.calls[0][0], pcm)
        self.assertEqual(self.calls[0][2], {"transcript": "I study English", "tutor_text": "Great!", "expected_text": ""})

    def test_unauthenticated_odd_or_oversized_audio_is_refused(self):
        def send(raw, headers=None):
            headers = self.headers if headers is None else headers
            return self.client.post("/api/learning/voice", headers=headers, json={
                "pcm_base64": base64.b64encode(raw).decode("ascii"), "sample_rate": 16000,
            }).status_code

        self.assertEqual(send(b"\x00\x00" * 10, headers={}), 401)
        self.assertEqual(send(b"\x00" * 3), 400)
        self.assertEqual(send(b"\x00\x00" * 700_000), 400)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
