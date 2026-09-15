"""Course features of Learning English: age groups and the Pre-A1 route, the
placement test, generated activities and the recommended next step."""

from __future__ import annotations

import copy
import json
import unittest

from fastapi.testclient import TestClient

from dashboard.server import DashboardServer
from learning_english import activities, placement
from learning_english import service as service_module
from learning_english.catalog import CEFR_LEVELS, build_catalog, next_step, next_unit_id
from learning_english.controller import LearningEnglishController
from learning_english.ollama_cloud import OllamaCloud
from learning_english.service import LearningEnglishService
from learning_english.store import LearningProgressStore
from learning_english.types import TutorResponse


def _store():
    memory = {}

    def load():
        return copy.deepcopy(memory)

    def save(value):
        memory.clear()
        memory.update(copy.deepcopy(value))

    return LearningProgressStore(loader=load, saver=save), memory


def _turn(progress=100, exercise="conversation", **extra):
    return TutorResponse.from_mapping({
        "assistantText": "Well done.", "exerciseType": exercise,
        "lessonProgress": progress, **extra,
    })


def _wrong(question):
    return (question["answer"] + 1) % len(question["options"])


def _walk_placement(right_levels, audience="adults", step_fn=None):
    """Take the placement test, answering right only on the given levels."""
    step_fn = step_fn or (lambda answers: placement.advance(answers, audience=audience))
    answers = []
    while True:
        step = step_fn(answers)
        if step["done"]:
            return step, answers
        question = next(q for q in placement.QUESTIONS if q["id"] == step["question"]["id"])
        right = question["level"] in right_levels
        answers.append({"id": question["id"], "choice": question["answer"] if right else _wrong(question)})


def _checkpoint_raw():
    return {
        "title": "Presentaciones",
        "questions": [
            {
                "prompt": f"Question {number}", "options": ["I am", "I is", "I are"],
                "answerIndex": 0, "explanation": "Use am with I.",
            }
            for number in range(5)
        ],
    }


def _reading_raw():
    return {
        "title": "My dog",
        "text": "I have a dog. My dog is brown and very friendly. We play in the park every Saturday morning.",
        "translation": "Tengo un perro. Mi perro es café y muy amigable.",
        "glossary": [
            {"word": "friendly", "meaning": "amigable", "example": "She is friendly."},
            {"word": "Saturday", "meaning": "sábado", "example": "See you on Saturday."},
            {"word": "elephant", "meaning": "elefante", "example": "It is not in the text."},
        ],
        "questions": [
            {"prompt": "What colour is the dog?", "options": ["Brown", "Black", "White"], "answerIndex": 0, "explanation": "The text says brown."},
            {"prompt": "When do they play?", "options": ["Sunday", "Saturday", "Monday"], "answerIndex": 1, "explanation": "Every Saturday."},
            {"prompt": "Broken", "options": ["Same", "same", "Other"], "answerIndex": 0, "explanation": ""},
        ],
    }


class _NoGrammar:
    def status(self):
        return {"id": "grammar-test", "label": "Test", "state": "optional", "detail": ""}

    def check(self, _text):
        return []


