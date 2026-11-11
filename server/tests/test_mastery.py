"""Behavioral tests for deterministic mastery, revision and exposure semantics."""

from copy import deepcopy
import unittest

from app.services.mastery import DEFAULT_RULE_VERSION, build_mastery, compute_mastery, get_rule


def evidence(number: int, *, family: str | None = None, ratio: float = 1.0, **changes) -> dict:
    return {"id": f"e{number}", "student_id": "student", "goal_id": "goal", "question_id": f"q{number}",
            "family_id": family or f"family-{number}", "attempt_id": f"attempt-{number}", "revision": 1,
            "score_ratio": ratio, "exposure": "independent", "occurred_at": float(number), **changes}


class MasteryTests(unittest.TestCase):
    def calculate(self, rows, **kwargs):
        return compute_mastery(rows, student_id="student", goal_id="goal", **kwargs)

    def test_defaults_are_versioned_and_explicitly_uncalibrated(self):
        rule = get_rule().to_dict()
        self.assertEqual(rule["version"], DEFAULT_RULE_VERSION)
        self.assertFalse(rule["calibrated"])
        self.assertEqual(rule["min_independent_families"], 3)
        self.assertEqual(rule["mastery_threshold_percent"], 80)
        self.assertIn("未经校准", rule["notice"])
        with self.assertRaises(ValueError):
            get_rule("mastery-does-not-exist")

    def test_missing_or_insufficient_is_none_not_zero(self):
        for rows in ([], [evidence(1, ratio=0)], [evidence(1), evidence(2)]):
            result = self.calculate(rows)
            self.assertIsNone(result["percent"])
            self.assertEqual(result["status"], "insufficient_evidence")
        result = self.calculate([evidence(index, ratio=0) for index in range(1, 4)])
        self.assertEqual(result["percent"], 0.0)
        self.assertEqual(result["status"], "needs_practice")

    def test_grouping_family_deduplicates_variants_and_later_retries(self):
        rows = [evidence(1, family="same", ratio=0), evidence(2, family="same"), evidence(3, family="same")]
        result = self.calculate(rows)
        self.assertEqual(result["independent_families"], 1)
        self.assertEqual(result["observations"]["redo"], 2)
        self.assertEqual(result["families"][0]["independent_score_ratio"], 0)
        self.assertIsNone(result["percent"])

    def test_hints_exposure_and_redo_never_become_independent(self):
        rows = [evidence(1, assisted=True), evidence(2, exposed=True), evidence(3, retry=True),
                evidence(4, exposure="unknown"), evidence(5, attempt_number=2)]
        result = self.calculate(rows)
        self.assertEqual(result["independent_families"], 0)
        self.assertEqual(result["observations"], {"answer_exposed": 1, "hinted": 1, "independent": 0, "redo": 2, "unknown": 1})
        result = self.calculate([evidence(1, family="f", assisted=True), evidence(2, family="f")])
        self.assertEqual(result["independent_families"], 0)
        self.assertEqual(result["observations"]["redo"], 1)

    def test_missing_family_and_pending_score_cannot_manufacture_sample(self):
        result = self.calculate([evidence(1, family_id=None), evidence(2, status="pending"), evidence(3, status="failed")])
        self.assertEqual(result["independent_families"], 0)
        self.assertIsNone(result["percent"])

    def test_integer_score_units_and_explicit_independence_contract(self):
        rows = []
        for number, earned in enumerate((10, 10, 5), 1):
            row = evidence(number)
            row.pop("score_ratio")
            row.pop("exposure")
            row.update(score_units=earned, max_score_units=10, assisted=False, exposed=False, retry=False)
            rows.append(row)
        result = self.calculate(rows)
        self.assertEqual(result["percent"], 83.3)
        self.assertEqual(result["status"], "mastered")

    def test_latest_evidence_revision_changes_calculation_and_hash(self):
        rows = [evidence(index) for index in range(1, 4)]
        initial = self.calculate(rows)
        revised = self.calculate(rows + [evidence(1, revision=2, ratio=0)])
        self.assertEqual(initial["percent"], 100)
        self.assertEqual(revised["percent"], 66.7)
        self.assertNotEqual(initial["evidence_revision"], revised["evidence_revision"])
        self.assertEqual(len(revised["evidence_refs"]), 3)
        self.assertEqual(revised, self.calculate(list(reversed(rows + [evidence(1, revision=2, ratio=0)]))))
        with self.assertRaisesRegex(ValueError, "conflicting evidence"):
            self.calculate(rows + [evidence(1, ratio=0)])

    def test_retraction_removes_latest_logical_evidence(self):
        rows = [evidence(index) for index in range(1, 4)] + [evidence(3, revision=2, retracted=True)]
        result = self.calculate(rows)
        self.assertEqual(result["independent_families"], 2)
        self.assertIsNone(result["percent"])

    def test_stable_against_reordering_and_does_not_mutate_input(self):
        rows = [evidence(1, ratio=.1), evidence(2, ratio=.6), evidence(3)]
        before = deepcopy(rows)
        result = self.calculate(rows)
        self.assertEqual(result, self.calculate(rows[::-1]))
        self.assertEqual(rows, before)
        self.assertEqual(result["percent"], 56.7)

    def test_teacher_judgment_is_separate_and_new_evidence_updates_derived_result(self):
        rows = [evidence(index) for index in range(1, 4)]
        initial = self.calculate(rows)
        marker = {"id": "judgment-1", "student_id": "student", "goal_id": "goal", "judgment": "needs_review",
                  "revision": 1, "evidence_revision": initial["evidence_revision"], "created_at": 5,
                  "internal_note": "must not appear in derived output"}
        marked = self.calculate(rows, teacher_judgments=[marker])
        self.assertEqual(marked["status"], "mastered")
        self.assertEqual(marked["teacher_judgment"]["judgment"], "needs_review")
        self.assertFalse(marked["teacher_judgment"]["evidence_changed"])
        changed = self.calculate(rows + [evidence(4, ratio=0)], teacher_judgments=[marker])
        self.assertEqual(changed["percent"], 75)
        self.assertEqual(changed["status"], "developing")
        self.assertTrue(changed["teacher_judgment"]["evidence_changed"])
        self.assertNotIn("internal_note", str(changed))

    def test_matrix_includes_subject_without_any_evidence(self):
        result = build_mastery([evidence(1)], students=["student", "missing"], goals=["goal", "other"])
        self.assertEqual(len(result), 4)
        missing = next(row for row in result if row["student_id"] == "missing" and row["goal_id"] == "goal")
        self.assertIsNone(missing["percent"])
        self.assertEqual(missing["observed_families"], 0)

    def test_invalid_numeric_or_string_booleans_are_rejected(self):
        for changes in ({"score_ratio": float("nan")}, {"score_ratio": 2}, {"score_ratio": True},
                        {"assisted": "denied"}, {"retry": "false"}, {"revision": True}, {"attempt_number": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.calculate([evidence(1, **changes)])


if __name__ == "__main__":
    unittest.main()
