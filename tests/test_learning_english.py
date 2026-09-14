"""Unit and integration coverage for Lumina Learning English."""

from __future__ import annotations

import copy
import base64
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dashboard.server import DashboardServer
from learning_english.catalog import build_catalog
from learning_english.controller import LearningEnglishController
from learning_english.intent import confirmation_answer, detect_learning_english_intent
from learning_english.providers import LanguageToolProvider
from learning_english.review import ReviewScheduler
from learning_english.service import LearningEnglishService
from learning_english.store import LearningProgressStore
from learning_english.types import LearningEnglishState, TutorResponse
from ui import MainWindow


class _NoGrammarProvider:
    def status(self):
        return {"id": "grammar-test", "label": "Test", "state": "ready", "detail": ""}

    def check(self, _text):
        return []


class _GrammarFindingProvider(_NoGrammarProvider):
    def check(self, _text):
        return [{
            "original": "go",
            "corrected": "went",
            "explanation": "Past tense is required.",
            "category": "grammar",
            "source": "LanguageTool",
        }]


class LearningEnglishIntentTests(unittest.TestCase):
    def test_explicit_spanish_and_english_requests_are_high_confidence(self):
        for phrase in (
            "Lumina, quiero aprender inglés.",
            "Enséñame inglés.",
            "Activa Learning English.",
            "Teach me English.",
            "Let's practice English.",
            "Practiquemos pronunciación.",
        ):
            with self.subTest(phrase=phrase):
                match = detect_learning_english_intent(phrase)
                self.assertEqual(match.action, "enter")
                self.assertGreaterEqual(match.confidence, 0.9)

    def test_exit_phrases_only_exit_an_active_lesson(self):
        match = detect_learning_english_intent(
            "Regresar a la conversación normal.", active=True
        )
        self.assertEqual(match.action, "exit")
        self.assertGreaterEqual(match.confidence, 0.9)

    def test_exit_works_in_the_words_the_user_actually_used(self):
        for phrase in (
            "desactiva el modo learning",
            "Sí, termina",
            "Vuelve al modo de asistente general",
            "Sal del modo inglés",
            "Termina la clase, por favor",
            "Volver a Lumina",
            "Exit learning mode",
            "Go back to normal",
            "Modo normal",
        ):
            with self.subTest(phrase=phrase):
                match = detect_learning_english_intent(phrase, active=True)
                self.assertEqual(match.action, "exit")
                self.assertGreaterEqual(match.confidence, 0.9)

    def test_lesson_sentences_do_not_end_the_class(self):
        for phrase in (
            "Vuelve a la clase",
            "¿Cómo se dice sal en inglés?",
            "How do I end a sentence in English?",
            "I leave home at eight",
            "Termina la oración en pasado",
        ):
            with self.subTest(phrase=phrase):
                match = detect_learning_english_intent(phrase, active=True)
                self.assertLess(match.confidence, 0.9)

    def test_exit_phrases_do_nothing_outside_a_lesson(self):
        match = detect_learning_english_intent("desactiva el modo learning")
        self.assertEqual(match.action, "none")

    def test_unrelated_english_mention_does_not_switch_modes(self):
        match = detect_learning_english_intent("Busca el clima de English Bay")
        self.assertEqual(match.action, "none")

    def test_confirmation_is_deliberately_narrow(self):
        self.assertIs(confirmation_answer("Sí"), True)
        self.assertIs(confirmation_answer("No, gracias"), False)
        self.assertIsNone(confirmation_answer("quizá mañana"))


class LearningClassLifecycleTests(unittest.TestCase):
    def test_a_new_class_does_not_inherit_the_last_summary(self):
        store = LearningProgressStore(loader=lambda: {}, saver=lambda _: None)
        controller = LearningEnglishController(store)
        controller.enter("click")
        self.assertTrue(controller.complete()["lastSummary"])
        self.assertEqual(controller.enter("click")["lastSummary"], "")