class AudienceAndRouteTests(unittest.TestCase):
    def test_the_route_starts_from_zero(self):
        catalog = build_catalog({"level": "A1"}, {})
        self.assertEqual(
            [level["id"] for level in catalog["levels"]],
            ["PRE-A1", "A1", "A2", "B1", "B2", "C1", "C2"],
        )
        self.assertTrue(all(unit["available"] for unit in catalog["levels"][0]["units"]))
        self.assertEqual(catalog["audience"], "adults")

    def test_each_age_group_sees_its_own_scenarios_first(self):
        kids = build_catalog({"level": "PRE-A1", "audience": "kids"}, {})
        self.assertIn("kids", kids["scenarios"][0]["audiences"])
        self.assertEqual(kids["recommendedScenarioId"], "classroom")
        teens = build_catalog({"level": "A2", "audience": "teens"}, {})
        self.assertIn("teens", teens["scenarios"][0]["audiences"])
        self.assertEqual(teens["recommendedScenarioId"], "gaming-online")

    def test_next_unit_stays_on_the_students_level(self):
        self.assertEqual(next_unit_id("B1", ["b1-opinions"]), "b1-work")
        a1_units = [unit["id"] for unit in CEFR_LEVELS[1]["units"]]
        self.assertEqual(next_unit_id("A1", a1_units), "")
        self.assertTrue(build_catalog({"level": "A1"}, {"completed_units": a1_units})["levelComplete"])

    def test_a_unit_completes_only_after_all_its_lessons(self):
        controller = LearningEnglishController(_store()[0])
        for lesson in range(4):
            controller.enter("test")
            controller.apply_turn("Hi", _turn())
            controller.apply_turn("Hi again", _turn())  # still one lesson for this class
            snapshot = controller.complete()
            self.assertEqual(snapshot["curriculum"]["unit_lessons"]["a1-foundations"], lesson + 1)
        self.assertIn("a1-foundations", snapshot["curriculum"]["completed_units"])
        self.assertEqual(snapshot["currentUnit"]["id"], "a1-daily-life")
        self.assertNotIn("a1-daily-life", snapshot["curriculum"]["unit_lessons"])

    def test_a_role_play_credits_the_scenario_not_the_unit(self):
        controller = LearningEnglishController(_store()[0])
        controller.enter("test")
        controller.select_scenario("coffee-shop")
        snapshot = controller.apply_turn("A coffee, please.", _turn())
        self.assertIn("coffee-shop", snapshot["curriculum"]["completed_scenarios"])
        self.assertEqual(snapshot["curriculum"]["unit_lessons"], {})
        self.assertIsNone(controller.complete()["activeScenario"])

    def test_choosing_a_mode_unit_activity_or_exit_ends_the_role_play(self):
        controller = LearningEnglishController(_store()[0])
        controller.enter("test")
        for leave in (
            lambda: controller.set_mode("grammar"),
            lambda: controller.select_unit("a1-daily-life"),
            lambda: controller.select_listening_activity("missing-word"),
            controller.end_scenario,
        ):
            self.assertIsNotNone(controller.select_scenario("coffee-shop")["activeScenario"])
            self.assertIsNone(leave()["activeScenario"])

    def test_a_spoken_assessment_reaching_100_is_not_a_lesson(self):
        controller = LearningEnglishController(_store()[0])
        controller.enter("test")
        snapshot = controller.apply_turn("I think B1", _turn(exercise="assessment"))
        self.assertEqual(snapshot["curriculum"]["unit_lessons"], {})

    def test_audience_accepts_only_known_age_groups(self):
        controller = LearningEnglishController(_store()[0])
        self.assertEqual(controller.set_audience("kids")["profile"]["audience"], "kids")
        with self.assertRaises(ValueError):
            controller.set_audience("aliens")
        controller.enter("test")
        snapshot = controller.apply_turn(
            "Es para mi hijo adolescente", _turn(progress=10, profileUpdates={"audience": "teens"})
        )
        self.assertEqual(snapshot["profile"]["audience"], "teens")
        snapshot = controller.apply_turn("x", _turn(progress=10, profileUpdates={"audience": "robots"}))
        self.assertEqual(snapshot["profile"]["audience"], "teens")

    def test_the_last_lesson_survives_a_restart(self):
        store, memory = _store()
        controller = LearningEnglishController(store)
        controller.enter("test")
        controller.complete()
        reloaded = LearningProgressStore(
            loader=lambda: copy.deepcopy(memory), saver=lambda _value: None
        ).snapshot()
        self.assertTrue(reloaded["last_lesson"]["summary"])

    def test_weekly_target_is_validated(self):
        store = _store()[0]
        store.set_weekly_target(5)
        self.assertEqual(store.snapshot()["curriculum"]["weekly_target"], 5)
        for bad in (0, 8, "many"):
            with self.subTest(target=bad), self.assertRaises(ValueError):
                store.set_weekly_target(bad)

    def test_a_new_level_moves_the_route_to_that_level(self):
        store = _store()[0]
        store.update_profile({"level": "B1"})
        self.assertEqual(store.snapshot()["curriculum"]["current_unit_id"], "b1-opinions")

    def test_saved_progress_gains_the_new_skills_without_losing_its_own(self):
        legacy = {"learning_english": {
            "skill_progress": {"grammar": 40}, "profile": {"audience": "martians"},
        }}
        snapshot = LearningProgressStore(
            loader=lambda: copy.deepcopy(legacy), saver=lambda _value: None
        ).snapshot()
        self.assertEqual(snapshot["skill_progress"]["grammar"], 40)
        self.assertEqual(snapshot["skill_progress"]["reading"], 0)
        self.assertEqual(snapshot["profile"]["audience"], "")


