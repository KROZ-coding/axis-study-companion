"""Bounded, deterministic objective grading; no code execution or model calls.

Expressions are exact rational polynomials over real variables. Division by an
unproved nonzero expression, fractional/variable powers and unsupported syntax
are uncertain, even when two strings look identical. AI rubric output has a
separate structural validator and never falls back to keyword matching.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
import re
from typing import Any
import unicodedata


SCORE_UNITS_PER_POINT = 100
MAX_EXPRESSION_LENGTH = 2048
MAX_AST_NODES = 256
MAX_AST_DEPTH = 32
MAX_EXPONENT = 16
MAX_DEGREE = 32
MAX_TERMS = 128
MAX_VARIABLES = 4
MAX_COEFFICIENT_BITS = 2048
MAX_OPERATIONS = 8192
MAX_SCORE_UNITS = 10**9
_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
_NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_NAME = re.compile(r"[A-Za-z]\Z")
# A monomial is a sorted tuple of (variable, positive exponent) pairs.
_Monomial = tuple[tuple[str, int], ...]
_Polynomial = dict[_Monomial, Fraction]


class _Uncertain(ValueError):
    pass


def normalize_expression(expression: str) -> str:
    """Normalize notation only, without adding implicit multiplication or code."""
    if not isinstance(expression, str) or len(expression) > MAX_EXPRESSION_LENGTH or not expression.strip():
        raise _Uncertain("expression length")
    # NFKC turns x² into x2; rewrite superscripts first to retain their meaning.
    expression = re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+", lambda match: "**(" + match.group().translate(_SUPERSCRIPTS) + ")", expression)
    # A mixed number needs explicit disambiguation; 1½ must not become 11/2.
    if re.search(r"[0-9][¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]", expression):
        raise _Uncertain("ambiguous mixed number")
    # Parenthesize compatibility fractions before NFKC expands them, keeping
    # their boundary in expressions such as 1/½ and (½)^2.
    expression = re.sub(r"[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]", lambda match: "(" + unicodedata.normalize("NFKC", match.group()) + ")", expression)
    expression = unicodedata.normalize("NFKC", expression)
    if re.search(r"(?:\d\s*\(\d+⁄\d+\)|(?<=\))\s*\d)", expression):
        raise _Uncertain("ambiguous mixed number")
    expression = expression.translate(str.maketrans({"−": "-", "–": "-", "×": "*", "⋅": "*", "·": "*", "÷": "/", "⁄": "/", "∕": "/"})).replace("^", "**").strip()
    if len(expression) > MAX_EXPRESSION_LENGTH or not re.fullmatch(r"[A-Za-z0-9.+*/()\s-]+", expression):
        raise _Uncertain("unsupported notation")
    return expression


def _checked(value: Fraction) -> Fraction:
    if value.numerator.bit_length() > MAX_COEFFICIENT_BITS or value.denominator.bit_length() > MAX_COEFFICIENT_BITS:
        raise _Uncertain("coefficient bound")
    return value


def _constant(value: Fraction) -> _Polynomial:
    return {(): _checked(value)} if value else {}


def _constant_value(poly: _Polynomial) -> Fraction | None:
    if not poly:
        return Fraction(0)
    return poly.get(()) if len(poly) == 1 and () in poly else None


class _Parser:
    def __init__(self, expression: str) -> None:
        self.source = normalize_expression(expression)
        self.variables: set[str] = set()
        self.remaining = MAX_OPERATIONS

    def parse(self) -> _Polynomial:
        depth = 0
        for char in self.source:
            if char == "(":
                depth += 1
                if depth > MAX_AST_DEPTH:
                    raise _Uncertain("parenthesis depth")
            elif char == ")":
                depth -= 1
        try:
            tree = ast.parse(self.source, mode="eval")
        except (SyntaxError, ValueError, RecursionError):
            raise _Uncertain("invalid expression") from None
        count = 0
        stack = [(tree, 0)]
        allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
                   ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.UAdd, ast.USub)
        while stack:
            node, level = stack.pop()
            count += 1
            if count > MAX_AST_NODES or level > MAX_AST_DEPTH or not isinstance(node, allowed):
                raise _Uncertain("syntax or resource bound")
            stack.extend((child, level + 1) for child in ast.iter_child_nodes(node))
        return self._visit(tree.body)

    def _spend(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise _Uncertain("operation bound")

    def _number(self, node: ast.Constant) -> Fraction:
        if type(node.value) not in (int, float):
            raise _Uncertain("non-numeric literal")
        token = ast.get_source_segment(self.source, node) or ""
        if len(token) > 128 or not _NUMBER.fullmatch(token):
            raise _Uncertain("number format or bound")
        try:
            number = Decimal(token)
            parts = number.as_tuple()
            if not number.is_finite() or abs(parts.exponent) > 128 or len(parts.digits) > 128:
                raise _Uncertain("number bound")
            return _checked(Fraction(number))
        except (InvalidOperation, ValueError, OverflowError):
            raise _Uncertain("invalid number") from None

    def _add(self, left: _Polynomial, right: _Polynomial, sign: int = 1) -> _Polynomial:
        result = dict(left)
        for powers, coefficient in right.items():
            self._spend()
            value = _checked(result.get(powers, Fraction(0)) + coefficient * sign)
            if value:
                result[powers] = value
            else:
                result.pop(powers, None)
            if len(result) > MAX_TERMS:
                raise _Uncertain("term bound")
        return result

    def _multiply(self, left: _Polynomial, right: _Polynomial) -> _Polynomial:
        result: _Polynomial = {}
        for left_powers, left_value in left.items():
            for right_powers, right_value in right.items():
                self._spend()
                powers = dict(left_powers)
                for name, exponent in right_powers:
                    powers[name] = powers.get(name, 0) + exponent
                if sum(powers.values()) > MAX_DEGREE:
                    raise _Uncertain("degree bound")
                key = tuple(sorted(powers.items()))
                value = _checked(result.get(key, Fraction(0)) + _checked(left_value * right_value))
                if value:
                    result[key] = value
                else:
                    result.pop(key, None)
                if len(result) > MAX_TERMS:
                    raise _Uncertain("term bound")
        return result

    def _visit(self, node: ast.AST) -> _Polynomial:
        self._spend()
        if isinstance(node, ast.Constant):
            return _constant(self._number(node))
        if isinstance(node, ast.Name):
            if not _NAME.fullmatch(node.id) or node.id in {"True", "False", "None", "nan", "NaN", "inf", "Infinity"}:
                raise _Uncertain("unsupported variable")
            self.variables.add(node.id)
            if len(self.variables) > MAX_VARIABLES:
                raise _Uncertain("variable bound")
            return {((node.id, 1),): Fraction(1)}
        if isinstance(node, ast.UnaryOp):
            value = self._visit(node.operand)
            return {key: -coefficient for key, coefficient in value.items()} if isinstance(node.op, ast.USub) else value
        if not isinstance(node, ast.BinOp):
            raise _Uncertain("unsupported expression")
        left, right = self._visit(node.left), self._visit(node.right)
        if isinstance(node.op, ast.Add):
            return self._add(left, right)
        if isinstance(node.op, ast.Sub):
            return self._add(left, right, -1)
        if isinstance(node.op, ast.Mult):
            return self._multiply(left, right)
        if isinstance(node.op, ast.Div):
            divisor = _constant_value(right)
            if divisor is None or not divisor:
                raise _Uncertain("unproved division domain")
            return {key: _checked(coefficient / divisor) for key, coefficient in left.items()}
        exponent = _constant_value(right)
        if exponent is None or exponent.denominator != 1 or abs(exponent) > MAX_EXPONENT:
            raise _Uncertain("unproved power or exponent bound")
        power = exponent.numerator
        base = _constant_value(left)
        if power <= 0:
            if base is None or not base:
                raise _Uncertain("unproved zero/negative power domain")
            return _constant(_checked(base**power))
        if any(sum(dict(key).values()) * power > MAX_DEGREE for key in left):
            raise _Uncertain("degree bound")
        result = _constant(Fraction(1))
        while power:
            if power & 1:
                result = self._multiply(result, left)
            power //= 2
            if power:
                left = self._multiply(left, left)
        return result


def expressions_equivalent(left: str, right: str) -> bool | None:
    """True/False is a proof for supported real polynomials; None needs review."""
    try:
        first, second = _Parser(left), _Parser(right)
        a, b = first.parse(), second.parse()
        if len(first.variables | second.variables) > MAX_VARIABLES:
            return None
        return a == b
    except (_Uncertain, ArithmeticError, RecursionError):
        return None


# A descriptive alias for callers that prefer compare terminology.
compare_expressions = expressions_equivalent


def _result(status: str, units: int, why: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "score_units": units, "why": why, **extra}


def _units(value: Any, *, points: bool = False) -> int:
    if not points:
        if type(value) is not int or not 0 <= value <= MAX_SCORE_UNITS:
            raise ValueError("score_units must be a bounded non-negative integer")
        return value
    if type(value) not in (int, float, str, Decimal) or isinstance(value, bool):
        raise ValueError("score must be finite")
    if isinstance(value, str) and len(value) > 64:
        raise ValueError("score bound")
    try:
        decimal = Decimal(str(value))
        parts = decimal.as_tuple()
        if not decimal.is_finite() or len(parts.digits) > 64 or abs(parts.exponent) > 64:
            raise ValueError("score must be bounded and finite")
        # Fraction preserves sub-unit tails; Decimal's ambient precision could
        # otherwise round 2.000...0001 to a deceptively integral 200 units.
        scaled = Fraction(decimal) * SCORE_UNITS_PER_POINT
        if not 0 <= scaled <= MAX_SCORE_UNITS:
            raise ValueError("score range")
        if scaled.denominator != 1:
            raise ValueError("score precision")
        return scaled.numerator
    except (InvalidOperation, OverflowError):
        raise ValueError("invalid score") from None


def _score(entry: Mapping[str, Any], *, default: int | None = None) -> int:
    if "score_units" in entry:
        return _units(entry["score_units"])
    if "max_score_units" in entry:
        return _units(entry["max_score_units"])
    if "score" in entry:
        return _units(entry["score"], points=True)
    if default is None:
        raise ValueError("missing score")
    return default


def _question_type(question: Mapping[str, Any]) -> str:
    value = question.get("type", question.get("question_type"))
    if not isinstance(value, str):
        raise ValueError("invalid question type")
    return {"multiple_choice": "multi_choice", "true_false": "judge", "numeric": "math", "expression": "math"}.get(value, value)


def _options(question: Mapping[str, Any]) -> int:
    options = question.get("options")
    if not isinstance(options, list) or not 2 <= len(options) <= 32 or any(not isinstance(value, str) or not value.strip() for value in options):
        raise ValueError("invalid options")
    return len(options)


def _check_answer(kind: str, question: Mapping[str, Any], answer: Any, *, reference: bool = False) -> None:
    if kind == "single_choice":
        count = _options(question)
        if type(answer) is not int or not 0 <= answer < count:
            raise ValueError("单选答案必须为有效的整数选项下标")
    elif kind == "multi_choice":
        count = _options(question)
        if not isinstance(answer, list) or not answer or len(answer) > count or any(type(value) is not int or not 0 <= value < count for value in answer) or len(set(answer)) != len(answer):
            raise ValueError("多选答案必须为不重复的有效整数下标数组")
    elif kind == "judge":
        if type(answer) is not bool:
            raise ValueError("判断答案必须为布尔值")
    elif kind in {"fill", "math", "short_answer", "case"}:
        values = answer if reference and kind == "fill" and isinstance(answer, list) else [answer]
        if not values or len(values) > 32 or any(not isinstance(value, str) or not value.strip() or len(value) > 8_000 for value in values):
            raise ValueError("答案必须为有界非空文本")
    else:
        raise ValueError("题型需要人工处理")


def validate_answer(question: Mapping[str, Any], answer: Any) -> dict[str, Any]:
    """Validate shape only; ``valid=True`` does not claim the answer is correct.

    Results always include status/score_units/why plus the explicit ``valid``
    boolean. Invalid types are manual_pending instead of coerced to zero marks.
    """
    try:
        if not isinstance(question, Mapping):
            raise ValueError("题目结构无效")
        _score(question, default=SCORE_UNITS_PER_POINT)
        _check_answer(_question_type(question), question, answer)
    except ValueError as error:
        return _result("manual_pending", 0, str(error), valid=False)
    return _result("graded", 0, "答案类型校验通过，尚未判分", valid=True)


def grade_objective(question: Mapping[str, Any], answer: Any) -> dict[str, Any]:
    """No AI calls: prove objective equality or explicitly defer the decision.

    Prefer integer ``question.score_units``. Legacy ``score`` points are
    converted exactly at 100 units/point. ``uncertain`` and ``manual_pending``
    carry a zero placeholder; callers must not treat it as an earned zero.
    Text-only fill questions can explicitly set ``answer_format='text'`` for
    exact, case-sensitive trimmed comparison; no keyword scoring is available.
    """
    validation = validate_answer(question, answer)
    if not validation["valid"]:
        return _result("manual_pending", 0, validation["why"])
    kind = _question_type(question)
    maximum = _score(question, default=SCORE_UNITS_PER_POINT)
    if kind in {"short_answer", "case"}:
        return _result("manual_pending", 0, "主观题需要 AI 按评分量表判分或教师复核")
    reference = question.get("answer", question.get("reference_answer"))
    try:
        _check_answer(kind, question, reference, reference=True)
    except ValueError:
        return _result("manual_pending", 0, "参考答案无效，需教师复核")
    if kind in {"single_choice", "judge"}:
        correct = answer == reference
    elif kind == "multi_choice":
        correct = set(answer) == set(reference)
    else:
        if question.get("answer_format") != "text":
            # A restricted or discrete domain can make unequal real
            # polynomials equivalent. This module proves no such restriction.
            for key in ("domain", "variables", "assumptions", "constraints"):
                value = question.get(key)
                if value not in (None, "", "real", "reals", "R", "ℝ"):
                    return _result("uncertain", 0, "题目包含尚未证明的定义域或变量约束，交 AI 或教师复核")
        references = reference if isinstance(reference, list) else [reference]
        if question.get("answer_format") == "text":
            correct = any(answer.strip() == value.strip() for value in references)
        else:
            decisions = [expressions_equivalent(answer, value) for value in references]
            if True in decisions:
                correct = True
            elif any(value is None for value in decisions):
                return _result("uncertain", 0, "无法在安全表达式和定义域范围内证明等价，交 AI 或教师复核")
            else:
                correct = False
    return _result("graded", maximum if correct else 0, "答案确定等价" if correct else "答案确定不等价")


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > 64_000:
        raise ValueError("invalid rubric result")

    def pairs(entries):
        result = {}
        for key, item in entries:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    def constant(_):
        raise ValueError("non-finite JSON number")

    result = json.loads(value, object_pairs_hook=pairs, parse_constant=constant, parse_float=Decimal)
    if not isinstance(result, dict):
        raise ValueError("rubric result must be an object")
    return result


def validate_ai_rubric(question: Mapping[str, Any], result: Any) -> dict[str, Any]:
    """Validate AI per-point partial credit without clamping or keyword fallback.

    ``points`` must match the rubric in order and label, with bounded exact
    ``score_units`` (or legacy decimal ``score``), optional boolean ``matched``
    and optional text ``why``. An optional overall score must equal the sum.
    Invalid/ambiguous output is manual_pending, never silently awarded marks.
    """
    try:
        if not isinstance(question, Mapping):
            raise ValueError("invalid question")
        maximum = _score(question, default=SCORE_UNITS_PER_POINT)
        rubric = question.get("rubric")
        if not isinstance(rubric, list) or not 1 <= len(rubric) <= 32:
            raise ValueError("invalid rubric")
        payload = _json_object(result)
        points = payload.get("points")
        why = payload.get("why")
        if not isinstance(points, list) or len(points) != len(rubric) or not isinstance(why, str) or not why.strip() or len(why) > 2_000:
            raise ValueError("invalid rubric result shape")
        per_point = []
        labels: set[str] = set()
        total = possible = 0
        for expected, supplied in zip(rubric, points):
            if not isinstance(expected, Mapping) or not isinstance(supplied, Mapping):
                raise ValueError("invalid rubric point")
            label = expected.get("point")
            if not isinstance(label, str) or not label.strip() or len(label) > 1_000 or label in labels or supplied.get("point") != label:
                raise ValueError("point mismatch")
            labels.add(label)
            cap = _score(expected)
            if "max_score_units" in supplied or ("score_units" in supplied and "score" in supplied):
                raise ValueError("ambiguous point score")
            earned = _score(supplied)
            if earned > cap or ("matched" in supplied and type(supplied["matched"]) is not bool):
                raise ValueError("point score outside bounds")
            explanation = supplied.get("why", "")
            if not isinstance(explanation, str) or len(explanation) > 2_000:
                raise ValueError("invalid point explanation")
            total += earned
            possible += cap
            per_point.append({"point": label, "score_units": earned, "max_score_units": cap, "why": explanation})
        if possible > maximum or total > maximum:
            raise ValueError("rubric total outside question bounds")
        if "score_units" in payload and "score" in payload:
            raise ValueError("ambiguous total")
        if any(key in payload for key in ("score_units", "score", "max_score_units")) and _score(payload) != total:
            raise ValueError("total does not equal per-point sum")
        return _result("graded", total, why, per_point=per_point, valid=True)
    except (ValueError, TypeError, InvalidOperation, OverflowError, RecursionError):
        return _result("manual_pending", 0, "AI 评分结构、分点或分值无效，等待重新评分或教师复核", per_point=[], valid=False)


parse_rubric_grade = validate_ai_rubric
