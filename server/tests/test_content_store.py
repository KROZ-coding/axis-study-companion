"""Relational content safety, partial confirmation, source gates and outbox regressions."""
from contextlib import closing
from pathlib import Path
import hashlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.content_store import ContentConflict, ContentStore
from app.services.material_parser import parse_material, prepare_input, apply_result


class ContentStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="content-store-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/"content.sqlite3"
        self.store = ContentStore(self.path)

    def _import(self, text="first\n\nsecond", parsed=None, **kwargs):
        content = text.encode()
        values = {"course_id":"course","owner_id":"teacher","filename":"book.md","content":content,
                  "parsed":parsed or parse_material("book.md",content),"license_code":"own","rights_confirmed":True,
                  "allow_generation":True,"allow_retrieval":True,"allow_student_view":True,"external_processing_allowed":True}
        values.update(kwargs)
        return self.store.import_material(**values)

    def _confirm(self, material, blocks=None, **kwargs):
        blocks = blocks or self.store.list_blocks(material["id"])
        return self.store.confirm_blocks(material["id"],[b["id"] for b in blocks],actor_id="teacher",expected_revision=material["revision"],**kwargs)

    def test_schema_wal_foreign_keys_and_reopen_are_explicit(self):
        self.assertEqual(self.store.schema_version,1)
        self.assertEqual(self.store.journal_mode,"wal")
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0],1)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"material_files","import_batches","source_blocks","material_issues","confirmations","confirmation_blocks","course_requirements","knowledge_points","knowledge_prerequisites","content_jobs"} <= tables)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO requirement_blocks VALUES ('missing','missing')")
        self.assertEqual(ContentStore(self.path).schema_version,1)

    def test_newer_and_unversioned_foreign_databases_are_rejected_without_modification(self):
        for name,version in (("future",99),("foreign",0)):
            path = Path(self.directory.name)/(name+".sqlite3")
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE foreign_data(value TEXT)")
                db.execute(f"PRAGMA user_version={version}")
                db.commit()
            before = path.read_bytes()
            with self.assertRaises(RuntimeError):
                ContentStore(path)
            self.assertEqual(path.read_bytes(),before)

    def test_originals_use_new_private_directories_hashes_and_never_client_paths(self):
        first,second = self._import(),self._import()
        self.assertNotEqual(first["storage_key"],second["storage_key"])
        self.assertEqual(self.store.original_bytes(first["id"]),b"first\n\nsecond")
        self.assertEqual(first["sha256"],hashlib.sha256(b"first\n\nsecond").hexdigest())
        self.assertEqual((self.store.originals_dir/first["storage_key"]).name,"original.bin")
        (self.store.originals_dir/first["storage_key"]).write_bytes(b"tampered")
        with self.assertRaises(ContentConflict):
            self.store.original_bytes(first["id"])
        with self.assertRaises(ValueError):
            self._import(filename="../escape.md")

    def test_original_is_cleaned_up_when_relational_write_fails(self):
        before = set(self.store.originals_dir.glob("*"))
        with patch.object(self.store,"_insert_parse",side_effect=ValueError("invalid block")),self.assertRaises(ValueError):
            self._import()
        self.assertEqual(set(self.store.originals_dir.glob("*")),before)
        self.assertEqual(self.store.list_materials("course"),[])

    def test_partial_confirmation_is_all_or_nothing_and_only_qualified_blocks_are_usable(self):
        parsed = {"status":"parsed","format":"md","blocks":[
            {"anchor":"p:1","kind":"text","text":"qualified","data":{}},
            {"anchor":"p:2","kind":"text","text":"uncertain","data":{}}],
            "issues":[{"anchor":"p:2","code":"uncertain","message":"check","severity":"blocking"}]}
        material = self._import(parsed=parsed)
        good,bad = self.store.list_blocks(material["id"])
        with self.assertRaises(PermissionError):
            self.store.confirmed_sources("course",[good["id"]],purpose="generation")
        with self.assertRaises(ContentConflict):
            self._confirm(material,[good,bad])
        self.assertTrue(all(b["confirmed_by"] is None for b in self.store.list_blocks(material["id"])))
        result = self._confirm(material,[good])
        self.assertEqual(result["material"]["status"],"partial")
        self.assertEqual(self.store.confirmed_sources("course",[good["id"]],purpose="generation")[0]["anchor"],"p:1")
        with self.assertRaises(PermissionError):
            self.store.confirmed_sources("course",[good["id"],bad["id"]],purpose="generation")
        issue = self.store.list_issues(material["id"])[0]
        updated = self.store.resolve_issue(material["id"],issue["id"],actor_id="teacher",expected_revision=result["material"]["revision"],resolution="human verified")
        self.assertEqual(self.store.list_blocks(material["id"])[1]["status"],"draft")
        final = self._confirm(updated,[self.store.list_blocks(material["id"])[1]])
        self.assertEqual(final["material"]["status"],"confirmed")
        self.assertEqual(self.store.list_blocks(material["id"])[0]["text"],"qualified")
        with self.assertRaises(ContentConflict):
            self.store.update_draft(material["id"],actor_id="teacher",expected_revision=final["material"]["revision"],title="rewrite")

    def test_permissions_and_optimistic_revision_cannot_be_coerced(self):
        with self.assertRaises(ValueError):
            self._import(external_processing_allowed="denied")
        material = self._import(license_code="unknown",rights_confirmed=False)
        with self.assertRaises(ValueError):
            self._confirm(material)
        with self.assertRaises(PermissionError):
            self.store.update_draft(material["id"],actor_id="other",expected_revision=1,title="changed")
        material = self.store.update_draft(material["id"],actor_id="teacher",expected_revision=1,license_code="own",rights_confirmed=True)
        self.assertEqual(material["revision"],2)
        self.assertTrue(all(i["status"]=="resolved" for i in self.store.list_issues(material["id"])))
        with self.assertRaises(ContentConflict):
            self.store.update_draft(material["id"],actor_id="teacher",expected_revision=1,title="stale")
        self._confirm(material)

    def test_shared_owner_confirmation_public_attestation_and_answer_projection(self):
        parsed = {"status":"parsed","format":"txt","blocks":[{"anchor":"p:1","kind":"text","text":"public lesson"},{"anchor":"p:2","kind":"answer","text":"secret answer"}],"issues":[]}
        material = self._import(parsed=parsed,visibility="shared")
        public,answer = self.store.list_blocks(material["id"])
        with self.assertRaises(PermissionError):
            self._confirm(material,[public])
        with self.assertRaises(ValueError):
            self._confirm(material,[public],course_owner_confirmation=True,public_block_ids=[public["id"]])
        with self.assertRaises(ValueError):
            self._confirm(material,[answer],course_owner_confirmation=True,public_block_ids=[answer["id"]],public_content_attested=True)
        self._confirm(material,[public,answer],course_owner_confirmation=True,public_block_ids=[public["id"]],public_content_attested=True)
        student = self.store.list_blocks(material["id"],student=True)
        self.assertEqual([b["text"] for b in student],["public lesson"])
        self.assertEqual(self.store.search_confirmed("course","secret",student=True),[])

    def test_class_scope_license_purpose_and_literal_search(self):
        material = self._import(text="value 100% lesson",class_id="class-a",allow_generation=False)
        self._confirm(material)
        block = self.store.list_blocks(material["id"])[0]
        with self.assertRaises(PermissionError):
            self.store.confirmed_sources("course",[block["id"]],purpose="retrieval")
        with self.assertRaises(PermissionError):
            self.store.confirmed_sources("course",[block["id"]],purpose="generation",class_id="class-a")
        self.assertEqual(len(self.store.confirmed_sources("course",[block["id"]],purpose="retrieval",class_id="class-a")),1)
        self.assertEqual(self.store.search_confirmed("course","100%",class_id="class-a")[0]["id"],block["id"])
        self.assertEqual(self.store.search_confirmed("course","_",class_id="class-a"),[])
        self.assertEqual(self.store.search_confirmed("course","lesson",class_id="class-b"),[])

    def test_manual_ocr_transcription_requires_human_text_and_keeps_anchor_and_original_hash(self):
        content = b"\x89PNG\r\n\x1a\nprivate-image"
        material = self.store.import_material(course_id="course",owner_id="teacher",filename="scan.png",content=content,parsed=parse_material("scan.png",content),license_code="own",rights_confirmed=True,allow_retrieval=True)
        block = self.store.list_blocks(material["id"])[0]
        issue = self.store.list_issues(material["id"])[0]
        with self.assertRaises(ValueError):
            self.store.resolve_issue(material["id"],issue["id"],actor_id="teacher",expected_revision=1,resolution="looked at it")
        updated = self.store.resolve_issue(material["id"],issue["id"],actor_id="teacher",expected_revision=1,resolution="transcribed",replacement_text="人工转录正文")
        self._confirm(updated)
        after = self.store.list_blocks(material["id"])[0]
        self.assertEqual(after["anchor"],block["anchor"])
        self.assertNotEqual(after["sha256"],block["sha256"])
        self.assertTrue(after["data"]["manual_transcription"])
        self.assertEqual(self.store.get_material(material["id"])["sha256"],hashlib.sha256(content).hexdigest())

    def test_curriculum_sources_foreign_keys_and_prerequisite_cycle(self):
        material = self._import(); self._confirm(material)
        source_ids = [self.store.list_blocks(material["id"])[0]["id"]]
        requirement = self.store.add_requirement("course","掌握基础",created_by="teacher",source_block_ids=source_ids)
        self.assertEqual(self.store.list_requirements("course"),[])
        self.store.confirm_curriculum("requirement",requirement["id"],actor_id="owner")
        self.assertEqual(len(self.store.list_requirements("course")),1)
        points = [self.store.add_knowledge_point("course",str(i),f"K{i}",created_by="teacher",source_block_ids=source_ids) for i in range(3)]
        self.store.set_prerequisites(points[1]["id"],[points[0]["id"]],actor_id="teacher")
        self.store.set_prerequisites(points[2]["id"],[points[1]["id"]],actor_id="teacher")
        with self.assertRaises(ValueError):
            self.store.set_prerequisites(points[0]["id"],[points[2]["id"]],actor_id="teacher")
        with self.assertRaises(ContentConflict):
            self.store.confirm_curriculum("knowledge",points[1]["id"],actor_id="owner")
        for point in points:
            self.store.confirm_curriculum("knowledge",point["id"],actor_id="owner")
        with self.assertRaises(ContentConflict):
            self.store.set_prerequisites(points[2]["id"],[],actor_id="teacher")

    def test_worker_generation_and_lease_guards_reject_old_callbacks(self):
        job = self.store.create_job("question_generate",course_id="course",owner_id="teacher",payload={})
        bound = self.store.bind_job_generation(job["id"],1)
        self.assertEqual(bound["state"]["worker_generation"],1)
        self.assertGreater(bound["revision"],job["revision"])
        rebound = self.store.bind_job_generation(job["id"],2)
        with self.assertRaises(ContentConflict):
            self.store.bind_job_generation(job["id"],1)
        with self.assertRaises(ContentConflict):
            self.store.advance_job(job["id"],expected_revision=rebound["revision"],expected_generation=1,
                                   stage="review",status="pending",state={})
        with self.assertRaises(ContentConflict):
            self.store.advance_job(job["id"],expected_revision=rebound["revision"],expected_generation=2,
                                   stage="review",status="pending",state={},is_current=lambda:False)
        self.assertEqual(self.store.get_job(job["id"])["stage"],"generate")

    def test_durable_outbox_callbacks_reject_stale_and_terminal_results(self):
        material = self._import()
        job = self.store.create_job("material_parse",course_id="course",owner_id="teacher",payload={"file_id":material["id"],"material_revision":1})
        self.assertEqual([j["id"] for j in self.store.list_pending_jobs()],[job["id"]])
        prepared = prepare_input(self.store,job["id"])
        self.assertEqual(prepared["content"],b"first\n\nsecond")
        result = apply_result(self.store,job["id"],parse_material(prepared["filename"],prepared["content"]),expected_revision=prepared["revision"])
        self.assertEqual(result["status"],"completed")
        self.assertEqual(self.store.list_pending_jobs(),[])
        with self.assertRaises(ContentConflict):
            apply_result(self.store,job["id"],parse_material("book.md",b"stale"),expected_revision=prepared["revision"])
        self.assertEqual(self.store.list_blocks(material["id"])[0]["text"],"first")


if __name__=="__main__":
    unittest.main()
