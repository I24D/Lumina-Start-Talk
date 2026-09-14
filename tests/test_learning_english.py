"""Unit and integration coverage for Lumina Learning English."""

from __future__ import annotations

import copy
import json
import unittest

from fastapi.testclient import TestClient

from dashboard.server import DashboardServer
from learning_english.controller import LearningEnglishController
from learning_english.intent import confirmation_answer, detect_learning_english_intent
from learning_english.service import LearningEnglishService
from learning_english.store import LearningProgressStore
from learning_english.types import LearningEnglishState, TutorResponse
from ui import MainWindow


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
        service = LearningEnglishService(generator=lambda **_: {
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
        self.actions = []
        self.server.set_learning_callbacks(
            state=lambda: self.state,
            action=lambda name, body: self.actions.append((name, body)) or self.state,
            session=lambda handle: self.sessions.append(handle) or {"token": "t", "model": "m", "opening": ""},
            turn=lambda user, assistant: self.turns.append((user, assistant)) or self.state,
        )
        self.client = TestClient(self.server.app)

    def test_studio_and_authenticated_api_are_available(self):
        page = self.client.get("/learning-english")
        self.assertEqual(page.status_code, 200)
        self.assertIn("English Learning Studio", page.text)
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


class LearningDesktopLaunchTests(unittest.TestCase):
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