class PlacementTests(unittest.TestCase):
    def test_someone_who_knows_no_english_lands_on_pre_a1_quickly(self):
        step, answers = _walk_placement(set())
        self.assertEqual(step["level"], "PRE-A1")
        self.assertLessEqual(len(answers), 4)

    def test_an_advanced_student_climbs_to_c1(self):
        step, _ = _walk_placement(set(placement.LEVEL_ORDER))
        self.assertEqual(step["level"], "C1")

    def test_mixed_answers_settle_on_the_last_level_passed(self):
        step, answers = _walk_placement({"PRE-A1", "A1", "A2"})
        self.assertEqual(step["level"], "A2")
        self.assertEqual(len(answers), 6)

    def test_children_start_at_pre_a1_and_adults_at_a1(self):
        self.assertEqual(placement.advance([], audience="kids")["question"]["id"], "pre-a1-colour")
        self.assertEqual(placement.advance([], audience="adults")["question"]["id"], "a1-to-be")

    def test_the_browser_never_receives_the_answer_key(self):
        step = placement.advance([])
        self.assertNotIn("answer", step["question"])
        self.assertNotIn('"answer"', json.dumps(step))

    def test_repeated_and_unknown_answers_are_ignored(self):
        step = placement.advance([
            {"id": "a1-to-be", "choice": 0},
            {"id": "a1-to-be", "choice": 0},
            {"id": "made-up", "choice": 1},
        ])
        self.assertEqual(step["answered"], 1)
        self.assertEqual(step["question"]["id"], "a1-name")

    def test_the_result_sets_the_level_and_that_levels_first_unit(self):
        controller = LearningEnglishController(_store()[0])
        controller.enter("test")
        step, _ = _walk_placement({"PRE-A1", "A1", "A2"}, step_fn=controller.placement_step)
        self.assertEqual(step["level"], "A2")
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["profile"]["level"], "A2")
        self.assertTrue(snapshot["profile"]["level_confirmed"])
        self.assertEqual(snapshot["currentUnit"]["id"], "a2-past-plans")
        self.assertEqual(snapshot["placement"]["level"], "A2")


