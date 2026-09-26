"""Run with python -B -m unittest discover -s 写作 -p test_learning.py -v.

Both tracker copies and their real prompts are exercised without network calls.
Every mutable fixture lives in a TemporaryDirectory under repo/my/learning.
"""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]


def load_tracker(folder):
    spec = importlib.util.spec_from_file_location(
        "learning_test_" + folder, ROOT / folder / "learning_tracking.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_real_app(folder):
    """Load app.py in isolation so its real /api/evaluate route is exercised."""
    tracker_module = load_tracker(folder)
    old_tracker = sys.modules.get("learning_tracking")
    old_path = list(sys.path)
    sys.modules["learning_tracking"] = tracker_module
    sys.path.insert(0, str(ROOT / folder))
    try:
        spec = importlib.util.spec_from_file_location(
            "real_app_test_" + folder, ROOT / folder / "app.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = old_path
        if old_tracker is None:
            sys.modules.pop("learning_tracking", None)
        else:
            sys.modules["learning_tracking"] = old_tracker


class FakeAI:
    pairs = (
        ("人工智能提升效率却也改变职业结构", "城市绿化改善环境并且促进居民健康"),
        ("数字教育扩大机会同时带来隐私挑战", "传统节日传承文化也需要适应时代"),
        ("公共交通缓解拥堵还能够减少排放", "科研合作推动创新并且培养青年人才"),
    )

    def __init__(self):
        self.calls = []
        self.evaluations = []
        self.generation_count = 0
        self.repeat_sources = False

    @staticmethod
    def evaluation(score=90, major=False):
        return {
            "score": score, "passed": True, "feedback": "具体反馈",
            "remaining_issues": [{"explanation": "关键意义错误", "severity": "major"}] if major else [],
            "corrected_translation": "Corrected answer after submission.",
        }

    def __call__(self, messages, model=None):
        system = messages[0]["content"]
        payload = json.loads(messages[1]["content"])
        self.calls.append((system, payload, model))
        if "remaining_issues" in system or "评分" in system:
            result = self.evaluations.pop(0) if self.evaluations else self.evaluation()
        else:
            if '"items"' not in system:
                raise AssertionError("Unknown review prompt")
            index = 0 if self.repeat_sources else self.generation_count
            sources = self.pairs[index]
            self.generation_count += 1
            result = {"items": [
                {"source_cn": source, "reference": "PRIVATE_TRANSFER_" + str(index) + str(i), "focus": payload["tag"]}
                for i, source in enumerate(sources)
            ]}
        return json.dumps(result, ensure_ascii=False)


class LearningContract:
    def setUp(self):
        self.code = load_tracker(self.folder)
        parent = ROOT / "my" / "learning"
        existed = parent.exists()
        parent.mkdir(parents=True, exist_ok=True)
        if not existed:
            # Preserve the directory if another process has added real data.
            def remove_empty_parent():
                try:
                    parent.rmdir()
                except OSError:
                    pass
            self.addCleanup(remove_empty_parent)
        self.temp = tempfile.TemporaryDirectory(prefix="unittest-", dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.my_dir = Path(self.temp.name)
        self.clock = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        self.ai = FakeAI()
        self.store = self.code.LearningStore(self.module, self.my_dir, self.ai, json.loads, ROOT / self.folder / "prompts")
        self.store.now = lambda: self.clock.isoformat()
        self.answer = "Students learns from mistakes. They studies together."
        self.source = "学生从错误中学习。学校鼓励他们共同进步，并且在困难时期保持耐心。"
        self.key = "source_text" if self.module == "writing" else "source_cn"
        self.fragment = "Students learns from mistakes." if self.module == "writing" else "学生从错误中学习。"
        self.second_fragment = "They studies together." if self.module == "writing" else "学校鼓励他们共同进步，并且在困难时期保持耐心。"
        self.tag = "subject_verb_agreement" if self.module == "writing" else "omission"
        self.exercise = ({"topic": "learning", "prompt": "Directions: Discuss learning.", "prompt_type": "opinion", "sample_essay": "Sample essay retained in history."}
                         if self.module == "writing" else {"topic": "learning", "source": self.source, "reference": "Students learn from mistakes. Schools encourage cooperation."})

    def error(self, source=None, tag=None, **extra):
        result = {self.key: self.fragment if source is None else source, "tag": self.tag if tag is None else tag,
                  "issue_type": "structure", "explanation": "Fix the target problem.", "suggestion": "PRIVATE_ORIGINAL_REFERENCE", "severity": "major"}
        if self.module == "translation":
            result["user_text"] = "Students learns from mistakes."
        result.update(extra)
        return result

    def result(self, score=86, errors=None):
        result = {"score": score, "level": "good", "subscores": {key: score for key in self.code.DIMENSIONS[self.module]},
                  "feedback": "Keep improving.", "strengths": ["clear argument"], "next_focus": ["grammar"],
                  "error_items": [self.error()] if errors is None else errors}
        for key in self.code.DIMENSIONS[self.module]:
            result[key] = "Detailed dimension feedback"
        result["errors" if self.module == "writing" else "missed_terms"] = ["target issue"]
        return result

    def record(self, result=None, attempt_id=None, answer=None):
        return self.store.record_evaluation(self.exercise, self.answer if answer is None else answer,
                                            self.result() if result is None else result, attempt_id or str(uuid.uuid4()))

    def queue(self):
        return json.loads(self.store.queue_path.read_text(encoding="utf-8"))

    def item(self):
        self.record()
        return self.queue()[0]

    def due(self, item):
        self.clock = datetime.fromisoformat(item["due_at"])

    def submit(self, item, kind="original", round_id=None, index=None, score=90, major=False, **extra):
        self.ai.evaluations.append(self.ai.evaluation(score, major))
        data = {"review_id": item["review_id"], "kind": kind, "answer": "My corrected English answer.", "attempt_id": str(uuid.uuid4())}
        if round_id is not None:
            data["round_id"] = round_id
        if index is not None:
            data["index"] = index
        data.update(extra)
        return self.store.review_evaluate(data)

    def round(self, item, scores=(90, 90), major=False):
        original = self.submit(item)
        context = {"review_id": item["review_id"], "round_id": original["item"]["round"]["round_id"]}
        generated = self.store.generate_transfer(context)
        for index, score in enumerate(scores):
            self.submit(item, "transfer", context["round_id"], index, score, major and index == 0)
        return self.store.complete(context), generated, context

    def test_history_fields_substrings_tags_and_initial_queue(self):
        errors = [self.error(), self.error("absent phrase"), self.error(""),
                  self.error(self.second_fragment, "unknown_tag"), self.error(suggestion="")]
        result = self.result(errors=errors)
        output = self.record(result)
        self.assertEqual(output["result"], result)
        self.assertTrue({"result", "history_id", "progress"} <= output.keys())
        row = self.store.history()["items"][0]
        self.assertEqual(row["id"], output["history_id"])
        self.assertEqual(row["type"], self.module)
        self.assertEqual(row["created_at"], self.store.now())
        self.assertEqual(row["user_answer"], self.answer)
        for key, value in self.exercise.items():
            self.assertEqual(row[key], value)
        for key in ("score", "level", "subscores", "feedback", "strengths", "next_focus"):
            self.assertEqual(row[key], result[key])
        self.assertEqual([e["source_valid"] for e in row["error_items"]], [True, False, False, True, True])
        self.assertEqual(row["error_items"][3]["tag"], "other")
        queued = self.queue()
        self.assertEqual(len(queued), 2)
        self.assertEqual([x["tag"] for x in queued], [self.tag, "other"])
        for item in queued:
            self.assertEqual(item["source_history_id"], row["id"])
            self.assertEqual(item["stage"], 0)
            self.assertEqual(item["status"], "learning")
            self.assertEqual(item["due_at"], "2026-01-02T00:00:00+00:00")
        self.assertEqual(self.store.list_review()["items"], [])
        self.due(queued[0])
        self.assertEqual(len(self.store.list_review()["items"]), 2)
        if self.module == "writing":
            self.assertEqual(row["word_count"], len(self.answer.split()))
            self.assertEqual(set(row["subscores"]), {"task", "coherence", "vocab", "grammar"})
        else:
            self.assertEqual(row["source"], self.source)
            self.assertEqual(row["reference"], self.exercise["reference"])
            self.assertEqual(row["missed_terms"], ["target issue"])
            self.assertEqual(row["error_items"][0]["source_cn"], self.fragment)
            self.assertEqual(row["error_items"][0]["user_text"], "Students learns from mistakes.")
            self.assertEqual(set(row["subscores"]), {"accuracy", "fluency", "term_use", "grammar"})

    def test_fixed_tags(self):
        expected = ("subject_verb_agreement article tense singular_plural preposition clause non_finite sentence_structure word_choice collocation chinese_english coherence task_response punctuation other"
                    if self.module == "writing" else "omission addition mistranslation subject_error logic_relation tense article singular_plural preposition clause non_finite passive_active long_sentence word_choice collocation chinese_english proper_noun culture_loaded_term number_unit other")
        self.assertEqual(set(self.code.TAGS[self.module]), set(expected.split()))
        self.record(self.result(errors=[self.error(tag=tag) for tag in expected.split()]))
        self.assertEqual({x["tag"] for x in self.queue()}, set(expected.split()))

    def test_compare_first_second_and_duplicate_tags_not_merged(self):
        first = self.record(self.result(70, [self.error(), self.error(tag="article")]))
        self.assertTrue(first["progress"]["is_first_attempt"])
        second = self.record(self.result(90, [self.error(), self.error(tag="tense")]))
        comparison = second["progress"]
        self.assertEqual(comparison["previous_score"], 70)
        self.assertEqual(comparison["current_score"], 90)
        self.assertEqual(comparison["score_delta"], 20)
        self.assertEqual(comparison["subscore_delta"], {k: 20 for k in self.code.DIMENSIONS[self.module]})
        self.assertEqual(comparison["repeated_tags"], [self.tag])
        self.assertEqual(comparison["resolved_tags"], ["article"])
        self.assertEqual(comparison["new_tags"], ["tense"])
        self.assertEqual(len(self.queue()), 4)
        self.assertEqual(len({x["review_id"] for x in self.queue()}), 4)

    def test_six_attempt_averages_and_rolling_window(self):
        for index, score in enumerate((60, 70, 80, 80, 90, 100)):
            self.record(self.result(score, []))
            if index < 5:
                self.assertIsNone(self.store.progress()["improvement"])
        improvement = self.store.progress()["improvement"]
        for part in [improvement, *improvement["subscores"].values()]:
            self.assertEqual(part["previous_average"], 70)
            self.assertEqual(part["recent_average"], 90)
            self.assertEqual(part["delta"], 20)
        self.record(self.result(80, []))
        self.assertEqual(self.store.progress()["improvement"]["previous_average"], 76.67)

    def test_record_positional_attempt_id_duplicate_and_conflict(self):
        attempt_id = str(uuid.uuid4())
        first = self.record(attempt_id=attempt_id)
        duplicate = self.record(attempt_id=attempt_id)
        self.assertEqual(first["history_id"], duplicate["history_id"])
        self.assertEqual(self.store.history()["items"][0]["attempt_id"], attempt_id)
        self.assertEqual(len(self.queue()), 1)
        with self.assertRaises(self.code.LearningConflict):
            self.record(attempt_id=attempt_id, answer="A different answer.")
        evaluator = Mock(return_value=self.result())
        key = str(uuid.uuid4())
        self.store.evaluate(self.exercise, self.answer, "model", key, evaluator)
        self.store.evaluate(self.exercise, self.answer, "model", key, evaluator)
        evaluator.assert_called_once()

    def test_corrupt_history_keeps_good_records_bytes_and_normal_score(self):
        self.record()
        with self.store.history_path.open("a", encoding="utf-8") as file:
            file.write("{broken json\n")
        before = self.store.history_path.read_bytes()
        with self.assertLogs(self.store.logger, level="WARNING"):
            output = self.record()
        self.assertEqual(output["result"]["score"], 86)
        self.assertIn("tracking_warning", output)
        self.assertEqual(self.store.history_path.read_bytes(), before)
        self.assertEqual(len(self.store.history()["items"]), 1)

    def test_corrupt_queue_does_not_block_score_or_overwrite_file(self):
        self.record()
        self.store.queue_path.write_text("{broken json", encoding="utf-8")
        before = self.store.queue_path.read_bytes()
        with self.assertLogs(self.store.logger, level="WARNING"):
            output = self.record()
        self.assertEqual(output["result"]["score"], 86)
        self.assertIn("history_id", output)
        self.assertIn("tracking_warning", output)
        self.assertEqual(self.store.queue_path.read_bytes(), before)
        self.assertEqual(len(self.store.history()["items"]), 2)

    def test_atomic_write_failure_preserves_history_score_and_retry(self):
        self.record()
        before = self.store.history_path.read_bytes()
        evaluator = Mock(return_value=self.result(92))
        attempt_id = str(uuid.uuid4())
        with patch.object(self.code.os, "replace", side_effect=OSError("disk full")), self.assertLogs(self.store.logger, level="WARNING"):
            output = self.store.evaluate(self.exercise, self.answer, None, attempt_id, evaluator)
        self.assertEqual(output["result"]["score"], 92)
        self.assertIn("tracking_warning", output)
        self.assertEqual(self.store.history_path.read_bytes(), before)
        self.assertEqual(list(self.store.root.glob("*.tmp")), [])
        retry = self.store.evaluate(self.exercise, self.answer, None, attempt_id, evaluator)
        self.assertEqual(retry, output)
        evaluator.assert_called_once()

    def test_original_failure_blocks_generation_and_transfer_submission(self):
        item = self.item()
        self.due(item)
        original = self.submit(item, score=95, major=True)
        self.assertFalse(original["result"]["passed"])
        context = {"review_id": item["review_id"], "round_id": original["item"]["round"]["round_id"]}
        calls = len(self.ai.calls)
        with self.assertRaises(ValueError):
            self.store.generate_transfer(context)
        with self.assertRaises(ValueError):
            self.store.review_evaluate({**context, "kind": "transfer", "index": 0, "answer": "Not allowed"})
        self.assertEqual(len(self.ai.calls), calls)
        self.assertFalse(self.store.complete(context)["passed"])

    def test_transfer_average_passes_even_if_one_score_below_80(self):
        item = self.item()
        self.due(item)
        completed, _, _ = self.round(item, scores=(70, 90))
        self.assertTrue(completed["passed"])
        self.assertEqual(completed["item"]["stage"], 1)

    def test_transfer_major_fails_despite_high_average(self):
        item = self.item()
        self.due(item)
        completed, _, _ = self.round(item, scores=(95, 95), major=True)
        self.assertFalse(completed["passed"])
        self.assertEqual(completed["item"]["stage"], 0)
        self.assertEqual(completed["item"]["due_at"], (self.clock + timedelta(days=1)).isoformat())

    def test_three_due_rounds_mastered_and_recurrence_starts_new_item(self):
        item = self.item()
        seen = set()
        for stage in (1, 2, 3):
            self.due(item)
            completed, generated, context = self.round(item)
            sources = {x["source_cn"] for x in generated["item"]["round"]["transfers"]}
            self.assertTrue(seen.isdisjoint(sources))
            seen.update(sources)
            self.assertEqual(completed["item"]["stage"], stage)
            if stage < 3:
                self.assertEqual(completed["item"]["due_at"], (self.clock + timedelta(days=3 if stage == 1 else 7)).isoformat())
            # Replaying completion cannot advance another stage.
            self.assertEqual(self.store.complete(context), completed)
            item = completed["item"]
        self.assertEqual(item["status"], "mastered")
        self.assertEqual(self.store.progress()["review_counts"]["mastered"], 1)
        self.clock += timedelta(days=1)
        self.record()
        old, new = self.queue()
        self.assertEqual(old["status"], "mastered")
        self.assertTrue(new["recurrence"])
        self.assertEqual(new["stage"], 0)
        frequent = self.store.progress()["frequent_errors"][0]
        self.assertEqual(frequent["occurrence"], "recurrence")
        self.assertEqual(frequent["status"], "relearning")

    def test_early_round_does_not_advance_even_when_finished_after_due(self):
        item = self.item()
        original = self.submit(item)
        context = {"review_id": item["review_id"], "round_id": original["item"]["round"]["round_id"]}
        self.assertTrue(original["item"]["round"]["early"])
        self.store.generate_transfer(context)
        self.due(item)
        for index in (0, 1):
            self.submit(item, "transfer", context["round_id"], index)
        completed = self.store.complete(context)
        self.assertTrue(completed["passed"])
        self.assertTrue(completed["early"])
        self.assertEqual(completed["item"]["stage"], 0)
        self.assertEqual(completed["item"]["due_at"], item["due_at"])

    def test_new_round_cannot_reuse_old_transfer_answers_or_sources(self):
        item = self.item()
        self.due(item)
        completed, _, old_context = self.round(item)
        self.due(completed["item"])
        original = self.submit(item)
        context = {"review_id": item["review_id"], "round_id": original["item"]["round"]["round_id"]}
        self.assertNotEqual(context["round_id"], old_context["round_id"])
        self.assertEqual(original["item"]["round"]["transfers"], [])
        with self.assertRaises(ValueError):
            self.store.complete(context)
        with self.assertRaises(self.code.LearningConflict):
            self.store.review_evaluate({**old_context, "kind": "transfer", "index": 0, "answer": "Old round"})
        self.ai.repeat_sources = True
        with self.assertRaises(ValueError):
            self.store.generate_transfer(context)
        self.assertEqual(self.queue()[0]["rounds"][-1]["transfers"], [])

    def test_reference_hidden_until_individual_transfer_submitted(self):
        item = self.item()
        before = self.store.list_review(all_items=True)
        self.assertNotIn("PRIVATE_ORIGINAL_REFERENCE", json.dumps(before))
        previous = self.store.previous(item["review_id"])
        self.assertNotIn("PRIVATE_ORIGINAL_REFERENCE", json.dumps(previous))
        original = self.submit(item)
        context = {"review_id": item["review_id"], "round_id": original["item"]["round"]["round_id"]}
        generated = self.store.generate_transfer(context)
        self.assertNotIn("PRIVATE_TRANSFER", json.dumps(generated))
        submitted = self.submit(item, "transfer", context["round_id"], 0)
        self.assertEqual(submitted["result"]["reference"], "PRIVATE_TRANSFER_00")
        self.assertNotIn("PRIVATE_TRANSFER_01", json.dumps(submitted))
        self.assertEqual(submitted["item"]["round"]["transfers"][1]["result"], None)

    def test_review_duplicate_conflict_does_not_repeat_ai(self):
        item = self.item()
        key = str(uuid.uuid4())
        data = {"review_id": item["review_id"], "kind": "original", "answer": "Correct answer", "attempt_id": key}
        first = self.store.review_evaluate(data)
        duplicate = self.store.review_evaluate(data)
        self.assertEqual(first, duplicate)
        self.assertEqual(len(self.ai.calls), 1)
        self.assertEqual(len(self.queue()[0]["attempts"]), 1)
        with self.assertRaises(self.code.LearningConflict):
            self.store.review_evaluate({**data, "answer": "Changed answer"})
        with self.assertRaises(self.code.LearningConflict):
            self.store.review_evaluate({**data, "round_id": str(uuid.uuid4())})

    def test_flask_routes_storage_validation_and_conflict(self):
        app = Flask(__name__, template_folder=str(ROOT / self.folder / "templates"))
        app.config["TESTING"] = True
        tracker = self.code.install_learning(app, self.module, self.my_dir, self.ai, json.loads, ROOT / self.folder / "prompts")
        tracker.now = self.store.now
        self.assertIs(app.extensions["learning_tracking_" + self.module], tracker)
        item = self.item()
        self.due(item)
        client = app.test_client()
        for path in ("/progress", "/review", "/api/history", "/api/progress", "/api/review"):
            self.assertEqual(client.get(path).status_code, 200, path)
        self.assertEqual(len(client.get("/api/history").get_json()["items"]), 1)
        self.assertEqual(len(client.get("/api/review").get_json()["items"]), 1)
        previous = client.get("/api/review/" + item["review_id"] + "/previous")
        self.assertEqual(previous.status_code, 200)
        self.assertEqual(previous.get_json()["old_answer"], self.fragment if self.module == "writing" else "Students learns from mistakes.")
        for payload in ([], "text", None):
            self.assertEqual(client.post("/api/review/evaluate", json=payload).status_code, 400)
        data = {"review_id": item["review_id"], "kind": "original", "answer": "Correct answer", "attempt_id": str(uuid.uuid4())}
        first = client.post("/api/review/evaluate", json=data)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(client.post("/api/review/evaluate", json=data).get_json(), first.get_json())
        self.assertEqual(client.post("/api/review/evaluate", json={**data, "answer": "Changed"}).status_code, 409)
        context = {"review_id": item["review_id"], "round_id": first.get_json()["item"]["round"]["round_id"]}
        generated = client.post("/api/review/generate-transfer", json=context)
        self.assertEqual(generated.status_code, 200)
        self.assertNotIn("PRIVATE_TRANSFER", generated.get_data(as_text=True))
        self.assertEqual(client.post("/api/review/complete", json=context).status_code, 400)
        for index in (0, 1):
            response = client.post("/api/review/evaluate", json={**context, "kind": "transfer", "index": index, "answer": "Correct transfer", "attempt_id": str(uuid.uuid4())})
            self.assertEqual(response.status_code, 200)
        completed = client.post("/api/review/complete", json=context)
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()["item"]["stage"], 1)

    def test_real_app_evaluate_route_records_and_is_idempotent(self):
        real_app = load_real_app(self.folder)
        real_app.learning_tracker = self.store
        real_app.LearningConflict = self.code.LearningConflict
        scoring_result = self.result(90, [])
        scoring_call = Mock(return_value=json.dumps(scoring_result, ensure_ascii=False))
        real_app.call_deepseek = scoring_call
        real_app.parse_json_loose = json.loads
        real_app.app.config["TESTING"] = True
        exercise = copy.deepcopy(self.exercise)
        if self.module == "writing":
            exercise["sample_essay"] = "A sufficiently long sample essay that satisfies the real writing validation contract."
        else:
            exercise["source"] = "学生从错误中学习，学校鼓励他们共同进步，并且在困难时期保持耐心，这种教育方式有助于培养独立思考和合作解决问题的能力。"
        attempt_id = str(uuid.uuid4())
        client = real_app.app.test_client()
        payload = {"exercise": exercise, "answer": self.answer, "attempt_id": attempt_id}
        response = client.post("/api/evaluate", json=payload)
        self.assertEqual(response.status_code, 200)
        output = response.get_json()
        self.assertEqual(output["result"]["score"], 90)
        self.assertIn("history_id", output)
        self.assertIn("progress", output)
        self.assertEqual(output["word_count_server"], len(self.answer.split())) if self.module == "writing" else self.assertNotIn("word_count_server", output)
        duplicate = client.post("/api/evaluate", json=payload)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.get_json()["history_id"], output["history_id"])
        conflict = client.post("/api/evaluate", json={**payload, "answer": "A materially different answer."})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(self.store.history()["items"]), 1)


class WritingLearningTests(LearningContract, unittest.TestCase):
    folder = "写作"
    module = "writing"


class TranslationLearningTests(LearningContract, unittest.TestCase):
    folder = "翻译"
    module = "translation"


if __name__ == "__main__":
    unittest.main()
