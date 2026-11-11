"""API authorization and projections with real relational PlatformStore/ContentStore."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.content_store import ContentStore
from app.platform_store import PlatformStore
from app.routers.v1_materials import router
from app.services.material_parser import parse_material


class V1MaterialsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="v1-materials-")
        self.addCleanup(self.directory.cleanup)
        self.platform = PlatformStore(Path(self.directory.name)/"platform.sqlite3")
        self.content = ContentStore(Path(self.directory.name)/"content.sqlite3")
        self.learning = Mock()
        self.gateway = Mock()
        app = FastAPI()
        app.state.runtime = SimpleNamespace(platform=self.platform,content=self.content,learning=self.learning,gateway=self.gateway)
        app.include_router(router)
        self.client = TestClient(app,headers={"Origin":"http://testserver"})
        self.addCleanup(self.client.close)
        self.owner = self.platform.create_user("owner","负责人","local-test-password-hash","teacher")
        self.manager = self.platform.create_user("manager","协作教师","local-test-password-hash","teacher")
        self.other = self.platform.create_user("other","无关教师","local-test-password-hash","teacher")
        self.student = self.platform.create_user("student","学生","local-test-password-hash","student")
        self.outsider = self.platform.create_user("outsider","其他学生","local-test-password-hash","student")
        self.course = self.platform.create_course("数学","2026",self.owner["id"],course_id="course")
        self.class_a = self.platform.create_class("course","A班",owner_teacher_id=self.owner["id"],class_id="class-a")
        self.class_b = self.platform.create_class("course","B班",owner_teacher_id=self.owner["id"],class_id="class-b")
        self.platform.set_course_collaborator("course",self.manager["id"],"manage",granted_by=self.owner["id"])
        self.platform.create_course_membership("course",self.student["id"])
        self.platform.create_class_membership("class-a",self.student["id"])
        for key,user in (("owner",self.owner),("manager",self.manager),("other",self.other),("student",self.student),("outsider",self.outsider)):
            self.platform.create_session("token-"+key,user["id"],9_999_999_999)
        self._login("owner")

    def _login(self,name):
        self.client.cookies.clear()
        if name:
            self.client.cookies.set("sid","token-"+name)

    def _upload(self,filename="lesson.txt",data="课程公开正文\n\n参考答案：隐藏密钥",**form):
        values = {"visibility":"shared","license_code":"own","rights_confirmed":"true","allow_generation":"true",
                  "allow_retrieval":"true","allow_student_view":"true","external_processing_allowed":"true"}
        values.update(form)
        return self.client.post("/api/v1/courses/course/materials",files={"file":(filename,data.encode(),"application/octet-stream")},data=values)

    def _confirm(self,response,public=True,select=None):
        result = response.json()
        blocks = select or result["blocks"]
        return self.client.post(f"/api/v1/materials/{result['material']['id']}/confirm",json={
            "expected_revision":result["material"]["revision"],"block_ids":[b["id"] for b in blocks],
            "public_block_ids":[b["id"] for b in blocks if b["kind"]!="answer"] if public else [],
            "public_content_attested":public})

    def test_anonymous_student_and_unrelated_teacher_cannot_upload_or_enumerate(self):
        for name,status in ((None,401),("student",403),("outsider",403),("other",404)):
            self._login(name)
            response = self._upload()
            self.assertEqual(response.status_code,status,response.text)
        self._login("outsider")
        self.assertEqual(self.client.get("/api/v1/courses/course/materials").status_code,404)
        self._login("other")
        self.assertEqual(self.client.get("/api/v1/courses/course/materials").status_code,404)

    def test_upload_uses_explicit_permissions_hides_private_paths_and_is_not_publicly_downloadable(self):
        response = self._upload()
        self.assertEqual(response.status_code,201,response.text)
        result = response.json()
        self.assertNotIn("storage_key",result["material"])
        self.assertTrue(result["material"]["external_processing_allowed"])
        self.assertTrue(any(b["kind"]=="answer" for b in result["blocks"]))
        self.assertEqual(self.client.get(f"/api/v1/materials/{result['material']['id']}/original").status_code,404)
        self.assertEqual(self._upload(external_processing_allowed="denied").status_code,422)
        self.assertEqual(self._upload(filename="../danger.txt").status_code,422)
        self.gateway.complete.assert_not_called()

    def test_student_gets_only_confirmed_public_blocks_without_answers_issues_or_identity(self):
        response = self._upload()
        self.assertEqual(response.status_code,201,response.text)
        file_id = response.json()["material"]["id"]
        self._login("student")
        self.assertEqual(self.client.get(f"/api/v1/materials/{file_id}").status_code,404)
        self._login("owner")
        confirmed = self._confirm(response)
        self.assertEqual(confirmed.status_code,200,confirmed.text)
        self._login("student")
        read = self.client.get(f"/api/v1/materials/{file_id}")
        self.assertEqual(read.status_code,200,read.text)
        text = read.text
        self.assertIn("课程公开正文",text)
        self.assertNotIn("隐藏密钥",text)
        self.assertNotIn("issues",read.json())
        self.assertNotIn("owner_id",text)
        self.assertNotIn("storage_key",text)
        self.assertNotIn("metadata",text)
        self.assertNotIn("confirmed_by",text)
        self.assertEqual(self.client.get(f"/api/v1/materials/{file_id}/issues").status_code,403)
        self.assertEqual(self.client.get("/api/v1/courses/course/sources/search",params={"q":"隐藏密钥"}).json()["items"],[])
        self.gateway.complete.assert_not_called()

    def test_collaborator_only_manages_own_drafts_and_owner_confirms_shared_material(self):
        self._login("manager")
        response = self._upload()
        file_id = response.json()["material"]["id"]
        self.assertEqual(self._confirm(response).status_code,403)
        self._login("owner")
        self.assertEqual(self.client.get(f"/api/v1/materials/{file_id}").status_code,200)
        changed = self.client.patch(f"/api/v1/materials/{file_id}",json={"expected_revision":1,"title":"owner editing manager draft"})
        self.assertEqual(changed.status_code,403,changed.text)
        self.assertEqual(self._confirm(response).status_code,200)
        self._login("manager")
        changed = self.client.patch(f"/api/v1/materials/{file_id}",json={"expected_revision":2,"title":"mutate confirmed"})
        self.assertEqual(changed.status_code,409,changed.text)

    def test_private_material_and_wrong_class_are_hidden_from_others(self):
        private = self._upload(visibility="private")
        file_id = private.json()["material"]["id"]
        self._login("manager")
        self.assertEqual(self.client.get(f"/api/v1/materials/{file_id}").status_code,404)
        self._login("owner")
        wrong_class = self._upload(class_id="class-b")
        self.assertEqual(self._confirm(wrong_class).status_code,200)
        self._login("student")
        self.assertEqual(self.client.get(f"/api/v1/materials/{wrong_class.json()['material']['id']}").status_code,404)
        self.assertEqual(self.client.get("/api/v1/courses/course/sources/search",params={"q":"课程","class_id":"class-b"}).status_code,404)

    def test_partial_shared_material_does_not_show_unconfirmed_blocks_to_collaborator(self):
        response = self._upload(data="已确认知识\n\n还未确认草稿")
        self.assertEqual(self._confirm(response,public=False,select=[response.json()["blocks"][0]]).status_code,200)
        self._login("manager")
        read = self.client.get(f"/api/v1/materials/{response.json()['material']['id']}")
        self.assertEqual(read.status_code,200,read.text)
        self.assertIn("已确认知识",read.text)
        self.assertNotIn("还未确认草稿",read.text)
        self.assertNotIn("issues",read.json())

    def test_generation_post_only_creates_durable_pending_job_and_get_does_not_call_model(self):
        response = self._upload(data="获准课程知识")
        self.assertEqual(self._confirm(response).status_code,200)
        block_id = response.json()["blocks"][0]["id"]
        rejected = self.client.post("/api/v1/courses/course/question-jobs",json={"source_block_ids":[block_id],"external_processing_allowed":"denied"})
        self.assertEqual(rejected.status_code,422)
        response = self.client.post("/api/v1/courses/course/question-jobs",json={"source_block_ids":[block_id],"external_processing_allowed":True})
        self.assertEqual(response.status_code,202,response.text)
        job = response.json()["job"]
        self.assertEqual(job["status"],"pending")
        self.assertEqual([j["id"] for j in self.content.list_pending_jobs()],[job["id"]])
        self.assertEqual(self.client.get(f"/api/v1/content-jobs/{job['id']}").status_code,200)
        self._login("manager")
        self.assertEqual(self.client.get(f"/api/v1/content-jobs/{job['id']}").status_code,404)
        self._login("student")
        self.assertEqual(self.client.get(f"/api/v1/content-jobs/{job['id']}").status_code,403)
        self.learning.create_question_version.assert_not_called()
        self.gateway.complete.assert_not_called()

    def test_grade_mapping_preview_keeps_missing_null_and_checks_duplicates(self):
        response = self._upload(filename="scores.csv",data="学号,数学\n0012,0\n0013,\n0012,30",visibility="private",allow_student_view="false")
        self.assertEqual(response.status_code,201,response.text)
        result = response.json()
        preview = self.client.post(f"/api/v1/materials/{result['material']['id']}/grade-mapping-preview",json={"block_id":result["blocks"][0]["id"],"mapping":{"student_number_column":0,"score_columns":{"数学":1}}})
        self.assertEqual(preview.status_code,200,preview.text)
        records = preview.json()["records"]
        self.assertEqual(records[0]["student_number"],"0012")
        self.assertEqual(records[0]["score"],0)
        self.assertIsNone(records[1]["score"])
        self.assertTrue(records[0]["duplicate"])
        self.assertFalse(preview.json()["committed"])
        self._login("student")
        self.assertEqual(self.client.post(f"/api/v1/materials/{result['material']['id']}/grade-mapping-preview",json={"block_id":result["blocks"][0]["id"],"mapping":{}}).status_code,403)

    def test_an_answer_marked_text_cannot_be_attested_public(self):
        material = self.content.import_material(course_id="course",owner_id=self.owner["id"],filename="lesson.txt",content=b"answer data",parsed={"status":"parsed","format":"txt","blocks":[{"anchor":"p:1","kind":"text","text":"参考答案：秘密"}],"issues":[]},visibility="shared",license_code="own",rights_confirmed=True,allow_student_view=True)
        block_id = self.content.list_blocks(material["id"])[0]["id"]
        response = self.client.post(f"/api/v1/materials/{material['id']}/confirm",json={"expected_revision":1,"block_ids":[block_id],"public_block_ids":[block_id],"public_content_attested":True})
        self.assertEqual(response.status_code,422,response.text)


if __name__=="__main__":
    unittest.main()
