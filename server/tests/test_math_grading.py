"""Exact arithmetic, conservative domains and hostile-input grading contracts."""

from copy import deepcopy
from decimal import Decimal
import json
import os
import unittest
from unittest.mock import patch

os.environ["SKIP_ENV_FILE"] = "1"

from app.services.math_grading import (
    MAX_AST_DEPTH, MAX_AST_NODES, MAX_EXPRESSION_LENGTH, SCORE_UNITS_PER_POINT,
    expressions_equivalent, grade_objective, normalize_expression,
    validate_ai_rubric, validate_answer,
)


def question(kind="math", answer="1/2", **changes):
    value = {"type": kind, "answer": answer, "score_units": 250}
    value.update(changes)
    return value


class ExpressionTests(unittest.TestCase):
    def test_exact_fraction_decimal_arithmetic_and_polynomials(self):
        equivalents = (
            ("0.5", "1/2"), ("0.1+0.2", "0.3"), ("1e-3", "1/1000"),
            ("(1/3)+(1/6)", "0.5"), ("-(-0.25)", "1/4"),
            ("2**-3", "1/8"), ("2^0", "1"), ("(1+2)^3", "27"),
            ("(x+1)^2", "x*x+2*x+1"), ("x+x", "2*x"),
            ("(x+y)*(x-y)", "x^2-y^2"), ("(2*x+4)/2", "x+2"),
            ("x/(y-y+2)", "x/2"), ("(x+1)-(x+1)", "0"),
            ("3*(a+b)-a", "2*a+3*b"), ("x-x+1", "1"),
        )
        for left, right in equivalents:
            with self.subTest(left=left, right=right):
                self.assertIs(expressions_equivalent(left, right), True)
                self.assertIs(expressions_equivalent(right, left), True)
        for left, right in (("0.1+0.2", "0.30000000000000004"), ("x+1", "x-1"),
                            ("x", "y"), ("1/3", "0.3333333333333333"), ("x*x", "x")):
            with self.subTest(left=left, right=right):
                self.assertIs(expressions_equivalent(left, right), False)

    def test_unicode_and_notation_normalization(self):
        for left, right in (("２ × (３＋１)", "8"), ("6÷4", "3/2"),
                            ("−0.5", "-1/2"), ("x²+2×x+1", "(x+1)^2"),
                            ("x⁻¹", "1/x"), ("½", "0.5"), ("1/½", "2"), ("½^2", "1/4"), ("2⋅x", "x+x")):
            with self.subTest(left=left, right=right):
                expected = None if left == "x⁻¹" else True
                self.assertIs(expressions_equivalent(left, right), expected)
        self.assertEqual(normalize_expression("x^2 × 2"), "x**2 * 2")
        self.assertIsNone(expressions_equivalent("1½", "1.5"))

    def test_unproved_domains_and_powers_are_uncertain_even_for_identical_strings(self):
        expressions = ("x/x", "1/x", "(x*x-1)/(x-1)", "0/x", "x^0", "0^0", "0^-1",
                       "1/0", "(x-x)/(x-x)", "x^-1", "x^(1/2)", "(-1)^(1/2)",
                       "x^y", "2^(x-x)", "sqrt(x*x)", "abs(x)")
        for value in expressions:
            with self.subTest(value=value):
                # 2^(x-x) is the supported polynomial exponent zero over a
                # provably nonzero constant base and therefore is deterministic.
                expected = True if value == "2^(x-x)" else None
                self.assertIs(expressions_equivalent(value, value), expected)
        self.assertIsNone(expressions_equivalent("(x*x-1)/(x-1)", "x+1"))
        self.assertIsNone(expressions_equivalent("0*(1/x)", "0"))
        self.assertIsNone(expressions_equivalent("(1/x)-(1/x)", "0"))

    def test_expression_injection_never_executes_or_interprets_python(self):
        attacks = (
            "__import__('os').system('echo unexpected')", "open('secret').read()",
            "(1).__class__.__mro__", "[x for x in range(1000000)]", "(lambda:1)()",
            "x; import os", "x=1", "exec('print(1)')", "eval('1+1')", "{'x':1}",
            "f'{1}'", "[1][0]", "True", "False", "None", "1j", "0x10", "1_000",
            "1//2", "1%2", "1<<100", "x and y", "1 < 2", "x if y else z",
            "nan", "Infinity", "∞", "x\x00+1", "2x", "1,2", "1 # comment",
        )
        with patch("os.system", side_effect=AssertionError("must not execute")), patch("builtins.eval", side_effect=AssertionError("must not eval")), patch("builtins.exec", side_effect=AssertionError("must not exec")):
            for value in attacks:
                with self.subTest(value=value):
                    self.assertIsNone(expressions_equivalent(value, value))

    def test_resource_bounds_fail_safely(self):
        expressions = (
            "1" * (MAX_EXPRESSION_LENGTH + 1),
            "(" * (MAX_AST_DEPTH + 1) + "1" + ")" * (MAX_AST_DEPTH + 1),
            "+".join("1" for _ in range(MAX_AST_NODES + 1)),
            "-" * (MAX_AST_DEPTH + 1) + "1", "2^17", "9^9^9", "2^999999999",
            "1e999999999", "1e-999999999", "9" * 129,
            "a+b+c+d+e", "(a+b+c+d)^16", "((x^16)^16)^16",
        )
        for value in expressions:
            with self.subTest(value=value[:100]):
                self.assertIsNone(expressions_equivalent(value, value))
        self.assertIs(expressions_equivalent("2^16", "65536"), True)
        self.assertIs(expressions_equivalent("x^16", "x^16"), True)