class ActivityTests(unittest.TestCase):
    def test_a_checkpoint_keeps_only_well_formed_questions(self):
        raw = _checkpoint_raw()
        raw["questions"][0]["answerIndex"] = 7
        raw["questions"][1]["options"] = ["same", "Same", "other"]
        self.assertEqual(len(activities.validate("checkpoint", raw)["questions"]), 3)
        raw["questions"] = raw["questions"][:2]
        with self.assertRaises(ValueError):
            activities.validate("checkpoint", raw)

    def test_a_reading_glossary_keeps_only_words_found_in_the_text(self):
        activity = activities.validate("reading", _reading_raw())
        self.assertEqual([entry["word"] for entry in activity["glossary"]], ["friendly", "Saturday"])
        self.assertEqual(len(activity["questions"]), 2)

    def test_the_browser_copy_has_no_answer_key_and_lumina_grades(self):
        activity = activities.validate("reading", _reading_raw())
        self.assertNotIn("answer", json.dumps(activities.public(activity)["questions"]))
        result = activities.grade(activity, [0, 0])
        self.assertEqual((result["correct"], result["total"], result["score"]), (1, 2, 50))
        self.assertFalse(result["passed"])

    def test_passing_a_checkpoint_completes_the_unit(self):
        controller = LearningEnglishController(_store()[0])
        controller.enter("test")
        public = controller.register_activity(
            activities.validate("checkpoint", _checkpoint_raw(), unit_id="a1-foundations")
        )
        self.assertNotIn("answer", json.dumps(public))
        self.assertTrue(controller.submit_activity(public["id"], [0, 0, 0, 0, 0])["passed"])
        snapshot = controller.snapshot()
        self.assertIn("a1-foundations", snapshot["curriculum"]["completed_units"])
        self.assertTrue(snapshot["curriculum"]["checkpoints"]["a1-foundations"]["passed"])
        self.assertEqual(snapshot["currentUnit"]["id"], "a1-daily-life")
        with self.assertRaises(ValueError):
            controller.submit_activity(public["id"], [0])

    def test_a_reading_raises_the_reading_skill(self):
        controller = LearningEnglishController(_store()[0])
        controller.enter("test")
        public = controller.register_activity(activities.validate("reading", _reading_raw()))
        controller.submit_activity(public["id"], [0, 1])
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["skill_progress"]["reading"], 100)
        self.assertEqual(snapshot["activity_log"][-1]["kind"], "reading")

    def test_activities_need_an_open_class(self):
        controller = LearningEnglishController(_store()[0])
        with self.assertRaises(RuntimeError):
            controller.register_activity(activities.validate("checkpoint", _checkpoint_raw()))

    def test_the_generation_brief_follows_the_course_and_the_age_group(self):
        seen = []
        service = LearningEnglishService(
            grammar_provider=_NoGrammar(),
            generator=lambda request, schema: seen.append((request, schema)) or _reading_raw(),
        )
        snapshot = {
            "profile": {"level": "PRE-A1", "audience": "kids", "primary_language": "español"},
            "currentUnit": {
                "id": "pre-a1-first-words", "title": "Números, colores y objetos",
                "objective": "Contar hasta 20", "skill": "vocabulary", "level": "PRE-A1",
            },
        }
        activity = service.generate_activity("reading", snapshot)
        request, schema = seen[0]
        self.assertIs(schema, activities.READING_SCHEMA)
        self.assertEqual(request["student"]["audience"], "kids")
        self.assertIn("child", " ".join(request["rules"]))
        self.assertIn("25 to 50", request["task"])
        self.assertEqual(activity["unitId"], "pre-a1-first-words")
        with self.assertRaises(ValueError):
            service.generate_activity("essay", snapshot)


class NextStepTests(unittest.TestCase):
    def test_the_studio_recommends_one_clear_step(self):
        confirmed = {"level": "A1", "level_confirmed": True}
        catalog = build_catalog(
            confirmed, {"current_unit_id": "a1-foundations", "unit_lessons": {"a1-foundations": 2}}
        )
        self.assertEqual(next_step({"level_confirmed": False}, catalog, reviews_due=9)["kind"], "placement")
        self.assertEqual(next_step(confirmed, catalog, reviews_due=3)["kind"], "review")
        unit = next_step(confirmed, catalog, reviews_due=0)
        self.assertEqual(unit["kind"], "unit")
        self.assertIn("2/4", unit["detail"])
        done = build_catalog(
            confirmed, {"completed_units": ["a1-foundations", "a1-daily-life", "a1-survival"]}
        )
        self.assertEqual(next_step(confirmed, done, reviews_due=0)["kind"], "assessment")


