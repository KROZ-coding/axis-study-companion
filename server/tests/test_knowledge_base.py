"""Knowledge index must stay a plain JSON document (no pickle on disk or load path)."""

import json
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.services.knowledge_base import LocalKnowledgeBase, _write_index, reindex_existing


CHUNKS = [
    {"src": "docs/a.md", "kb": "audit_standards", "text": "审计准则 要求 注册会计师 保持职业怀疑"},
    {"src": "docs/b.md", "kb": "method", "text": "函证程序 应当 评价 回函 的 可靠性"},
]


class KnowledgeIndexSerializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="knowledge-index-test-")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.index_path = self.data_dir / "knowledge" / "index.json"

    def test_default_index_path_is_json(self) -> None:
        self.assertEqual(LocalKnowledgeBase(Settings(data_dir=str(self.data_dir))).index_path,
                         self.data_dir / "knowledge" / "index.json")

    def test_write_index_persists_plain_json(self) -> None:
        result = _write_index(CHUNKS, self.index_path, source_dir=str(self.data_dir))
        self.assertEqual(result["chunks"], len(CHUNKS))
        payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertIsInstance(payload["postings"], dict)
        self.assertEqual(payload["source_dir"], str(self.data_dir))

    def test_load_and_search_round_trip(self) -> None:
        _write_index(CHUNKS, self.index_path, source_dir=str(self.data_dir))
        kb = LocalKnowledgeBase(Settings(data_dir=str(self.data_dir)))
        self.assertTrue(kb.load())
        hits = kb.search("职业怀疑")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["name"], "docs/a.md")
        self.assertEqual(hits[0]["kb"], "audit_standards")

    def test_reindex_existing_reuses_chunks(self) -> None:
        _write_index(CHUNKS, self.index_path, source_dir=str(self.data_dir))
        result = reindex_existing(self.index_path)
        self.assertEqual(result["chunks"], len(CHUNKS))
        self.assertEqual(result["files"], 0)
        payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        self.assertEqual([chunk["src"] for chunk in payload["chunks"]], ["docs/a.md", "docs/b.md"])

    def test_missing_index_loads_as_empty(self) -> None:
        kb = LocalKnowledgeBase(Settings(data_dir=str(self.data_dir)))
        self.assertFalse(kb.load())
        self.assertEqual(kb.search("职业怀疑"), [])

    def test_corrupt_index_is_ignored_not_raised(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text("{not-json", encoding="utf-8")
        kb = LocalKnowledgeBase(Settings(data_dir=str(self.data_dir)))
        self.assertFalse(kb.load())
        self.assertEqual(kb.search("职业怀疑"), [])

    def test_reindex_rejects_non_json_index(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_bytes(b"\x80\x04\x95legacy-pickle")
        with self.assertRaises(ValueError) as caught:
            reindex_existing(self.index_path)
        self.assertIn("JSON", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