class TutorResponseTests(unittest.TestCase):
    def test_invalid_structured_values_fall_back_safely(self):
        result = TutorResponse.from_mapping(
            {
                "assistantText": "Try again.",
                "exerciseType": "unsupported",
                "lessonProgress": 900,
                "corrections": [{"original": "", "corrected": "x"}],
            }
        )
        self.assertEqual(result.exercise_type, "conversation")
        self.assertEqual(result.lesson_progress, 100)
        self.assertEqual(result.corrections, [])
        self.assertEqual(result.speech_text, "Try again.")

    def test_service_uses_validated_structured_result(self):
        service = LearningEnglishService(grammar_provider=_NoGrammarProvider(), generator=lambda **_: {
            "assistantText": "Good attempt.",
            "speechText": "Good attempt.",
            "uiLanguage": "es",
            "exerciseType": "grammar",
            "corrections": [{
                "original": "She go yesterday.",
                "corrected": "She went yesterday.",
                "explanation": "Went is the past form of go.",
                "category": "grammar",
            }],
            "newVocabulary": [{
                "word": "went", "meaning": "pasado de go", "example": "I went home."
            }],
            "nextAction": "Say it again.",
            "lessonProgress": 25,
            "shouldWaitForUser": True,
            "profileUpdates": {"level": "A2"},
        })
        result = service.analyze_turn(
            user_text="She go yesterday.", assistant_text="Good attempt.", snapshot={}
        )
        self.assertEqual(result.corrections[0].corrected, "She went yesterday.")
        self.assertEqual(result.new_vocabulary[0].word, "went")
        self.assertEqual(result.profile_updates["level"], "A2")

    def test_live_session_token_locks_the_tutor_and_never_carries_the_key(self):
        configs = []

        def factory(config):
            configs.append(config)
            return "auth_tokens/abc"

        service = LearningEnglishService(api_key_loader=lambda: "secret-key", token_factory=factory)
        session = service.create_live_session(
            {}, assistant_name="Lumina", user_name="Dal", voice_name="Aoede"
        )
        self.assertEqual(session["token"], "auth_tokens/abc")
        self.assertEqual(session["model"], "models/gemini-3.1-flash-live-preview")
        self.assertTrue(session["opening"])
        constraints = configs[0]["live_connect_constraints"]
        self.assertEqual(constraints["model"], session["model"])
        self.assertIn("Volver a Lumina", constraints["config"]["system_instruction"])
        self.assertNotIn("secret-key", json.dumps(session))

        resumed = service.create_live_session(
            {}, assistant_name="Lumina", user_name="Dal", voice_name="Aoede", resume_handle="h1"
        )
        self.assertEqual(resumed["opening"], "")
        self.assertEqual(
            configs[1]["live_connect_constraints"]["config"]["session_resumption"], {"handle": "h1"}
        )

    def test_objective_grammar_findings_are_merged_with_gemini_feedback(self):
        service = LearningEnglishService(
            grammar_provider=_GrammarFindingProvider(),
            generator=lambda **_: {
                "assistantText": "Good attempt.",
                "speechText": "Good attempt.",
                "uiLanguage": "es",
                "exerciseType": "grammar",
                "corrections": [],
                "newVocabulary": [],
                "nextAction": "Try once more.",
                "lessonProgress": 15,
                "shouldWaitForUser": True,
                "profileUpdates": {},
            },
        )
        result = service.analyze_turn(
            user_text="Yesterday I go home.",
            assistant_text="Good attempt.",
            snapshot={"currentMode": "grammar"},
        )
        self.assertEqual(len(result.corrections), 1)
        self.assertEqual(result.corrections[0].corrected, "went")
        self.assertEqual(result.corrections[0].source, "LanguageTool")


class LearningCatalogAndReviewTests(unittest.TestCase):
    def test_catalog_has_original_a1_to_c2_route_and_level_gates(self):
        catalog = build_catalog(
            {"level": "B1", "goal": "I need English for travel"},
            {"current_unit_id": "b1-work", "completed_units": ["a1-foundations"]},
        )
        self.assertEqual([item["id"] for item in catalog["levels"]], ["A1", "A2", "B1", "B2", "C1", "C2"])
        self.assertEqual(sum(len(item["units"]) for item in catalog["levels"]), 18)
        self.assertEqual(catalog["recommendedScenarioId"], "hotel-checkin")
        self.assertEqual(
            [item["id"] for item in catalog["listeningActivities"]],
            ["missing-word", "build-sentence", "multiple-choice", "spot-the-word"],
        )
        self.assertTrue(next(item for item in catalog["levels"] if item["id"] == "B1")["units"][1]["current"])
        self.assertFalse(next(item for item in catalog["levels"] if item["id"] == "C2")["units"][0]["available"])

    def test_fsrs_schedules_a_real_next_review(self):
        scheduler = ReviewScheduler()
        now = datetime.now(timezone.utc)
        result = scheduler.review(scheduler.new_card(now), "good", now=now)
        due = datetime.fromisoformat(result["dueAt"])
        self.assertEqual(result["engine"], "fsrs-6")
        self.assertGreater(due, now)
        self.assertFalse(scheduler.is_due(result["card"], now=now))


