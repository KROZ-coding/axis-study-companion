"""Question pipeline uses fake model results; no network or real gateway is used."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.content_store import ContentConflict, ContentStore
from app.services.material_parser import parse_material
from app.services.question_bank import QuestionBank, QuestionValidationError, question_fingerprints, validate_question


def candidate(answer=1,stem="二加三等于多少？"):
    return {"type":"single_choice","stem":stem,"options":["四","五"],"answer":answer,
            "explanation":"出题者专用答案解释 MARKER_SECRET","points":1,"rubric":[],"source_refs":["B1"]}


class FakeAuthority:
    def __init__(self):
        self.versions = {}

    @contextmanager
    def transaction(self):
        before = deepcopy(self.versions)
        try:
            yield self
        except BaseException:
            self.versions = before
            raise

    def get_question(self,question_id,version,actor_id,**kwargs):
        item = self.versions.get((question_id,version))
        if item is None:
            raise LookupError("missing question")
        if item["owner_id"]!=actor_id:
            raise LookupError("question not owned")
        return {"question_id":question_id,"version":version,**deepcopy(item["content"])}

    def find_question_duplicate(self,course_id,content_sha,family_sha):
        for (question_id,version),item in self.versions.items():
            value = item["content"]
            if value.get("course_id")==course_id and (value.get("content_sha")==content_sha or value.get("family_sha")==family_sha):
                return {"question_id":question_id,"version":version}
        return None

    def create_question_version(self,actor_id,question_id,version,question,**kwargs):
        if (question_id,version) in self.versions:
            raise ValueError("immutable duplicate")
        if version>1:
            previous = self.versions.get((question_id,version-1))
            if previous is None or previous["owner_id"]!=actor_id:
                raise PermissionError("only owner may create next version")
        content = {**deepcopy(dict(question)),**kwargs}
        self.versions[(question_id,version)] = {"owner_id":actor_id,"content":content}
        return {"question_id":question_id,"version":version,"content":content}


class QuestionBankTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="question-pipeline-")
        self.addCleanup(self.directory.cleanup)
        self.content = ContentStore(Path(self.directory.name)/"content.sqlite3")
        self.authority = FakeAuthority()
        self.bank = QuestionBank(self.content,self.authority)
        data = "二加三等于五。课程知识材料。联系人 test@example.invalid 电话13800138000".encode()
        material = self.content.import_material(course_id="course",owner_id="teacher",filename="lesson.txt",content=data,parsed=parse_material("lesson.txt",data),license_code="own",rights_confirmed=True,allow_generation=True,allow_retrieval=True,external_processing_allowed=True)
        self.material_id = material["id"]
        self.source = self.content.list_blocks(material["id"])[0]
        self.content.confirm_blocks(material["id"],[self.source["id"]],actor_id="teacher",expected_revision=1)

    def _create(self,**kwargs):
        values = {"course_id":"course","owner_id":"teacher","source_block_ids":[self.source["id"]],"external_processing_allowed":True}
        values.update(kwargs)
        return self.bank.create_generation(**values)

    def _apply(self,job,data):
        return self.bank.apply_result(job["id"],job["stage"],{"text":"local fake","data":data,"model":"fake-model","provider":"fake-provider","reasoning_effort":"high","request_id":"fake-id","usage":None},expected_revision=job["revision"])

    def _generate(self,question=None):
        return self._apply(self._create(),{"questions":[question or candidate()]})

    def _finish(self,job,answer=1):
        if job["stage"] in {"review","recheck"}:
            job = self._apply(job,{"solutions":[{"index":0,"answer":answer,"reasoning":"独立计算二加三为五","solvable":True}]})
        return self._apply(job,{"comparisons":[{"index":0,"agrees":True,"valid":True,"reason":"答案一致，材料支持"}]})

    def test_generation_structure_permission_anchor_and_dedup_are_required(self):
        for permission in (False,"denied",1,None):
            with self.subTest(permission=permission),self.assertRaises(PermissionError):
                self._create(external_processing_allowed=permission)
        job = self._create()
        prepared = self.bank.prepare_input(job["id"])
        text = json.dumps(prepared["messages"],ensure_ascii=False)
        self.assertNotIn("test@example.invalid",text)
        self.assertNotIn("13800138000",text)
        self.assertNotIn(self.material_id,text)
        self.assertNotIn(self.source["id"],text)
        self.assertNotIn("owner_id",text)
        self.assertIn('B1',text)
        malformed = candidate(); malformed["source_refs"]=["B99"]
        failed = self._apply(job,{"questions":[malformed]})
        self.assertEqual(failed["status"],"quarantined")
        self.assertEqual(self.authority.versions,{})

    def test_independent_review_sees_no_generated_answer_explanation_or_rubric(self):
        question = candidate(); question["rubric"]=[{"point":"MARKER_RUBRIC_SECRET","score_units":100}]
        job = self._generate(question)
        self.assertEqual(job["stage"],"review")
        prepared = self.bank.prepare_input(job["id"],stage="review")
        context = json.loads(prepared["messages"][1]["content"])
        self.assertNotIn("answer",context["questions"][0])
        self.assertNotIn("explanation",context["questions"][0])
        self.assertNotIn("rubric",context["questions"][0])
        self.assertNotIn("MARKER_SECRET",str(prepared))
        self.assertNotIn("MARKER_RUBRIC_SECRET",str(prepared))
        self.assertEqual(prepared["purpose"],"review")
        finished = self._finish(job)
        self.assertEqual(finished["status"],"completed")
        saved = next(iter(self.authority.versions.values()))["content"]
        self.assertEqual(saved["answer"],1)
        self.assertEqual(saved["external_processing_permission"],"approved")
        self.assertEqual(saved["source_anchors"][0]["anchor"],self.source["anchor"])
        self.assertEqual(saved["review_evidence"]["calls"]["review"]["model"],"fake-model")

    def test_conflict_gets_exactly_one_independent_recheck_and_never_rewrites_original_key(self):
        job = self._generate(candidate(answer=0))
        job = self._finish(job,answer=1)
        self.assertEqual(job["stage"],"recheck")
        recheck = self.bank.prepare_input(job["id"])
        self.assertNotIn("MARKER_SECRET",str(recheck))
        job = self._finish(job,answer=1)
        self.assertEqual(job["status"],"quarantined")
        self.assertEqual(job["state"]["candidates"][0]["answer"],0)
        self.assertEqual(job["state"]["recheck_count"],1)
        self.assertEqual(self.authority.versions,{})
        with self.assertRaises(ContentConflict):
            self.bank.prepare_input(job["id"])

    def test_boolean_false_comparison_and_unanswerable_solution_cannot_publish(self):
        job = self._generate()
        job = self._apply(job,{"solutions":[{"index":0,"answer":None,"reasoning":"题目有歧义","solvable":False}]})
        job = self._apply(job,{"comparisons":[{"index":0,"agrees":True,"valid":True,"reason":"假同意"}]})
        self.assertEqual(job["stage"],"recheck")
        self.assertEqual(self.authority.versions,{})

    def test_bad_rubric_or_review_result_quarantines_without_publication(self):
        question = candidate(); question["rubric"]=[{"point":"bad","score_units":0.5}]
        job = self._generate(question)
        self.assertEqual(job["status"],"quarantined")
        job = self._generate()
        job = self._apply(job,{"solutions":[{"index":0,"answer":1,"solvable":"false","reasoning":"invalid bool"}]})
        self.assertEqual(job["status"],"quarantined")
        self.assertEqual(self.authority.versions,{})

    def test_stale_callbacks_and_terminal_replay_are_rejected(self):
        original = self._create()
        current = self._apply(original,{"questions":[candidate()]})
        with self.assertRaises(ContentConflict):
            self._apply(original,{"questions":[candidate(answer=0)]})
        final = self._finish(current)
        with self.assertRaises(ContentConflict):
            self._apply(current,{"solutions":[]})
        self.assertEqual(len(self.authority.versions),1)
        self.assertEqual(final["status"],"completed")

    def test_sha_and_family_duplicate_generation_is_quarantined(self):
        self.assertEqual(self._finish(self._generate())["status"],"completed")
        duplicate = self._generate()
        self.assertEqual(duplicate["status"],"quarantined")
        self.assertIn("duplicate",duplicate["error"])
        self.assertEqual(len(self.authority.versions),1)
        exact,family = question_fingerprints(validate_question(candidate(stem="1加2等于多少")))
        other_exact,other_family = question_fingerprints(validate_question(candidate(stem="3加4等于多少")))
        self.assertNotEqual(exact,other_exact)
        self.assertEqual(family,other_family)

    def test_manual_edit_adds_new_version_after_review_and_keeps_original(self):
        final = self._finish(self._generate())
        reference = final["state"]["published_versions"][0]
        old = deepcopy(self.authority.versions[(reference["question_id"],1)])
        question = candidate(); question["explanation"]="人工纠正解释"
        edited = self.bank.create_edit(course_id="course",owner_id="teacher",question_id=reference["question_id"],expected_version=1,question=question,source_block_ids=[self.source["id"]],external_processing_allowed=True)
        self.assertEqual(edited["stage"],"review")
        final_edit = self._finish(edited)
        self.assertEqual(final_edit["status"],"completed")
        self.assertEqual(self.authority.versions[(reference["question_id"],1)],old)
        self.assertEqual(self.authority.versions[(reference["question_id"],2)]["content"]["explanation"],"人工纠正解释")

    def test_missing_authority_dedupe_fails_closed(self):
        class UnextendedAuthority:
            pass
        bank = QuestionBank(self.content,UnextendedAuthority())
        job = self._create()
        result = bank.apply_result(job["id"],"generate",{"questions":[candidate()]},expected_revision=1)
        self.assertEqual(result["status"],"quarantined")
        self.assertIn("deduplication",result["error"])

    def test_authority_commit_before_content_commit_replays_the_same_immutable_version(self):
        job = self._generate()
        job = self._apply(job,{"solutions":[{"index":0,"answer":1,"reasoning":"独立解题","solvable":True}]})
        # Simulate process death after LearningStore committed but before content.sqlite3 advanced.
        before = deepcopy(job)
        self.bank._publish(job,job["state"]["candidates"],self.bank._sources(job),job["state"])
        self.assertEqual(len(self.authority.versions),1)
        finished = self._finish(before)
        self.assertEqual(finished["status"],"completed")
        self.assertEqual(len(self.authority.versions),1)

    def test_batch_publication_failure_rolls_back_other_question_versions(self):
        second = candidate(stem="比较甲乙的大小？")
        job = self._apply(self._create(count=2),{"questions":[candidate(),second]})
        job = self._apply(job,{"solutions":[{"index":i,"answer":1,"reasoning":"独立求解","solvable":True} for i in range(2)]})
        create = self.authority.create_question_version
        count = 0
        def fail_second(*args,**kwargs):
            nonlocal count
            count += 1
            if count==2:
                raise ValueError("simulated second authority insert conflict")
            return create(*args,**kwargs)
        with patch.object(self.authority,"create_question_version",side_effect=fail_second):
            final = self._apply(job,{"comparisons":[{"index":i,"agrees":True,"valid":True,"reason":"一致"} for i in range(2)]})
        self.assertEqual(final["status"],"quarantined")
        self.assertEqual(self.authority.versions,{})

    def test_lost_worker_lease_rejects_callback_without_changing_question_or_job(self):
        job = self._create()
        bank = QuestionBank(self.content,self.authority,is_current=lambda:False)
        with self.assertRaises(ContentConflict):
            bank.apply_result(job["id"],job["stage"],{"questions":[candidate()]},expected_revision=job["revision"])
        self.assertEqual(self.content.get_job(job["id"])["revision"],job["revision"])
        self.assertEqual(self.authority.versions,{})

    def test_database_write_lock_is_not_held_during_injected_model_call(self):
        import asyncio
        content = self.content
        class Gateway:
            async def complete(self,purpose,messages,**kwargs):
                # BEGIN IMMEDIATE would fail here if the caller retained a write lock across I/O.
                with content.transaction() as db:
                    db.execute("SELECT 1")
                return {"questions":[candidate()]}
        bank = QuestionBank(content,self.authority,Gateway())
        result = asyncio.run(bank.execute_stage(self._create()["id"]))
        self.assertEqual(result["stage"],"review")


class RealLearningQuestionBankTests(unittest.TestCase):
    """Integration uses the sole authoritative question_versions database; no model calls."""
    def setUp(self):
        from app.learning_store import LearningStore
        self.directory = tempfile.TemporaryDirectory(prefix="question-real-learning-")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.content = ContentStore(root/"content.sqlite3")
        self.learning = LearningStore(root/"learning.sqlite3")
        self.bank = QuestionBank(self.content,self.learning)
        data = "课程依据：二加三等于五。".encode()
        material = self.content.import_material(course_id="course",owner_id="teacher",filename="lesson.txt",content=data,
            parsed=parse_material("lesson.txt",data),license_code="own",rights_confirmed=True,allow_generation=True,
            allow_retrieval=True,external_processing_allowed=True)
        self.source = self.content.list_blocks(material["id"])[0]
        self.content.confirm_blocks(material["id"],[self.source["id"]],actor_id="teacher",expected_revision=1)

    def _create(self):
        return self.bank.create_generation(course_id="course",owner_id="teacher",source_block_ids=[self.source["id"]],external_processing_allowed=True)

    def _apply(self,job,data):
        return self.bank.apply_result(job["id"],job["stage"],{"text":"local fake","data":data,"model":"fake-model","provider":"fake","reasoning_effort":"high","usage":None},expected_revision=job["revision"])

    def _review(self,job):
        job = self._apply(job,{"solutions":[{"index":0,"answer":1,"reasoning":"独立解题：二加三为五","solvable":True}]})
        return self._apply(job,{"comparisons":[{"index":0,"agrees":True,"valid":True,"reason":"一致且有来源支持"}]})

    def _publish(self):
        generated = self._apply(self._create(),{"questions":[candidate()]})
        self.assertEqual(generated["stage"],"review",generated.get("error"))
        return self._review(generated)

    def _get(self,reference,actor="teacher"):
        item = self.learning.get_question(reference["question_id"],reference["version"],actor)
        return item.get("content",item)

    def test_real_learning_preserves_approved_course_provenance_and_external_permission(self):
        final = self._publish()
        self.assertEqual(final["status"],"completed",final.get("error"))
        reference = final["state"]["published_versions"][0]
        question = self._get(reference)
        self.assertEqual(question["course_id"],"course")
        self.assertEqual(question["external_processing_permission"],"approved")
        self.assertEqual(question["content_sha"],reference["content_sha"])
        self.assertEqual(question["family_sha"],reference["family_sha"])
        self.assertEqual(question["source_anchors"][0]["block_id"],self.source["id"])
        self.assertEqual(question["source_anchors"][0]["anchor"],self.source["anchor"])
        self.assertGreaterEqual(question["source_revision"],2)
        self.assertEqual(question["review_evidence"]["calls"]["review"]["model"],"fake-model")
        with self.learning.connect() as db:
            row = db.execute("SELECT status,course_id FROM question_versions WHERE question_id=? AND version=1",(reference["question_id"],)).fetchone()
        self.assertEqual(tuple(row),("approved","course"))
        with self.assertRaises(LookupError):
            self._get(reference,actor="unrelated-teacher")

    def test_real_learning_dedup_and_owner_list_use_authoritative_metadata(self):
        reference = self._publish()["state"]["published_versions"][0]
        duplicate = self.learning.find_question_duplicate("course",reference["content_sha"],reference["family_sha"])
        self.assertEqual(duplicate["question_id"],reference["question_id"])
        self.assertFalse(self.learning.find_question_duplicate("another-course",reference["content_sha"],reference["family_sha"]))
        failed = self._apply(self._create(),{"questions":[candidate()]})
        self.assertEqual(failed["status"],"quarantined")
        items = self.learning.list_questions("course","teacher")
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]["question_id"],reference["question_id"])
        self.assertEqual(self.learning.list_questions("course","unrelated-teacher"),[])

    def test_real_learning_manual_edit_creates_v2_and_original_is_immutable(self):
        first = self._publish()["state"]["published_versions"][0]
        original = deepcopy(self._get(first))
        edited = candidate(); edited["explanation"]="人工澄清解释，原题不覆盖"
        job = self.bank.create_edit(course_id="course",owner_id="teacher",question_id=first["question_id"],expected_version=1,
            question=edited,source_block_ids=[self.source["id"]],external_processing_allowed=True)
        final = self._review(job)
        self.assertEqual(final["status"],"completed",final.get("error"))
        second = final["state"]["published_versions"][0]
        self.assertEqual(second["question_id"],first["question_id"])
        self.assertEqual(second["version"],2)
        self.assertEqual(self._get(first),original)
        self.assertEqual(self._get(second)["explanation"],edited["explanation"])
        with self.assertRaises(LookupError):
            self.bank.create_edit(course_id="course",owner_id="other",question_id=first["question_id"],expected_version=2,
                question=edited,source_block_ids=[self.source["id"]],external_processing_allowed=True)

    def test_real_learning_crash_replay_keeps_single_version_and_matching_hash(self):
        job = self._apply(self._create(),{"questions":[candidate()]})
        job = self._apply(job,{"solutions":[{"index":0,"answer":1,"reasoning":"独立解题","solvable":True}]})
        with self.learning.transaction():
            self.bank._publish(job,job["state"]["candidates"],self.bank._sources(job),job["state"])
        final = self._apply(job,{"comparisons":[{"index":0,"agrees":True,"valid":True,"reason":"一致"}]})
        self.assertEqual(final["status"],"completed",final.get("error"))
        with self.learning.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM question_versions").fetchone()[0],1)


if __name__=="__main__":
    unittest.main()