class ObjectiveGradingTests(unittest.TestCase):
    def test_math_grades_in_integer_units_and_uncertain_is_explicit(self):
        result = grade_objective(question(), "0.5")
        self.assertEqual(result["status"], "graded")
        self.assertEqual(result["score_units"], 250)
        self.assertIs(type(result["score_units"]), int)
        result = grade_objective(question(), "0.6")
        self.assertEqual((result["status"], result["score_units"]), ("graded", 0))
        for value in ("x/x", "1/0", "__import__('os')", "sin(x)", "x" * 9000):
            with self.subTest(value=value[:100]):
                result = grade_objective(question(answer=value), value)
                self.assertIn(result["status"], {"uncertain", "manual_pending"})
                self.assertEqual(result["score_units"], 0)
                self.assertTrue(result["why"])

    def test_selection_and_judgment_types_are_strict(self):
        single = question("single_choice", 1, options=["first", "second", "third"])
        for value in (True, False, 1.0, "1", -1, 3, None, [1], {}):
            with self.subTest(value=value):
                self.assertFalse(validate_answer(single, value)["valid"])
                self.assertEqual(grade_objective(single, value)["status"], "manual_pending")
        self.assertEqual(grade_objective(single, 1)["score_units"], 250)
        self.assertEqual(grade_objective(single, 0)["score_units"], 0)
        multi = question("multi_choice", [0, 2], options=single["options"])
        self.assertEqual(grade_objective(multi, [2, 0])["score_units"], 250)
        self.assertEqual(grade_objective(multi, [0])["score_units"], 0)
        for value in ([0, 0, 2], [True, 2], [0.0, 2], [0, 3], [], "0,2", {0, 2}):
            with self.subTest(value=value):
                self.assertFalse(validate_answer(multi, value)["valid"])
        judge = question("judge", True)
        self.assertEqual(grade_objective(judge, True)["score_units"], 250)
        self.assertEqual(grade_objective(judge, False)["score_units"], 0)
        for value in (1, 0, "true", "True", None, []):
            with self.subTest(value=value):
                self.assertEqual(grade_objective(judge, value)["status"], "manual_pending")
        for malformed in ({**single, "answer": True}, {**multi, "answer": [0, 0]}, {**judge, "answer": 1}):
            self.assertEqual(grade_objective(malformed, 1 if malformed["type"] == "single_choice" else [0] if malformed["type"] == "multi_choice" else True)["status"], "manual_pending")

    def test_fill_math_alternatives_and_exact_text_opt_in(self):
        fill = question("fill", ["1/2", "0.50"])
        self.assertEqual(grade_objective(fill, "0.5")["score_units"], 250)
        self.assertEqual(grade_objective(fill, "1/3")["score_units"], 0)
        fill["answer"].append("sqrt(2)")
        self.assertEqual(grade_objective(fill, "1/3")["status"], "uncertain")
        words = question("fill", "triangle")
        self.assertEqual(grade_objective(words, "triangle")["status"], "uncertain")
        words["answer_format"] = "text"
        self.assertEqual(grade_objective(words, " triangle ")["score_units"], 250)
        self.assertEqual(grade_objective(words, "TRIANGLE")["score_units"], 0)
        self.assertEqual(grade_objective(question("short_answer", "keyword", rubric=[{"point": "keyword", "score_units": 250}]), "keyword")["status"], "manual_pending")

    def test_restricted_domains_are_not_assumed_to_be_all_reals(self):
        for domain in ("integers", "{0,1}", {"x": [0, 1]}, ["positive"]):
            with self.subTest(domain=domain):
                result = grade_objective(question(answer="x", domain=domain), "x^2")
                self.assertEqual(result["status"], "uncertain")
        self.assertEqual(grade_objective(question(answer="x", domain="real"), "x^2")["status"], "graded")
        self.assertEqual(grade_objective(question(answer="x", variables={"x": "integer"}), "x")["status"], "uncertain")

    def test_scores_exact_conversion_invalid_boundaries_and_default_units(self):
        self.assertEqual(SCORE_UNITS_PER_POINT, 100)
        self.assertEqual(grade_objective({"type": "math", "answer": "1/2", "score": "2.75"}, "0.5")["score_units"], 275)
        self.assertEqual(grade_objective({"type": "math", "answer": "1/2"}, "0.5")["score_units"], 100)
        for value in (True, -1, 1.5, "100", 10**12):
            with self.subTest(value=value):
                self.assertEqual(grade_objective(question(score_units=value), "0.5")["status"], "manual_pending")
        for value in (True, -1, float("nan"), float("inf"), "NaN", "Infinity", "1.001", "2.0000000000000000000000000000001"):
            with self.subTest(value=value):
                result = grade_objective({"type": "math", "answer": "1/2", "score": value}, "0.5")
                self.assertEqual(result["status"], "manual_pending")
        self.assertEqual(grade_objective(question(score_units=0), "0.5")["score_units"], 0)