class LearningProviderTests(unittest.TestCase):
    def test_languagetool_defaults_to_localhost_and_maps_objective_finding(self):
        provider = LanguageToolProvider(timeout=0.01)

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"matches": [{
                    "offset": 2,
                    "length": 2,
                    "message": "Use the past tense.",
                    "replacements": [{"value": "went"}],
                    "rule": {"issueType": "grammar"},
                }]}

        self.assertTrue(provider.endpoint.startswith("http://127.0.0.1:"))
        with patch("learning_english.providers.requests.post", return_value=Response()):
            findings = provider.check("I go yesterday")
        self.assertEqual(findings[0]["original"], "go")
        self.assertEqual(findings[0]["corrected"], "went")
        self.assertEqual(findings[0]["source"], "LanguageTool")
        self.assertEqual(provider.status()["state"], "ready")


class LearningProgressTests(unittest.TestCase):
    def setUp(self):
        self.memory = {}

        def load():
            return copy.deepcopy(self.memory)

        def save(value):
            self.memory = copy.deepcopy(value)

        self.store = LearningProgressStore(loader=load, saver=save)

    def test_progress_survives_a_new_store_instance(self):
        self.store.begin_session("test")
        self.store.update_profile({
            "primary_language": "español", "level": "A2", "goal": "viajes"
        })
        self.store.record_turn(
            "She go yesterday.",
            TutorResponse.from_mapping({
                "assistantText": "She went yesterday.",
                "exerciseType": "grammar",
                "lessonProgress": 30,
                "corrections": [{
                    "original": "She go yesterday.",
                    "corrected": "She went yesterday.",
                    "explanation": "Past simple.",
                    "category": "grammar",
                }],
                "newVocabulary": [{
                    "word": "went", "meaning": "fue", "example": "She went home."
                }],
            }),
        )
        reloaded = LearningProgressStore(
            loader=lambda: copy.deepcopy(self.memory),
            saver=lambda value: setattr(self, "memory", copy.deepcopy(value)),
        ).snapshot()
        self.assertEqual(reloaded["profile"]["level"], "A2")
        self.assertEqual(reloaded["skill_progress"]["grammar"], 30)
        self.assertEqual(reloaded["vocabulary"][0]["word"], "went")

    def test_failed_save_keeps_a_temporary_session(self):
        store = LearningProgressStore(loader=lambda: {}, saver=lambda _: (_ for _ in ()).throw(OSError()))
        store.begin_session("offline")
        state = store.snapshot()
        self.assertTrue(state["current_session_id"])
        self.assertEqual(state["storageStatus"], "temporary")

    def test_vocabulary_is_due_immediately_and_reviewed_with_fsrs(self):
        self.store.save_word("journey", "viaje", "It was a long journey.")
        self.assertEqual(self.store.due_reviews()[0]["word"], "journey")
        reviewed = self.store.review_word("journey", "good")
        state = self.store.snapshot()
        self.assertEqual(reviewed["engine"], "fsrs-6")
        self.assertEqual(state["metrics"]["total_reviews"], 1)
        self.assertEqual(state["metrics"]["successful_reviews"], 1)
        self.assertGreaterEqual(state["metrics"]["xp"], 3)
        self.assertEqual(self.store.due_reviews(), [])

    def test_old_vocabulary_is_migrated_without_losing_content(self):
        legacy = {"learning_english": {
            "version": 1,
            "vocabulary": [{"word": "hello", "meaning": "hola", "example": "Hello!"}],
        }}
        migrated = LearningProgressStore(
            loader=lambda: copy.deepcopy(legacy), saver=lambda _value: None
        ).snapshot()
        self.assertEqual(migrated["version"], 2)
        self.assertEqual(migrated["vocabulary"][0]["word"], "hello")
        self.assertIn("srs", migrated["vocabulary"][0])

    def test_scenario_and_curriculum_selection_persist_in_the_session(self):
        controller = LearningEnglishController(self.store)
        controller.enter("test")
        self.store.update_profile({"level": "B1"})
        controller.select_scenario("job-interview")
        snapshot = controller.select_unit("b1-work")
        snapshot = controller.select_listening_activity("missing-word")
        session = snapshot["sessions"][-1]
        self.assertEqual(snapshot["activeScenario"]["id"], "job-interview")
        self.assertEqual(snapshot["currentUnit"]["id"], "b1-work")
        self.assertEqual(session["scenario_id"], "job-interview")
        self.assertEqual(session["unit_id"], "b1-work")
        self.assertEqual(snapshot["activeListeningActivity"]["id"], "missing-word")

    def test_locked_cefr_content_cannot_be_selected_through_the_api_layer(self):
        controller = LearningEnglishController(self.store)
        controller.enter("test")
        with self.assertRaises(ValueError):
            controller.select_scenario("academic-discussion")
        with self.assertRaises(ValueError):
            controller.select_unit("c2-rhetoric")


