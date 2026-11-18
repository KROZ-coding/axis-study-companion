from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zipfile import ZipFile

from app.services.competition_inputs import (CompetitionInputError, TABLES, input_preview,
                                             parse_organizer_inputs)
from app.services.competition_runs import CompetitionConflict, CompetitionRunError, CompetitionRunStore, export_run
from test_competition_inputs import actual_csv_inputs, workbook_input


class CompetitionRunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="competition-runs-")
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "competition.sqlite3"
        self.store = CompetitionRunStore(self.db_path)
        try:
            self.parsed = parse_organizer_inputs(workbook_input())
        except unittest.SkipTest:
            self.parsed = parse_organizer_inputs(actual_csv_inputs())
        self.preview = self.store.create_preview("course-a", "teacher-a", "Organized run", self.parsed)

    def test_versioned_store_immutable_scoped_idempotent_confirm_and_exports(self):
        scope = {"node_ids": ["N02"], "class_ids": ["DC01"], "groups": [
            {"id": "practice-set", "label": "教师练习组", "student_ids": ["D01", "D02"]}],
                 "representative_student_ids": ["D01"], "material_mappings": [
                     {"material_id": "B02", "topic_id": "T03", "indicator_ids": ["T03I1"]}]}
        confirmed = self.store.confirm(self.preview["id"], "course-a", "teacher-a", "Organized run", 1, scope)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["result"]["sections"]["representative_cases"][0]["student_id"], "D01")
        self.assertNotIn("D02", str(confirmed["result"]["sections"]["representative_cases"]))
        selected_group = next(row for row in confirmed["result"]["sections"]["class_and_selected_group_performance"] if row["group_kind"] == "selected_group")
        self.assertEqual(selected_group["student_count_denominator"], 2)
        self.assertEqual(selected_group["student_ids"], ["D01", "D02"])
        same = self.store.confirm(self.preview["id"], "course-a", "teacher-a", "Organized run", 1, scope)
        self.assertEqual(same["result"], confirmed["result"])
        with self.assertRaises(CompetitionConflict):
            self.store.confirm(self.preview["id"], "course-a", "teacher-a", "Organized run", 1, {})
        self.assertIsNone(self.store.get("not-a-run"))
        self.assertEqual(self.store.list_course("course-b"), [])
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='competition_runs'").fetchone()[0], "competition_runs")
        finally:
            connection.close()
        for fmt in ("json", "csv", "zip", "html"):
            body, _, _ = export_run(confirmed, fmt)
            self.assertTrue(body)
            if fmt == "zip":
                with ZipFile(io.BytesIO(body)) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertEqual(manifest["run_id"], confirmed["id"])
                    self.assertIn("representative_cases.csv", archive.namelist())
            if fmt == "html":
                self.assertIn(b"Content-Security-Policy", body)
                self.assertIn(b"&lt;", body) if b"<script" in body else None

    def test_large_cells_reassemble_completely_and_archive_hashes_match(self):
        confirmed = self.store.confirm(self.preview["id"], "course-a", "teacher-a", "Organized run", 1, {})
        body, _, _ = export_run(confirmed, "zip")
        import hashlib
        from app.services.competition_runs import _safe_cell
        with ZipFile(io.BytesIO(body)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            for name, expected in manifest["files"].items():
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), expected)
            self.assertEqual(json.loads(archive.read("result.json"))["run"], confirmed["result"])
            chunks = list(csv.DictReader(io.StringIO(archive.read("large_cells.chunks.csv").decode("utf-8-sig"))))
            self.assertTrue(chunks)
            for name in archive.namelist():
                if not name.endswith(".chunks.manifest.json"):
                    continue
                table = name.removesuffix(".chunks.manifest.json")
                for entry in json.loads(archive.read(name)):
                    parts = sorted([r for r in chunks if r["table"] == table and int(r["row"]) == entry["row"] and r["field"] == entry["field"]], key=lambda r: int(r["part"]))
                    self.assertEqual(len(parts), entry["chunks"])
                    restored = "".join(r["value"][1:] if r["escaped"] == "true" else r["value"] for r in parts)
                    self.assertEqual(hashlib.sha256(restored.encode()).hexdigest(), entry["sha256"])
                    self.assertTrue(all(len(r["value"]) <= 12001 for r in parts))
            representative = next(r for r in chunks if r["table"] == "representative_cases" and r["field"] == "evidence")
            original = confirmed["result"]["sections"]["representative_cases"][int(representative["row"]) - 2]["evidence"]
            matching = sorted([r for r in chunks if r["table"] == "representative_cases" and r["row"] == representative["row"] and r["field"] == "evidence"], key=lambda r: int(r["part"]))
            self.assertEqual("".join(r["value"][1:] if r["escaped"] == "true" else r["value"] for r in matching), _safe_cell(original))

    def test_creation_request_survives_reopen_and_rejects_changed_payload(self):
        run = self.store.create_preview("course-a", "teacher-a", "Request", self.parsed, request_id="same-request")
        reopened = CompetitionRunStore(self.db_path)
        self.assertEqual(reopened.create_preview("course-a", "teacher-a", "Request", self.parsed, request_id="same-request"), run)
        self.assertEqual(reopened.get_by_request("course-a", "teacher-a", "same-request"), run)
        self.assertIsNone(reopened.get_by_request("course-a", "teacher-b", "same-request"))
        self.assertIsNone(reopened.get_by_request("course-b", "teacher-a", "same-request"))
        with self.assertRaises(CompetitionConflict):
            reopened.create_preview("course-a", "teacher-a", "Changed", self.parsed, request_id="same-request")
        def denied():
            raise PermissionError("current access revoked")
        with self.assertRaises(PermissionError):
            reopened.create_preview("course-a", "teacher-a", "Denied", self.parsed, request_id="denied", access_check=denied)
        self.assertIsNone(reopened.get_by_request("course-a", "teacher-a", "denied"))
        with self.assertRaises(PermissionError):
            reopened.confirm(run["id"], "course-a", "teacher-a", "Request", 1, {}, access_check=denied)
        self.assertEqual(reopened.get(run["id"])["status"], "preview")

    def test_foreign_database_is_rejected_before_journal_or_version_mutation(self):
        path = Path(self.directory.name) / "foreign.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA application_id=42")
        original = path.read_bytes()
        with self.assertRaises(RuntimeError):
            CompetitionRunStore(path)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(Path(str(path) + "-wal").exists())

    def test_future_database_is_rejected_before_journal_or_version_mutation(self):
        path = Path(self.directory.name) / "future.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA user_version=99")
        original = path.read_bytes()
        with self.assertRaises(RuntimeError):
            CompetitionRunStore(path)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(Path(str(path) + "-wal").exists())

    def test_individual_recommendations_link_case_evidence_and_complete_exports(self):
        confirmed = self.store.confirm(self.preview["id"], "course-a", "teacher-a", "Organized run", 1, {})
        sections = confirmed["result"]["sections"]
        material_ids = {row["material_id"] for row in self.parsed["entities"]["materials"]}
        recommendations = {row["id"]: row for row in sections["individual_learning_recommendations"]}
        self.assertTrue(recommendations)
        for case in sections["representative_cases"]:
            self.assertTrue(case["observations"])
            for identifier in case["recommendation_ids"]:
                recommendation = recommendations[identifier]
                self.assertEqual(recommendation["student_id"], case["student_id"])
                self.assertTrue(recommendation["action"])
                self.assertTrue(recommendation["observation_plan"])
                for reference in recommendation["references"]:
                    self.assertIn(reference["id"], material_ids)
                    self.assertTrue(reference["source_ref"])
        body, _, _ = export_run(confirmed, "html")
        for phrase in ("课程学情分析", "学习要求", "学情表现", "学习建议", "评分", "建议"):
            self.assertIn(phrase, body.decode("utf-8"))
        archive_bytes = export_run(confirmed, "zip")[0]
        self.assertEqual(archive_bytes, export_run(confirmed, "zip")[0])
        with ZipFile(io.BytesIO(archive_bytes)) as archive:
            self.assertTrue({"report.html", "result.json", "field_dictionary.json", "data_notes.json",
                             "input_schema.json", "output_schema.json", "source_permissions.json", "scope.json"} <= set(archive.namelist()))

    def test_invalid_scope_cannot_expand_to_guessed_or_other_class_ids(self):
        for scope in ({"class_ids": ["outside-class"]}, {"node_ids": ["guessed-node"]},
                      {"student_ids": ["D01", "D22"], "class_ids": ["DC01"]},
                      {"student_ids": ["D01"], "class_ids": ["DC01"],
                       "groups": [{"id": "all", "label": "Everything", "student_ids": ["D01", "D39"]}]}):
            with self.subTest(scope=scope), self.assertRaises(CompetitionRunError):
                self.store.confirm(self.preview["id"], "course-a", "teacher-a", "Organized run", 1, scope)
        with self.assertRaises(CompetitionRunError):
            self.store.confirm(self.preview["id"], "course-a", "teacher-a", "Organized run", True, {})

    def test_custom_cohort_denominator_and_one_or_two_auto_representatives(self):
        first = self.store.confirm(self.preview["id"], "course-a", "teacher-a", "Organized run", 1,
                                   {"student_ids": ["D01"]})
        self.assertEqual(first["result"]["summary"]["scope"]["student_count_denominator"], 1)
        self.assertEqual(len(first["result"]["scope"]["representative_student_ids"]), 1)
        suggestion = first["scope"]["auto_suggestions"][0]
        self.assertEqual(suggestion["student_id"], "D01")

    def test_formula_csv_text_is_neutralized_in_all_csv_export_representations(self):
        inputs = actual_csv_inputs()
        rows = list(csv.DictReader(io.StringIO(inputs["课程要求.csv"].decode("utf-8-sig"))))
        rows[0]["观察指标"] = "=HYPERLINK('https://example.invalid','bad')"
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=TABLES["课程要求"], lineterminator="\r\n")
        writer.writeheader(); writer.writerows(rows)
        inputs["课程要求.csv"] = output.getvalue().encode("utf-8")
        parsed = parse_organizer_inputs(inputs)
        run = CompetitionRunStore(Path(self.directory.name) / "formula.sqlite3").create_preview("course", "teacher", "Formula", parsed)
        from app.services.competition_runs import CompetitionRunStore as Store
        run = Store(Path(self.directory.name) / "formula.sqlite3").confirm(run["id"], "course", "teacher", "Formula", 1, {})
        body, _, _ = export_run(run, "zip")
        with ZipFile(io.BytesIO(body)) as archive:
            content = archive.read("requirements.csv").decode("utf-8-sig")
        self.assertIn("'=HYPERLINK", content)


if __name__ == "__main__":
    unittest.main()