class RubricTests(unittest.TestCase):
    def setUp(self):
        self.question = question("short_answer", "Explain both steps", score_units=500, rubric=[
            {"point": "common denominator", "score_units": 200},
            {"point": "exact arithmetic", "score_units": 300},
        ])
        self.grade = {"points": [{"point": "common denominator", "score_units": 100, "matched": False, "why": "partly justified"},
                                 {"point": "exact arithmetic", "score_units": 300, "matched": True, "why": "correct"}],
                      "score_units": 400, "why": "Partial credit for one incomplete explanation."}

    def test_partial_credit_is_per_point_and_exact(self):
        before = deepcopy(self.grade)
        graded = validate_ai_rubric(self.question, self.grade)
        self.assertEqual((graded["status"], graded["score_units"], graded["valid"]), ("graded", 400, True))
        self.assertEqual(sum(point["score_units"] for point in graded["per_point"]), 400)
        self.assertEqual(self.grade, before)
        self.assertEqual(validate_ai_rubric(self.question, json.dumps(self.grade))["score_units"], 400)
        legacy_question = {"type": "short_answer", "score": "5.00", "rubric": [
            {"point": "common denominator", "score": "2.00"}, {"point": "exact arithmetic", "score": Decimal("3.00")}],}
        legacy_result = '{"points":[{"point":"common denominator","score":1.25},{"point":"exact arithmetic","score":2.50}],"score":3.75,"why":"partial"}'
        self.assertEqual(validate_ai_rubric(legacy_question, legacy_result)["score_units"], 375)

    def test_rejects_missing_duplicate_reordered_points_and_mismatched_totals(self):
        cases = []
        for points in (self.grade["points"][:1], self.grade["points"] * 2, list(reversed(self.grade["points"]))):
            cases.append({**self.grade, "points": points})
        cases.extend(({**self.grade, "score_units": 401}, {**self.grade, "why": ""},
                      {**self.grade, "score": 4}, {"score_units": 500, "why": "keywords matched"}))
        altered = deepcopy(self.grade)
        altered["points"][1]["point"] = altered["points"][0]["point"]
        cases.append(altered)
        for payload in cases:
            with self.subTest(payload=payload):
                result = validate_ai_rubric(self.question, payload)
                self.assertEqual((result["status"], result["score_units"], result["valid"]), ("manual_pending", 0, False))
                self.assertEqual(result["per_point"], [])
        malformed_question = deepcopy(self.question)
        malformed_question["rubric"][0]["score_units"] = 500
        self.assertEqual(validate_ai_rubric(malformed_question, self.grade)["status"], "manual_pending")

    def test_rejects_nonfinite_coerced_out_of_range_and_subunit_scores_without_clamping(self):
        for score in (True, -1, 201, "100", 0.5, float("nan"), float("inf"), 10**12):
            payload = deepcopy(self.grade)
            payload["points"][0]["score_units"] = score
            with self.subTest(score=score):
                self.assertEqual(validate_ai_rubric(self.question, payload)["status"], "manual_pending")
        for matched in ("true", 1, None):
            payload = deepcopy(self.grade)
            payload["points"][0]["matched"] = matched
            self.assertEqual(validate_ai_rubric(self.question, payload)["status"], "manual_pending")
        for score in ("NaN", "Infinity", "0.001", 3):
            payload = deepcopy(self.grade)
            payload.pop("score_units")
            payload["points"][0].pop("score_units")
            payload["points"][0]["score"] = score
            self.assertEqual(validate_ai_rubric(self.question, payload)["status"], "manual_pending")

    def test_requires_complete_strict_json_and_never_uses_keywords(self):
        for payload in ('prefix ' + json.dumps(self.grade), '```json\n' + json.dumps(self.grade) + '\n```',
                        json.dumps(self.grade) + ' trailing', '{"points":[],"why":"x","score":NaN}',
                        '{"points":[],"points":[],"why":"x"}', self.question["answer"], {"points": []}):
            with self.subTest(payload=str(payload)[:100]):
                self.assertEqual(validate_ai_rubric(self.question, payload)["status"], "manual_pending")
        self.assertEqual(grade_objective(self.question, "common denominator exact arithmetic")["status"], "manual_pending")


if __name__ == "__main__":
    unittest.main()