class LearningFlowIntegrationTests(unittest.TestCase):
    def test_enter_exercise_correction_and_return_to_normal(self):
        memory = {}

        def load():
            return copy.deepcopy(memory)

        def save(value):
            memory.clear()
            memory.update(copy.deepcopy(value))

        events = []
        controller = LearningEnglishController(
            LearningProgressStore(loader=load, saver=save),
            event_sink=lambda name, payload: events.append((name, payload)),
        )
        entered = controller.enter("click")
        self.assertTrue(entered["active"])
        self.assertEqual(controller.state, LearningEnglishState.ENTERING)
        controller.session_ready()
        response = TutorResponse.from_mapping({
            "assistantText": "She went to work yesterday.",
            "speechText": "She went to work yesterday.",
            "exerciseType": "grammar",
            "corrections": [{
                "original": "She go to work yesterday.",
                "corrected": "She went to work yesterday.",
                "explanation": "Went is the past of go.",
                "category": "grammar",
            }],
            "newVocabulary": [],
            "nextAction": "Repeat the corrected sentence.",
            "lessonProgress": 40,
            "shouldWaitForUser": True,
            "profileUpdates": {},
        })
        active = controller.apply_turn("She go to work yesterday.", response)
        self.assertEqual(active["lastResponse"]["corrections"][0]["corrected"], "She went to work yesterday.")
        ended = controller.complete()
        self.assertFalse(ended["active"])
        self.assertEqual(ended["state"], "inactive")
        self.assertTrue(ended["last_lesson"]["summary"])
        names = [name for name, _ in events]
        self.assertIn("learningEnglish.modeEntered", names)
        self.assertIn("learningEnglish.correctionCreated", names)
        self.assertIn("learningEnglish.sessionCompleted", names)
        self.assertIn("learningEnglish.modeExited", names)


