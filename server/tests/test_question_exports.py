"""Complete packets preserve exact versions without exposing account data."""
from copy import deepcopy
import csv
import hashlib
from io import StringIO
import json
import secrets
import unittest

from app.services.question_exports import build_packet, canonical_json, export_questions, selection_hash


def detail(identifier="q-1", version=2):
    private_marker = secrets.token_hex(24)
    return {"question": {"question_id": identifier, "version": version, "status": "approved", "course_id": "course",
            "owner_id": "private-owner", "content": {"type": "text", "stem": "解释 a(b+c)=ab+ac。\n请举例。", "options": [],
            "answer": "乘法对加法满足分配律。", "explanation": "两项都须乘以 a。", "points": 2,
            "rubric": [{"point": "说明两项分别相乘", "score_units": 200}], "difficulty": "medium", "answer_format": "math",
            "knowledge_points": ["K1"], "student_submission": private_marker,
            "provenance": {"origin": "generated", "filename": "赛方资料.csv", "row_number": 4, "cookie": private_marker}}},
            "sources": [{"block_id": "block", "anchor": "M12 / 第4条", "sha256": "source-hash", "source_filename": "赛方资料.csv",
                         "text": private_marker, "export_allowed": False, "server_path": private_marker}],
            "reviews": {"independent_solution": {"answer": "乘法分配律", "solvable": True}, "recheck_count": 0,
                        "calls": {"review": {"model": "declared-model", "request_id": "call-id", "usage": None,
                                             "private_marker": private_marker, "raw_response": private_marker}}},
            "usage": {"student_ids": [private_marker]}, "versions": [{"version": 1}]}


class QuestionExportTests(unittest.TestCase):
    def test_exact_order_identity_full_content_and_privacy(self):
        values = [detail("q-second", 3), detail("q-first", 1)]
        before = deepcopy(values)
        data, media, filename = export_questions(values, "json")
        packet = json.loads(data)
        self.assertEqual(packet["selection"], [{"question_id": "q-second", "version": 3}, {"question_id": "q-first", "version": 1}])
        self.assertEqual(packet["question_count"], 2)
        self.assertEqual(packet["selection_sha256"], selection_hash(packet["selection"]))
        digest = packet.pop("packet_sha256")
        self.assertEqual(digest, hashlib.sha256(canonical_json(packet).encode()).hexdigest())
        question = packet["questions"][0]
        self.assertEqual(question["content"]["answer"], values[0]["question"]["content"]["answer"])
        self.assertEqual(question["content"]["rubric"][0]["score_units"], 200)
        self.assertEqual(question["sources"][0]["anchor"], "M12 / 第4条")
        self.assertIsNone(question["reviews"]["calls"]["review"]["usage"])
        for value in values:
            self.assertNotIn(value["question"]["content"]["student_submission"].encode(), data)
        self.assertEqual(values, before)
        self.assertEqual(media, "application/json")
        self.assertTrue(filename.endswith(".json"))
        self.assertEqual(data, export_questions(values, "json")[0])
        self.assertNotEqual(export_questions(list(reversed(values)), "json")[0], data)

    def test_source_body_requires_explicit_export_permission(self):
        value = detail()
        for flag in (None, False, "true", 1):
            value["sources"][0]["export_allowed"] = flag
            self.assertNotIn("text", build_packet([value])["questions"][0]["sources"][0])
        value["sources"][0]["export_allowed"] = True
        self.assertEqual(build_packet([value])["questions"][0]["sources"][0]["text"], value["sources"][0]["text"])

    def test_csv_roundtrip_and_formula_safety_preserve_json_original(self):
        value = detail()
        value["question"]["content"]["stem"] = '=HYPERLINK("https://example.invalid","题目")\n第二行,含逗号'
        data, media, _ = export_questions([value], "csv")
        row = list(csv.DictReader(StringIO(data.decode("utf-8-sig"))))[0]
        self.assertTrue(row["stem"].startswith("'="))
        self.assertEqual(row["stem"][1:], value["question"]["content"]["stem"])
        self.assertEqual(json.loads(row["rubric"])[0]["score_units"], 200)
        self.assertEqual(json.loads(row["sources"])[0]["anchor"], "M12 / 第4条")
        self.assertEqual(row["answer"], value["question"]["content"]["answer"])
        self.assertEqual(row["selection_sha256"], build_packet([value])["selection_sha256"])
        self.assertTrue(media.startswith("text/csv"))
        self.assertEqual(json.loads(export_questions([value], "json")[0])["questions"][0]["content"]["stem"], value["question"]["content"]["stem"])

    def test_html_has_readable_answer_rubric_source_and_escaped_text(self):
        value = detail()
        value["question"]["content"]["stem"] = '<script>alert("bad")</script> 数学题'
        data, media, _ = export_questions([value], "html")
        text = data.decode()
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        for expected in ("参考答案", "评分细则", "说明两项分别相乘", "2 分", "M12 / 第4条", "declared-model"):
            self.assertIn(expected, text)
        self.assertIn("Content-Security-Policy", text)
        self.assertNotIn(value["question"]["content"]["student_submission"], text)
        self.assertTrue(media.startswith("text/html"))

    def test_no_partial_export_for_invalid_selection(self):
        invalid = [[], [detail(), detail()], [detail(), {"question": {}}], [detail(version=True)],
                   [detail(version=0)], [detail(str(index)) for index in range(101)]]
        missing = detail()
        missing["question"]["content"].pop("answer")
        invalid.append([missing])
        for values in invalid:
            with self.subTest(values=len(values)), self.assertRaises(ValueError):
                export_questions(values, "json")
        with self.assertRaises(ValueError):
            export_questions([detail()], "pdf")


if __name__ == "__main__":
    unittest.main()