class CourseWebApiTests(unittest.TestCase):
    def setUp(self):
        self.server = DashboardServer()
        self.server._tokens.add("test-token")
        self.headers = {"Authorization": "Bearer test-token"}
        self.calls = []
        self.server.set_learning_callbacks(
            state=lambda: {"active": True},
            placement=lambda answers: self.calls.append(("placement", answers))
            or {"step": {"done": False}, "state": {}},
            activity=lambda op, body: self.calls.append((op, body.get("kind")))
            or {"activity": {"id": "a"}},
        )
        self.client = TestClient(self.server.app)

    def test_placement_and_activities_require_auth_and_reach_lumina(self):
        self.assertEqual(self.client.post("/api/learning/placement", json={"answers": []}).status_code, 401)
        self.assertEqual(self.client.post("/api/learning/activity", json={"op": "create"}).status_code, 401)
        answer = [{"id": "a1-to-be", "choice": 0}]
        placed = self.client.post("/api/learning/placement", headers=self.headers, json={"answers": answer})
        self.assertEqual(placed.status_code, 200)
        created = self.client.post(
            "/api/learning/activity", headers=self.headers, json={"op": "create", "kind": "reading"}
        )
        self.assertEqual(created.json()["activity"]["id"], "a")
        self.assertEqual(self.calls, [("placement", answer), ("create", "reading")])

    def test_malformed_requests_are_rejected_and_new_actions_are_allowed(self):
        self.assertEqual(
            self.client.post("/api/learning/placement", headers=self.headers, json={"answers": "all"}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/learning/activity", headers=self.headers, json={"op": "delete"}).status_code, 400
        )
        for action in ("audience", "scenario-end"):
            with self.subTest(action=action):
                response = self.client.post(
                    "/api/learning/action", headers=self.headers, json={"action": action}
                )
                self.assertNotEqual(response.json().get("error"), "Unsupported action")


class _QuotaSpent(Exception):
    code = 429


class _Models:
    """Stands in for the Gemini client's models; spent ones answer 429."""

    def __init__(self, spent=()):
        self.spent = set(spent)
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        if model in self.spent:
            raise _QuotaSpent("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        return type("Response", (), {"text": json.dumps(_reading_raw())})()


def _service_with(models):
    client = type("Client", (), {"models": models})()
    return LearningEnglishService(
        api_key_loader=lambda: "key", client_factory=lambda _key: client,
        ollama=OllamaCloud(), grammar_provider=_NoGrammar(),
        # Never the user's real OpenAI key: selecting OpenAI would put it first.
        openai_key_loader=lambda: None,
    )


class ModelQuotaTests(unittest.TestCase):
    def test_a_spent_daily_quota_moves_on_and_stays_skipped(self):
        models = _Models(spent={service_module.MODEL})
        service = _service_with(models)
        snapshot = {"profile": {"level": "A1"}, "currentUnit": {"id": "a1-foundations"}}
        service.generate_activity("reading", snapshot)
        service.generate_activity("reading", snapshot)
        lighter = service_module.GEMINI_TEXT_MODELS[1]
        self.assertEqual(models.calls, [service_module.MODEL, lighter, lighter])

    def test_a_busy_model_hands_over_without_being_skipped_later(self):
        class Busy(_Models):
            def generate_content(self, *, model, contents, config):
                if model == service_module.MODEL and not self.calls:
                    self.calls.append(model)
                    error = Exception("503 UNAVAILABLE")
                    error.code = 503
                    raise error
                return super().generate_content(model=model, contents=contents, config=config)

        models = Busy()
        service = _service_with(models)
        service.generate_activity("reading", {"profile": {}})
        service.generate_activity("reading", {"profile": {}})
        self.assertEqual(
            models.calls,
            [service_module.MODEL, service_module.GEMINI_TEXT_MODELS[1], service_module.MODEL],
        )

    def test_errors_other_than_quota_are_not_hidden(self):
        class Broken:
            def generate_content(self, **_kwargs):
                raise TypeError("bad request")

        with self.assertRaises(TypeError):
            _service_with(Broken()).generate_activity("checkpoint", {"profile": {}})

    def test_when_every_model_is_spent_the_quota_error_surfaces(self):
        models = _Models(spent=set(service_module.GEMINI_TEXT_MODELS))
        with self.assertRaises(_QuotaSpent):
            _service_with(models).generate_activity("reading", {"profile": {}})
        self.assertEqual(models.calls, list(service_module.GEMINI_TEXT_MODELS))

    def test_turns_where_only_the_tutor_spoke_spend_no_quota(self):
        models = _Models()
        result = _service_with(models).analyze_turn(
            user_text="", assistant_text="Hello! What's your name?", snapshot={}
        )
        self.assertEqual(models.calls, [])
        self.assertEqual(result.assistant_text, "Hello! What's your name?")

    def test_only_the_students_own_turns_earn_xp(self):
        controller = LearningEnglishController(_store()[0])
        controller.enter("test")
        controller.apply_turn("", _turn(progress=5))
        self.assertEqual(controller.snapshot()["metrics"]["xp"], 0)
        controller.apply_turn("Hello", _turn(progress=5))
        self.assertEqual(controller.snapshot()["metrics"]["xp"], 5)


if __name__ == "__main__":
    unittest.main()