class LearningWebApiTests(unittest.TestCase):
    def setUp(self):
        self.server = DashboardServer()
        self.server._tokens.add("test-token")
        self.headers = {"Authorization": "Bearer test-token"}
        self.state = {"active": True, "state": "lesson"}
        self.sessions = []
        self.turns = []
        self.pronunciations = []
        self.actions = []
        self.server.set_learning_callbacks(
            state=lambda: self.state,
            action=lambda name, body: self.actions.append((name, body)) or self.state,
            session=lambda handle: self.sessions.append(handle) or {"token": "t", "model": "m", "opening": ""},
            turn=lambda user, assistant: self.turns.append((user, assistant)) or self.state,
            pronunciation=lambda pcm, expected, rate: self.pronunciations.append(
                (pcm, expected, rate)
            ) or {"score": 92},
        )
        self.client = TestClient(self.server.app)

    def test_studio_and_authenticated_api_are_available(self):
        page = self.client.get("/learning-english")
        self.assertEqual(page.status_code, 200)
        self.assertIn("English Learning Studio", page.text)
        self.assertIn("Ruta de aprendizaje A1–C2", page.text)
        self.assertIn("Repaso inteligente", page.text)
        self.assertIn("Elige un escenario real", page.text)
        self.assertNotIn("gemini_api_key", page.text)
        self.assertEqual(self.client.get("/api/learning/state").status_code, 401)
        state = self.client.get("/api/learning/state", headers=self.headers)
        self.assertEqual(state.json()["state"], "lesson")

    def test_session_turn_and_exit_flow_reach_backend_callbacks(self):
        self.assertEqual(self.client.post("/api/learning/live-session", json={}).status_code, 401)
        session = self.client.post(
            "/api/learning/live-session", headers=self.headers, json={"handle": "h1"}
        )
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["token"], "t")
        self.assertEqual(self.sessions, ["h1"])
        turn = self.client.post(
            "/api/learning/turn", headers=self.headers,
            json={"user": "I study English", "assistant": "Great!"},
        )
        self.assertEqual(turn.status_code, 200)
        self.assertEqual(self.turns, [("I study English", "Great!")])
        ended = self.client.post(
            "/api/learning/action", headers=self.headers, json={"action": "exit"}
        )
        self.assertEqual(ended.status_code, 200)
        self.assertEqual(self.actions[0][0], "exit")

        scenario = self.client.post(
            "/api/learning/action", headers=self.headers,
            json={"action": "scenario", "scenario_id": "coffee-shop"},
        )
        review = self.client.post(
            "/api/learning/action", headers=self.headers,
            json={"action": "review-word", "word": "hello", "rating": "good"},
        )
        listening = self.client.post(
            "/api/learning/action", headers=self.headers,
            json={"action": "listening-activity", "activity_id": "missing-word"},
        )
        self.assertEqual(scenario.status_code, 200)
        self.assertEqual(review.status_code, 200)
        self.assertEqual(listening.status_code, 200)
        self.assertEqual(
            [item[0] for item in self.actions[-3:]],
            ["scenario", "review-word", "listening-activity"],
        )

    def test_pronunciation_audio_reaches_the_optional_engine_callback(self):
        response = self.client.post(
            "/api/learning/pronunciation",
            headers=self.headers,
            json={
                "expected_text": "Hello",
                "sample_rate": 16000,
                "pcm_base64": base64.b64encode(b"\x00\x00" * 80).decode("ascii"),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["score"], 92)
        self.assertEqual(self.pronunciations[0][1:], ("Hello", 16000))


class LearningDesktopLaunchTests(unittest.TestCase):
    def test_learning_keeps_the_user_requested_separate_browser_microphone(self):
        browser_source = Path("dashboard/static/learning-english.js").read_text(encoding="utf-8")
        desktop_source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("navigator.mediaDevices.getUserMedia", browser_source)
        self.assertIn("and not self._learning.active", desktop_source)

    def test_learning_button_callback_does_not_reference_remote_overlay_state(self):
        calls = []

        class Toggle:
            def setChecked(self, checked):
                calls.append(("checked", checked))

        class Log:
            def append_log(self, message):
                calls.append(("log", message))

        class WindowStub:
            on_learning_english = staticmethod(lambda: calls.append(("opened", True)))
            _drawer_btn = Toggle()
            _log = Log()

            @staticmethod
            def _toggle_drawer(opened):
                calls.append(("drawer", opened))

        MainWindow._open_learning_english(WindowStub())

        self.assertIn(("opened", True), calls)
        self.assertNotIn(("log", "ERR: Learning English could not start. Check the console for details."), calls)

    def test_learning_url_uses_loopback_for_the_local_browser(self):
        server = DashboardServer()
        url = server.get_learning_url()
        self.assertIn("://127.0.0.1:8000/auto-login", url)
        self.assertIn("next=/learning-english", url)

    def test_learning_url_uses_the_plain_loopback_twin_once_it_listens(self):
        # The HTTPS certificate is self-signed; the browser on this PC blocks it.
        server = DashboardServer()
        server._loopback_server = type("Listening", (), {"started": True})()
        url = server.get_learning_url()
        self.assertTrue(url.startswith("http://127.0.0.1:8002/auto-login"), url)
        self.assertIn("next=/learning-english", url)


if __name__ == "__main__":
    unittest.main()
