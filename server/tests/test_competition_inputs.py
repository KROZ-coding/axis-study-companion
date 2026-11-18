from __future__ import annotations

import csv
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from app.services.competition_inputs import (CompetitionInputError, REQUIRED_TABLES, TABLES,
                                             input_preview, parse_organizer_inputs)

SERVER = Path(__file__).resolve().parents[1]
PACKAGE = SERVER.parent / "赛题数据" / "赛题资料包"
OFFICIAL_DIR = PACKAGE / "4_赛题数据" / "模拟学情数据"


def actual_csv_inputs():
    inputs = {path.name: path.read_bytes() for path in OFFICIAL_DIR.glob("*.csv")}
    if len(inputs) < 6:
        raise unittest.SkipTest("restricted organizer CSV files are local acceptance inputs, not repository fixtures")
    return inputs


def workbook_input():
    path = PACKAGE / "4_赛题数据" / "模拟学情数据索引.xlsx"
    if not path.exists():
        raise unittest.SkipTest("organizer workbook not present")
    return {path.name: path.read_bytes()}


def csv_texts(inputs=None):
    result = {}
    for filename, content in (inputs or actual_csv_inputs()).items():
        result[filename] = content.decode("utf-8-sig")
    return result


class CompetitionInputsTests(unittest.TestCase):
    def test_actual_csv_field_dictionary_row_and_revision_counts(self):
        parsed = parse_organizer_inputs(actual_csv_inputs())
        summary = input_preview(parsed)["summary"]
        self.assertEqual(summary["students"], 40)
        self.assertEqual(summary["classes"], 2)
        self.assertEqual(summary["table_rows"]["作答记录"], 806)
        self.assertEqual(summary["current_events"], 799)
        self.assertEqual(summary["superseded_records"], 7)
        self.assertEqual(summary["eligible_scored_events"], 785)
        self.assertEqual(len(parsed["tables"]["字段说明"]), 42)
        self.assertEqual(sum(issue["code"] == "unmapped_textbook" for issue in parsed["issues"]), 8)
        self.assertTrue(any(issue["code"] == "index_notes_absent" for issue in parsed["issues"]))
        preview = input_preview(parsed)
        self.assertNotIn("4a+7b", str(preview))
        self.assertNotIn("=4a-7b", str(preview))
        self.assertEqual({row["id"] for row in input_preview(parsed)["options"]["nodes"]}, {"N01", "N02"})

    def test_actual_xlsx_accepts_index_notes_and_formula_cells_are_never_evaluated(self):
        parsed = parse_organizer_inputs(workbook_input())
        self.assertEqual(input_preview(parsed)["summary"]["current_events"], 799)
        self.assertEqual(len(parsed["tables"]["数据说明"]), 11)
        self.assertFalse(any(issue["code"] == "index_notes_absent" for issue in parsed["issues"]))

    def test_known_table_headers_are_order_insensitive_but_not_guessable(self):
        texts = csv_texts()
        rows = list(csv.reader(io.StringIO(texts["课程要求.csv"])))
        order = list(reversed(range(len(rows[0]))))
        texts["课程要求.csv"] = "\ufeff" + "\n".join(
            ",".join(row[index] for index in order) for row in rows
        )
        parsed = parse_organizer_inputs({name: text.encode("utf-8") for name, text in texts.items()})
        self.assertEqual(len(parsed["tables"]["课程要求"]), 9)
        texts["课程要求.csv"] = texts["课程要求.csv"].replace("主题编号", "人工猜测主题", 1)
        with self.assertRaises(CompetitionInputError):
            parse_organizer_inputs({name: text.encode("utf-8") for name, text in texts.items()})

    def test_file_name_byte_row_and_mixed_duplicate_bounds(self):
        inputs = actual_csv_inputs()
        for unsafe in ("../作答记录.csv", "C:\\作答记录.csv", "作答记录.csv/evil"):
            with self.subTest(name=unsafe), self.assertRaises(CompetitionInputError):
                parse_organizer_inputs({**inputs, unsafe: inputs["作答记录.csv"]})
        with self.assertRaises(CompetitionInputError):
            parse_organizer_inputs({"课程要求.csv": b"x" * (12 * 1024 * 1024 + 1)})
        with self.assertRaises(CompetitionInputError):
            parse_organizer_inputs({"mystery.csv": b"a\n1"})
        # Identical CSV/workbook copies cannot double the statistical denominator.
        mixed = {**inputs, **workbook_input()}
        parsed = parse_organizer_inputs(mixed)
        self.assertEqual(input_preview(parsed)["summary"]["current_events"], 799)

    def test_csv_parse_errors_do_not_echo_uploaded_text(self):
        inputs = actual_csv_inputs()
        inputs["作答记录.csv"] = inputs["作答记录.csv"] + b'"secret\n'
        with self.assertRaises(CompetitionInputError) as caught:
            parse_organizer_inputs(inputs)
        self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
