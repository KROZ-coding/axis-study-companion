"""Private, versioned relational content storage (question authority lives in LearningStore)."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4


class ContentConflict(ValueError):
    pass


class ContentStore:
    SCHEMA_VERSION = 1
    _MIGRATIONS = {1: "_migration_1"}
    LICENSES = frozenset({"own", "permission", "cc0", "cc_by", "cc_by_sa", "unknown", "restricted"})

    def __init__(self, db_path: str | Path, *, timeout: float = 15.0) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("content storage requires a file-backed database")
        self.db_path = Path(db_path)
        self.timeout = timeout
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.originals_dir = self.db_path.parent / "material_originals"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path), timeout=self.timeout, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @property
    def schema_version(self) -> int:
        with self.connect() as db:
            return int(db.execute("PRAGMA user_version").fetchone()[0])

    @property
    def journal_mode(self) -> str:
        with self.connect() as db:
            return str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def _initialize(self) -> None:
        # Refuse foreign/newer databases before changing their journal mode.
        with self.connect() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if version > self.SCHEMA_VERSION:
                raise RuntimeError("content database schema is newer than this application")
            if version == 0 and tables:
                raise RuntimeError("refusing an unversioned non-empty content database")
            if version and not {"material_files", "source_blocks", "import_batches", "content_jobs"}.issubset(tables):
                raise RuntimeError("database is not a content database")
            if str(db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
                raise RuntimeError("content database requires WAL")
            db.execute("BEGIN IMMEDIATE")
            try:
                version = int(db.execute("PRAGMA user_version").fetchone()[0])
                while version < self.SCHEMA_VERSION:
                    version += 1
                    getattr(self, self._MIGRATIONS[version])(db)
                    if version == 1:
                        db.execute("PRAGMA user_version=1")
                    else:
                        raise RuntimeError("no fixed schema version statement registered")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _migration_1(db: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE import_batches (
                id TEXT PRIMARY KEY, course_id TEXT NOT NULL, class_id TEXT, owner_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('draft','partial','confirmed','manual_required','quarantined')),
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
            """CREATE TABLE material_files (
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(id),
                course_id TEXT NOT NULL, class_id TEXT, owner_id TEXT NOT NULL,
                filename TEXT NOT NULL, title TEXT NOT NULL, format TEXT NOT NULL,
                sha256 TEXT NOT NULL CHECK(length(sha256)=64), byte_size INTEGER NOT NULL CHECK(byte_size>=0),
                storage_key TEXT NOT NULL UNIQUE, visibility TEXT NOT NULL CHECK(visibility IN ('private','shared')),
                license_code TEXT NOT NULL, license_note TEXT NOT NULL DEFAULT '', rights_confirmed INTEGER NOT NULL CHECK(rights_confirmed IN (0,1)),
                allow_generation INTEGER NOT NULL CHECK(allow_generation IN (0,1)),
                allow_retrieval INTEGER NOT NULL CHECK(allow_retrieval IN (0,1)),
                allow_student_view INTEGER NOT NULL CHECK(allow_student_view IN (0,1)),
                external_processing_allowed INTEGER NOT NULL DEFAULT 0 CHECK(external_processing_allowed IN (0,1)),
                status TEXT NOT NULL CHECK(status IN ('draft','partial','confirmed','manual_required','quarantined')),
                revision INTEGER NOT NULL DEFAULT 1, metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
            "CREATE INDEX idx_material_course ON material_files(course_id,class_id,owner_id,status)",
            "CREATE INDEX idx_material_hash ON material_files(course_id,sha256)",
            """CREATE TABLE source_blocks (
                id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES material_files(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL CHECK(ordinal>=0), anchor TEXT NOT NULL CHECK(length(anchor)>0),
                kind TEXT NOT NULL CHECK(kind IN ('text','table','image','answer')),
                text TEXT NOT NULL DEFAULT '', data_json TEXT NOT NULL DEFAULT '{}',
                sha256 TEXT NOT NULL CHECK(length(sha256)=64),
                status TEXT NOT NULL CHECK(status IN ('draft','confirmed','quarantined','manual_required')),
                student_visible INTEGER NOT NULL DEFAULT 0 CHECK(student_visible IN (0,1)),
                confirmed_by TEXT, confirmed_at REAL, UNIQUE(file_id,anchor), UNIQUE(file_id,ordinal))""",
            "CREATE INDEX idx_blocks_status ON source_blocks(file_id,status,student_visible)",
            """CREATE TABLE material_issues (
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
                file_id TEXT NOT NULL REFERENCES material_files(id) ON DELETE CASCADE,
                block_id TEXT REFERENCES source_blocks(id) ON DELETE CASCADE,
                code TEXT NOT NULL, message TEXT NOT NULL, severity TEXT NOT NULL CHECK(severity IN ('warning','blocking','manual_required')),
                status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
                resolution TEXT, resolved_by TEXT, created_at REAL NOT NULL, resolved_at REAL)""",
            "CREATE INDEX idx_issues_file ON material_issues(file_id,status)",
            """CREATE TABLE confirmations (
                id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES material_files(id),
                actor_id TEXT NOT NULL, file_revision INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '',
                public_content_attested INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)""",
            """CREATE TABLE confirmation_blocks (
                confirmation_id TEXT NOT NULL REFERENCES confirmations(id) ON DELETE CASCADE,
                block_id TEXT NOT NULL REFERENCES source_blocks(id), student_visible INTEGER NOT NULL,
                PRIMARY KEY(confirmation_id,block_id))""",
            """CREATE TABLE course_requirements (
                id TEXT PRIMARY KEY, course_id TEXT NOT NULL, text TEXT NOT NULL,
                created_by TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','confirmed')),
                confirmed_by TEXT, created_at REAL NOT NULL, confirmed_at REAL)""",
            """CREATE TABLE requirement_blocks (
                requirement_id TEXT NOT NULL REFERENCES course_requirements(id) ON DELETE CASCADE,
                block_id TEXT NOT NULL REFERENCES source_blocks(id), PRIMARY KEY(requirement_id,block_id))""",
            """CREATE TABLE knowledge_points (
                id TEXT PRIMARY KEY, course_id TEXT NOT NULL, code TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('draft','confirmed')),
                confirmed_by TEXT, created_at REAL NOT NULL, confirmed_at REAL,
                UNIQUE(course_id,code), UNIQUE(id,course_id))""",
            """CREATE TABLE knowledge_sources (
                knowledge_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
                block_id TEXT NOT NULL REFERENCES source_blocks(id), PRIMARY KEY(knowledge_id,block_id))""",
            """CREATE TABLE knowledge_prerequisites (
                knowledge_id TEXT NOT NULL, prerequisite_id TEXT NOT NULL, course_id TEXT NOT NULL,
                PRIMARY KEY(knowledge_id,prerequisite_id), CHECK(knowledge_id<>prerequisite_id),
                FOREIGN KEY(knowledge_id,course_id) REFERENCES knowledge_points(id,course_id) ON DELETE CASCADE,
                FOREIGN KEY(prerequisite_id,course_id) REFERENCES knowledge_points(id,course_id) ON DELETE CASCADE)""",
            """CREATE TABLE content_jobs (
                id TEXT PRIMARY KEY, course_id TEXT NOT NULL, class_id TEXT, owner_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('material_parse','question_generate','question_edit')),
                stage TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','running','completed','quarantined')),
                revision INTEGER NOT NULL DEFAULT 1, input_json TEXT NOT NULL, state_json TEXT NOT NULL DEFAULT '{}',
                error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
            "CREATE INDEX idx_content_jobs_scope ON content_jobs(course_id,owner_id,status)",
        )
        for statement in statements:
            db.execute(statement)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid4().hex}"

    @staticmethod
    def _file(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        for name in ("rights_confirmed", "allow_generation", "allow_retrieval", "allow_student_view", "external_processing_allowed"):
            result[name] = bool(result[name])
        return result

    @staticmethod
    def _block(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["data"] = json.loads(result.pop("data_json"))
        result["student_visible"] = bool(result["student_visible"])
        if "external_processing_allowed" in result:
            result["external_processing_allowed"] = result["external_processing_allowed"] == 1
        return result

    def import_material(self, *, course_id: str, owner_id: str, filename: str, content: bytes,
                        parsed: Mapping[str, Any], class_id: str | None = None, title: str = "",
                        visibility: str = "private", license_code: str = "unknown", license_note: str = "",
                        rights_confirmed: bool = False, allow_generation: bool = False,
                        allow_retrieval: bool = False, allow_student_view: bool = False,
                        external_processing_allowed: bool = False) -> dict[str, Any]:
        """Persist a parser result and an unexecutable, private original atomically w.r.t. SQLite."""
        from .services.material_parser import validate_filename
        validate_filename(filename)
        if not course_id.strip() or not owner_id.strip() or visibility not in {"private", "shared"}:
            raise ValueError("invalid material scope")
        if any(type(value) is not bool for value in (rights_confirmed,allow_generation,allow_retrieval,allow_student_view,external_processing_allowed)):
            raise ValueError("source permission flags must be explicit booleans")
        if license_code not in self.LICENSES:
            raise ValueError("invalid license")
        if rights_confirmed and license_code in {"unknown", "restricted"}:
            raise ValueError("source permission has not been established")
        if rights_confirmed and license_code in {"permission", "cc_by", "cc_by_sa"} and not license_note.strip():
            raise ValueError("permission or attribution statement is required")
        payload = dict(parsed)
        if payload.get("status") not in {"parsed", "manual_required"}:
            raise ValueError("invalid parser result")
        self.originals_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Every upload gets a new empty directory. Never use a client filename as a path.
        directory = Path(tempfile.mkdtemp(prefix="upload_", dir=self.originals_dir))
        storage_key = f"{directory.name}/original.bin"
        try:
            descriptor = os.open(directory / "original.bin", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            now = time.time()
            file_id, batch_id = self._id("mat"), self._id("batch")
            with self.transaction() as db:
                status = "manual_required" if payload["status"] == "manual_required" else "draft"
                db.execute("INSERT INTO import_batches VALUES (?,?,?,?,?,?,?)",
                           (batch_id, course_id, class_id, owner_id, status, now, now))
                db.execute("""INSERT INTO material_files
                    (id,batch_id,course_id,class_id,owner_id,filename,title,format,sha256,byte_size,storage_key,visibility,
                     license_code,license_note,rights_confirmed,allow_generation,allow_retrieval,allow_student_view,external_processing_allowed,status,
                     metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (file_id,batch_id,course_id,class_id,owner_id,filename,title.strip() or filename,
                            payload.get("format", ""),hashlib.sha256(content).hexdigest(),len(content),storage_key,visibility,
                            license_code,license_note.strip(),int(rights_confirmed),int(allow_generation),int(allow_retrieval),
                            int(allow_student_view),int(external_processing_allowed),status,self._json(payload.get("metadata", {})),now,now))
                self._insert_parse(db, file_id, batch_id, payload)
                if not rights_confirmed:
                    self._insert_issue(db, file_id, batch_id, "source_permission_required", "来源许可尚未人工确认", "blocking")
            return self.get_material(file_id)  # type: ignore[return-value]
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def _insert_issue(self, db: sqlite3.Connection, file_id: str, batch_id: str, code: str,
                      message: str, severity: str, block_id: str | None = None) -> str:
        issue_id = self._id("issue")
        db.execute("""INSERT INTO material_issues(id,batch_id,file_id,block_id,code,message,severity,created_at)
                      VALUES (?,?,?,?,?,?,?,?)""", (issue_id,batch_id,file_id,block_id,code,message,severity,time.time()))
        return issue_id

    def _insert_parse(self, db: sqlite3.Connection, file_id: str, batch_id: str, parsed: Mapping[str, Any]) -> None:
        anchors: dict[str, str] = {}
        issues = list(parsed.get("issues", []))
        for ordinal, value in enumerate(parsed.get("blocks", [])):
            block = dict(value)
            anchor, text = str(block.get("anchor", "")).strip(), str(block.get("text", ""))
            if not anchor or anchor in anchors:
                raise ValueError("source anchors must be nonempty and unique")
            block_id = self._id("block")
            anchors[anchor] = block_id
            linked = [issue for issue in issues if issue.get("anchor") == anchor]
            status = "draft"
            if any(i.get("severity") == "manual_required" for i in linked):
                status = "manual_required"
            elif any(i.get("severity") == "blocking" for i in linked):
                status = "quarantined"
            data = dict(block.get("data", {}))
            digest = hashlib.sha256(self._json({"text": text, "data": data}).encode()).hexdigest()
            db.execute("""INSERT INTO source_blocks(id,file_id,ordinal,anchor,kind,text,data_json,sha256,status)
                          VALUES (?,?,?,?,?,?,?,?,?)""", (block_id,file_id,ordinal,anchor,block.get("kind", "text"),text,self._json(data),digest,status))
        for issue in issues:
            anchor = issue.get("anchor")
            if anchor is not None and anchor not in anchors:
                raise ValueError("issue references an unknown source anchor")
            self._insert_issue(db,file_id,batch_id,str(issue["code"]),str(issue["message"]),
                               str(issue.get("severity", "blocking")),anchors.get(anchor))

    def get_material(self, file_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._file(db.execute("SELECT * FROM material_files WHERE id=?", (file_id,)).fetchone())

    def list_materials(self, course_id: str, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("invalid pagination")
        with self.connect() as db:
            return [self._file(r) for r in db.execute(
                "SELECT * FROM material_files WHERE course_id=? ORDER BY created_at,id LIMIT ? OFFSET ?",
                (course_id,limit,offset))]  # type: ignore[misc]

    def list_blocks(self, file_id: str, *, confirmed_only: bool = False, student: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [self._block(r) for r in db.execute("""SELECT b.* FROM source_blocks b
                JOIN material_files f ON f.id=b.file_id WHERE b.file_id=?
                  AND (?=0 OR b.status='confirmed')
                  AND (?=0 OR (b.student_visible=1 AND b.kind IN ('text','table')
                               AND f.allow_student_view=1 AND f.rights_confirmed=1))
                ORDER BY b.ordinal""", (file_id,int(bool(confirmed_only or student)),int(bool(student))))]

    def list_issues(self, file_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM material_issues WHERE file_id=? ORDER BY created_at,id", (file_id,))]

    def original_bytes(self, file_id: str, *, max_bytes: int = 25 * 1024 * 1024) -> bytes:
        material = self.get_material(file_id)
        if material is None:
            raise KeyError(file_id)
        root = self.originals_dir.resolve()
        raw_path = root / material["storage_key"]
        path = raw_path.resolve()
        if not path.is_relative_to(root) or raw_path.is_symlink() or raw_path.parent.is_symlink():
            raise ValueError("unsafe private original path")
        with path.open("rb") as stream:
            content = stream.read(max_bytes + 1)
        if len(content) > max_bytes or len(content) != material["byte_size"] or hashlib.sha256(content).hexdigest() != material["sha256"]:
            raise ContentConflict("private original size or hash mismatch")
        return content

    def update_draft(self, file_id: str, *, actor_id: str, expected_revision: int, title: str | None = None,
                     license_code: str | None = None, license_note: str | None = None,
                     rights_confirmed: bool | None = None, allow_generation: bool | None = None,
                     allow_retrieval: bool | None = None, allow_student_view: bool | None = None,
                     external_processing_allowed: bool | None = None) -> dict[str, Any]:
        changes = {k: v for k, v in locals().items() if k in {
            "title","license_code","license_note","rights_confirmed","allow_generation","allow_retrieval","allow_student_view","external_processing_allowed"} and v is not None}
        for name in {"rights_confirmed","allow_generation","allow_retrieval","allow_student_view","external_processing_allowed"} & changes.keys():
            if type(changes[name]) is not bool:
                raise ValueError("source permission flags must be explicit booleans")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM material_files WHERE id=?", (file_id,)).fetchone()
            self._assert_draft(db,row,actor_id,expected_revision)
            effective = dict(row)
            effective.update(changes)
            if effective["license_code"] not in self.LICENSES:
                raise ValueError("invalid license")
            if effective["rights_confirmed"] and (effective["license_code"] in {"unknown","restricted"} or
                    (effective["license_code"] in {"permission","cc_by","cc_by_sa"} and not effective["license_note"].strip())):
                raise ValueError("source permission or attribution required")
            if "title" in changes and not str(changes["title"]).strip():
                raise ValueError("title required")
            if title is not None:
                db.execute("UPDATE material_files SET title=? WHERE id=?", (title,file_id))
            if license_code is not None:
                db.execute("UPDATE material_files SET license_code=? WHERE id=?", (license_code,file_id))
            if license_note is not None:
                db.execute("UPDATE material_files SET license_note=? WHERE id=?", (license_note,file_id))
            if rights_confirmed is not None:
                db.execute("UPDATE material_files SET rights_confirmed=? WHERE id=?", (rights_confirmed,file_id))
            if allow_generation is not None:
                db.execute("UPDATE material_files SET allow_generation=? WHERE id=?", (allow_generation,file_id))
            if allow_retrieval is not None:
                db.execute("UPDATE material_files SET allow_retrieval=? WHERE id=?", (allow_retrieval,file_id))
            if allow_student_view is not None:
                db.execute("UPDATE material_files SET allow_student_view=? WHERE id=?", (allow_student_view,file_id))
            if external_processing_allowed is not None:
                db.execute("UPDATE material_files SET external_processing_allowed=? WHERE id=?", (external_processing_allowed,file_id))
            if effective["rights_confirmed"]:
                db.execute("""UPDATE material_issues SET status='resolved',resolution='许可由上传者确认',resolved_by=?,resolved_at=?
                              WHERE file_id=? AND code='source_permission_required' AND status='open'""", (actor_id,time.time(),file_id))
            db.execute("UPDATE material_files SET revision=revision+1,updated_at=? WHERE id=?", (time.time(),file_id))
        return self.get_material(file_id)  # type: ignore[return-value]

    @staticmethod
    def _assert_draft(db: sqlite3.Connection, row: sqlite3.Row | None, actor_id: str, expected_revision: int, *, allow_partial: bool = False) -> None:
        if row is None:
            raise KeyError("material not found")
        if row["owner_id"] != actor_id:
            raise PermissionError("only the uploader may manage their own draft")
        if row["revision"] != expected_revision:
            raise ContentConflict("material revision changed")
        if not allow_partial and db.execute("SELECT 1 FROM source_blocks WHERE file_id=? AND status='confirmed' LIMIT 1", (row["id"],)).fetchone():
            raise ContentConflict("confirmed content is immutable; import a new version")

    def resolve_issue(self, file_id: str, issue_id: str, *, actor_id: str, expected_revision: int,
                      resolution: str, replacement_text: str | None = None) -> dict[str, Any]:
        if not resolution.strip():
            raise ValueError("a human resolution is required")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM material_files WHERE id=?", (file_id,)).fetchone()
            self._assert_draft(db,row,actor_id,expected_revision,allow_partial=True)
            issue = db.execute("SELECT * FROM material_issues WHERE id=? AND file_id=?", (issue_id,file_id)).fetchone()
            if issue is None:
                raise KeyError("issue not found")
            if issue["status"] != "open":
                raise ContentConflict("issue already resolved")
            if issue["code"] == "source_permission_required":
                raise ValueError("update the source license before resolving its issue")
            block_id = issue["block_id"]
            if not block_id or db.execute("SELECT status FROM source_blocks WHERE id=?", (block_id,)).fetchone()[0] == "confirmed":
                raise ContentConflict("only unresolved unconfirmed block issues may be changed")
            if issue["severity"] == "manual_required" and (not block_id or not replacement_text or not replacement_text.strip()):
                raise ValueError("manual transcription is required")
            if replacement_text is not None:
                if not block_id or not replacement_text.strip() or len(replacement_text) > 100_000:
                    raise ValueError("invalid manual transcription")
                text = replacement_text.strip()
                data = {"manual_transcription": True, "original_anchor": db.execute("SELECT anchor FROM source_blocks WHERE id=?", (block_id,)).fetchone()[0]}
                digest = hashlib.sha256(self._json({"text":text,"data":data}).encode()).hexdigest()
                db.execute("UPDATE source_blocks SET text=?,data_json=?,sha256=?,kind='text' WHERE id=?", (text,self._json(data),digest,block_id))
            db.execute("UPDATE material_issues SET status='resolved',resolution=?,resolved_by=?,resolved_at=? WHERE id=?",
                       (resolution.strip(),actor_id,time.time(),issue_id))
            if block_id and not db.execute("SELECT 1 FROM material_issues WHERE block_id=? AND status='open' AND severity<>'warning'", (block_id,)).fetchone():
                db.execute("UPDATE source_blocks SET status='draft' WHERE id=? AND status<>'confirmed'", (block_id,))
            db.execute("UPDATE material_files SET revision=revision+1,updated_at=? WHERE id=?", (time.time(),file_id))
        return self.get_material(file_id)  # type: ignore[return-value]

    def confirm_blocks(self, file_id: str, block_ids: Sequence[str], *, actor_id: str,
                       expected_revision: int, public_block_ids: Sequence[str] = (),
                       public_content_attested: bool = False, note: str = "",
                       course_owner_confirmation: bool = False) -> dict[str, Any]:
        selected, public = set(block_ids), set(public_block_ids)
        if type(public_content_attested) is not bool or type(course_owner_confirmation) is not bool:
            raise ValueError("confirmation flags must be explicit booleans")
        if not selected or len(selected) != len(block_ids) or not public <= selected:
            raise ValueError("select unique source blocks; public blocks must be selected")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM material_files WHERE id=?", (file_id,)).fetchone()
            if row is None:
                raise KeyError(file_id)
            if row["revision"] != expected_revision:
                raise ContentConflict("material revision changed")
            if row["visibility"] == "shared" and not course_owner_confirmation:
                raise PermissionError("shared materials require course-owner confirmation")
            if row["visibility"] == "private" and row["owner_id"] != actor_id:
                raise PermissionError("private materials require uploader confirmation")
            if not row["rights_confirmed"] or row["license_code"] in {"unknown","restricted"}:
                raise ValueError("confirmed source permission is required")
            if public and (row["visibility"] != "shared" or not public_content_attested or not row["allow_student_view"]):
                raise ValueError("student publication requires explicit permission and answer-free attestation")
            if db.execute("SELECT 1 FROM material_issues WHERE file_id=? AND block_id IS NULL AND status='open' AND severity<>'warning'", (file_id,)).fetchone():
                raise ContentConflict("file-wide unresolved issues prevent confirmation")
            blocks = {r["id"]: r for r in db.execute("SELECT * FROM source_blocks WHERE file_id=?", (file_id,))}
            if not selected <= blocks.keys():
                raise ValueError("source block belongs to another material")
            from .services.material_parser import is_answer_content
            for block_id in selected:
                block = blocks[block_id]
                if block["status"] != "draft" or not block["text"].strip():
                    raise ContentConflict("only qualified draft blocks may be confirmed")
                if block_id in public and (block["kind"] not in {"text","table"} or is_answer_content(block["text"])):
                    raise ValueError("answers and images cannot be student-public source blocks")
                if db.execute("SELECT 1 FROM material_issues WHERE block_id=? AND status='open' AND severity<>'warning'", (block_id,)).fetchone():
                    raise ContentConflict("unresolved source block issue")
            now, confirmation_id = time.time(), self._id("confirm")
            db.execute("INSERT INTO confirmations VALUES (?,?,?,?,?,?,?)", (confirmation_id,file_id,actor_id,expected_revision,note,int(public_content_attested),now))
            for block_id in selected:
                db.execute("UPDATE source_blocks SET status='confirmed',student_visible=?,confirmed_by=?,confirmed_at=? WHERE id=?",
                           (int(block_id in public),actor_id,now,block_id))
                db.execute("INSERT INTO confirmation_blocks VALUES (?,?,?)", (confirmation_id,block_id,int(block_id in public)))
            remaining = db.execute("SELECT count(*) FROM source_blocks WHERE file_id=? AND status<>'confirmed'", (file_id,)).fetchone()[0]
            status = "partial" if remaining else "confirmed"
            db.execute("UPDATE material_files SET status=?,revision=revision+1,updated_at=? WHERE id=?", (status,now,file_id))
            db.execute("UPDATE import_batches SET status=?,updated_at=? WHERE id=?", (status,now,row["batch_id"]))
        return {"confirmation_id":confirmation_id,"material":self.get_material(file_id),"confirmed_block_ids":sorted(selected)}

    def confirmed_sources(self, course_id: str, block_ids: Sequence[str], *, purpose: str,
                          student: bool = False, class_id: str | None = None) -> list[dict[str, Any]]:
        """Only this projection may enter generation/retrieval. Explicitly selected sources are all-or-nothing."""
        if purpose not in {"generation","retrieval"} or not block_ids or len(set(block_ids)) != len(block_ids) or len(block_ids) > 500:
            raise ValueError("invalid confirmed source selection")
        queries = {
            "generation": """SELECT b.*,f.course_id,f.class_id,f.sha256 original_sha256,f.license_code,f.license_note,f.visibility,f.owner_id,f.revision source_revision,
                        f.rights_confirmed,f.allow_generation,f.allow_retrieval,f.allow_student_view,f.external_processing_allowed
                    FROM source_blocks b JOIN material_files f ON f.id=b.file_id
                    WHERE b.id IN (SELECT value FROM json_each(?)) AND f.course_id=? AND b.status='confirmed'
                        AND f.rights_confirmed=1 AND f.allow_generation=1""",
            "retrieval": """SELECT b.*,f.course_id,f.class_id,f.sha256 original_sha256,f.license_code,f.license_note,f.visibility,f.owner_id,f.revision source_revision,
                        f.rights_confirmed,f.allow_generation,f.allow_retrieval,f.allow_student_view,f.external_processing_allowed
                    FROM source_blocks b JOIN material_files f ON f.id=b.file_id
                    WHERE b.id IN (SELECT value FROM json_each(?)) AND f.course_id=? AND b.status='confirmed'
                        AND f.rights_confirmed=1 AND f.allow_retrieval=1""",
        }
        with self.connect() as db:
            rows = list(db.execute(queries[purpose], (self._json(list(block_ids)),course_id)))
        result = []
        for row in rows:
            if row["class_id"] is not None and row["class_id"] != class_id:
                continue
            if student and (row["visibility"] != "shared" or not row["student_visible"] or not row["allow_student_view"] or row["kind"] not in {"text","table"}):
                continue
            if row["license_code"] in {"unknown","restricted"} or not row["anchor"].strip():
                continue
            result.append(self._block(row))
        if len(result) != len(block_ids):
            raise PermissionError("every source must be confirmed, in scope, and licensed for this use")
        order = {block_id:index for index,block_id in enumerate(block_ids)}
        return sorted(result,key=lambda block:order[block["id"]])

    def search_confirmed(self, course_id: str, query: str, *, student: bool = False,
                         class_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        if not query.strip() or len(query) > 200 or not 1 <= limit <= 100:
            raise ValueError("invalid source search")
        # Literal matching: '%' and '_' are not wildcard instructions.
        with self.connect() as db:
            return [self._block(r) for r in db.execute("""SELECT b.*,f.course_id,f.class_id,f.sha256 original_sha256,f.license_code
                 FROM source_blocks b JOIN material_files f ON f.id=b.file_id
                 WHERE f.course_id=? AND (f.class_id IS NULL OR f.class_id=?)
                   AND b.status='confirmed' AND f.rights_confirmed=1 AND f.allow_retrieval=1
                   AND f.license_code NOT IN ('unknown','restricted') AND instr(lower(b.text),lower(?))>0
                   AND (?=0 OR (b.student_visible=1 AND f.allow_student_view=1
                                AND f.visibility='shared' AND b.kind IN ('text','table')))
                 ORDER BY f.created_at,b.ordinal LIMIT ?""",(course_id,class_id,query.strip(),int(bool(student)),limit))]

    def delete_draft(self, file_id: str, *, actor_id: str, expected_revision: int) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM material_files WHERE id=?", (file_id,)).fetchone()
            self._assert_draft(db,row,actor_id,expected_revision)
            if db.execute("SELECT 1 FROM content_jobs WHERE input_json LIKE ? AND status IN ('pending','running')", (f'%"{file_id}"%',)).fetchone():
                raise ContentConflict("material has pending jobs")
            db.execute("DELETE FROM material_files WHERE id=?", (file_id,))
            db.execute("DELETE FROM import_batches WHERE id=?", (row["batch_id"],))
        directory = (self.originals_dir / row["storage_key"]).parent
        if directory.parent.resolve() == self.originals_dir.resolve() and not directory.is_symlink():
            shutil.rmtree(directory,ignore_errors=True)

    def add_requirement(self, course_id: str, text: str, *, created_by: str, source_block_ids: Sequence[str]) -> dict[str, Any]:
        if not text.strip() or not source_block_ids:
            raise ValueError("requirement text and source blocks required")
        self.confirmed_sources(course_id,source_block_ids,purpose="retrieval")
        record_id = self._id("req")
        with self.transaction() as db:
            db.execute("INSERT INTO course_requirements(id,course_id,text,created_by,status,created_at) VALUES (?,?,?,?,'draft',?)", (record_id,course_id,text.strip(),created_by,time.time()))
            db.executemany("INSERT INTO requirement_blocks VALUES (?,?)", [(record_id,i) for i in source_block_ids])
        return self.list_requirements(course_id,include_drafts=True,created_by=created_by)[-1]

    def list_requirements(self, course_id: str, *, include_drafts: bool = False, created_by: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            result = [dict(r) for r in db.execute("""SELECT * FROM course_requirements WHERE course_id=?
                AND (status='confirmed' OR (?=1 AND (?=0 OR created_by=?)))
                ORDER BY created_at,id""",(course_id,int(bool(include_drafts)),int(bool(created_by)),created_by))]
            for item in result:
                item["source_block_ids"] = [r[0] for r in db.execute("SELECT block_id FROM requirement_blocks WHERE requirement_id=?", (item["id"],))]
        return result

    def add_knowledge_point(self, course_id: str, code: str, title: str, *, created_by: str,
                            source_block_ids: Sequence[str], description: str = "") -> dict[str, Any]:
        if not code.strip() or not title.strip() or not source_block_ids:
            raise ValueError("knowledge code, title and confirmed sources required")
        self.confirmed_sources(course_id,source_block_ids,purpose="retrieval")
        record_id = self._id("kp")
        with self.transaction() as db:
            db.execute("INSERT INTO knowledge_points(id,course_id,code,title,description,created_by,status,created_at) VALUES (?,?,?,?,?,?,'draft',?)", (record_id,course_id,code.strip(),title.strip(),description,created_by,time.time()))
            db.executemany("INSERT INTO knowledge_sources VALUES (?,?)", [(record_id,i) for i in source_block_ids])
        return next(i for i in self.list_knowledge_points(course_id,include_drafts=True,created_by=created_by) if i["id"] == record_id)

    def list_knowledge_points(self, course_id: str, *, include_drafts: bool = False, created_by: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            result = [dict(r) for r in db.execute("""SELECT * FROM knowledge_points WHERE course_id=?
                AND (status='confirmed' OR (?=1 AND (?=0 OR created_by=?)))
                ORDER BY created_at,id""",(course_id,int(bool(include_drafts)),int(bool(created_by)),created_by))]
            for item in result:
                item["source_block_ids"] = [r[0] for r in db.execute("SELECT block_id FROM knowledge_sources WHERE knowledge_id=?",(item["id"],))]
                item["prerequisite_ids"] = [r[0] for r in db.execute("SELECT prerequisite_id FROM knowledge_prerequisites WHERE knowledge_id=?",(item["id"],))]
        return result

    def set_prerequisites(self, knowledge_id: str, prerequisite_ids: Sequence[str], *, actor_id: str) -> None:
        if len(set(prerequisite_ids)) != len(prerequisite_ids):
            raise ValueError("duplicate prerequisites")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM knowledge_points WHERE id=?", (knowledge_id,)).fetchone()
            if row is None:
                raise KeyError(knowledge_id)
            if row["created_by"] != actor_id:
                raise PermissionError("only the author may edit knowledge drafts")
            if row["status"] != "draft":
                raise ContentConflict("confirmed knowledge is immutable")
            db.execute("DELETE FROM knowledge_prerequisites WHERE knowledge_id=?", (knowledge_id,))
            for prerequisite in prerequisite_ids:
                if prerequisite == knowledge_id:
                    raise ValueError("knowledge prerequisite cycle")
                if not db.execute("SELECT 1 FROM knowledge_points WHERE id=? AND course_id=?",(prerequisite,row["course_id"])).fetchone():
                    raise ValueError("prerequisites must belong to the same course")
                cycle = db.execute("""WITH RECURSIVE ancestors(id) AS (
                    SELECT prerequisite_id FROM knowledge_prerequisites WHERE knowledge_id=?
                    UNION SELECT p.prerequisite_id FROM knowledge_prerequisites p JOIN ancestors a ON p.knowledge_id=a.id)
                    SELECT 1 FROM ancestors WHERE id=? LIMIT 1""", (prerequisite,knowledge_id)).fetchone()
                if cycle:
                    raise ValueError("knowledge prerequisite cycle")
                db.execute("INSERT INTO knowledge_prerequisites VALUES (?,?,?)", (knowledge_id,prerequisite,row["course_id"]))

    def confirm_curriculum(self, kind: str, record_id: str, *, actor_id: str) -> None:
        if kind not in {"requirement","knowledge"}:
            raise ValueError("unknown curriculum kind")
        queries = {
            "requirement": (
                "SELECT * FROM course_requirements WHERE id=?",
                "SELECT 1 FROM requirement_blocks WHERE requirement_id=?",
                "SELECT 1 FROM requirement_blocks l JOIN source_blocks b ON b.id=l.block_id WHERE l.requirement_id=? AND b.status<>'confirmed'",
                "UPDATE course_requirements SET status='confirmed',confirmed_by=?,confirmed_at=? WHERE id=?",
            ),
            "knowledge": (
                "SELECT * FROM knowledge_points WHERE id=?",
                "SELECT 1 FROM knowledge_sources WHERE knowledge_id=?",
                "SELECT 1 FROM knowledge_sources l JOIN source_blocks b ON b.id=l.block_id WHERE l.knowledge_id=? AND b.status<>'confirmed'",
                "UPDATE knowledge_points SET status='confirmed',confirmed_by=?,confirmed_at=? WHERE id=?",
            ),
        }
        select_query, source_query, unconfirmed_query, update_query = queries[kind]
        with self.transaction() as db:
            row = db.execute(select_query, (record_id,)).fetchone()
            if row is None:
                raise KeyError(record_id)
            if row["status"] != "draft":
                raise ContentConflict("curriculum already confirmed")
            if not db.execute(source_query,(record_id,)).fetchone():
                raise ValueError("curriculum must have confirmed sources")
            if db.execute(unconfirmed_query,(record_id,)).fetchone():
                raise ContentConflict("curriculum has unconfirmed sources")
            if kind == "knowledge" and db.execute("SELECT 1 FROM knowledge_prerequisites p JOIN knowledge_points k ON k.id=p.prerequisite_id WHERE p.knowledge_id=? AND k.status<>'confirmed'",(record_id,)).fetchone():
                raise ContentConflict("confirm prerequisites first")
            db.execute(update_query,(actor_id,time.time(),record_id))

    def create_job(self, kind: str, *, course_id: str, owner_id: str, payload: Mapping[str, Any],
                   class_id: str | None = None, job_id: str | None = None) -> dict[str, Any]:
        if kind not in {"material_parse","question_generate","question_edit"}:
            raise ValueError("unknown content job kind")
        job_id, now = job_id or self._id("contentjob"), time.time()
        stage = "parse" if kind == "material_parse" else ("review" if kind == "question_edit" else "generate")
        with self.transaction() as db:
            db.execute("INSERT INTO content_jobs(id,course_id,class_id,owner_id,kind,stage,status,input_json,created_at,updated_at) VALUES (?,?,?,?,?,?,'pending',?,?,?)",(job_id,course_id,class_id,owner_id,kind,stage,self._json(dict(payload)),now,now))
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM content_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["input"] = json.loads(result.pop("input_json"))
        result["state"] = json.loads(result.pop("state_json"))
        return result

    def list_pending_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Durable outbox: dispatcher enqueues queue job_id=content job id; pending remains until a result applies."""
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1 <= limit <= 500:
            raise ValueError("invalid pending job limit")
        with self.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM content_jobs WHERE status IN ('pending','running') ORDER BY created_at,id LIMIT ?",(limit,))]
        return [self.get_job(job_id) for job_id in ids]  # type: ignore[misc]

    @contextmanager
    def job_result_guard(self, job_id: str, *, expected_revision: int, expected_stage: str,
                         expected_generation: int | None = None,
                         is_current: Callable[[], Any] | None = None) -> Iterator[sqlite3.Connection]:
        """Fence a local result. It is entered only after model I/O has completed."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM content_jobs WHERE id=?",(job_id,)).fetchone()
            if (row is None or row["revision"] != expected_revision or row["stage"] != expected_stage
                    or row["status"] not in {"pending","running"}):
                raise ContentConflict("stale or terminal content job result")
            if expected_generation is not None:
                generation = json.loads(row["state_json"]).get("worker_generation")
                if generation != expected_generation:
                    raise ContentConflict("content callback worker generation changed")
            if is_current is not None and is_current() is False:
                raise ContentConflict("content callback lease lost")
            yield db
            # A worker may provide its actual queue lease validator. Raising rolls back domain writes.
            if is_current is not None and is_current() is False:
                raise ContentConflict("content callback lease lost")

    def bind_job_generation(self, job_id: str, generation: int) -> dict[str, Any]:
        """A newly claimed worker can fence older result callbacks before doing local work."""
        if type(generation) is not int or generation < 1:
            raise ValueError("worker generation must be a positive integer")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM content_jobs WHERE id=?",(job_id,)).fetchone()
            if row is None or row["status"] not in {"pending","running"}:
                raise ContentConflict("content job is terminal")
            state = json.loads(row["state_json"])
            old = state.get("worker_generation",0)
            if generation < old:
                raise ContentConflict("worker generation is stale")
            if generation != old:
                state["worker_generation"] = generation
                db.execute("UPDATE content_jobs SET state_json=?,revision=revision+1,updated_at=? WHERE id=?",(self._json(state),time.time(),job_id))
        return self.get_job(job_id)  # type: ignore[return-value]

    @staticmethod
    def _write_job_result(db: sqlite3.Connection, job_id: str, *, expected_revision: int,
                          stage: str, status: str, state_json: str, error: str | None = None) -> None:
        cursor = db.execute("""UPDATE content_jobs SET stage=:stage,status=:status,state_json=:state_json,
            error=:error,revision=revision+1,updated_at=:updated_at
            WHERE id=:job_id AND revision=:expected_revision AND status IN ('pending','running')""",
            {"stage": stage, "status": status, "state_json": state_json, "error": error,
             "updated_at": time.time(), "job_id": job_id, "expected_revision": expected_revision})
        if cursor.rowcount != 1:
            raise ContentConflict("stale or terminal content job result")

    def advance_job(self, job_id: str, *, expected_revision: int, stage: str, status: str,
                    state: Mapping[str, Any], error: str | None = None,
                    expected_generation: int | None = None,
                    is_current: Callable[[], Any] | None = None) -> dict[str, Any]:
        current = self.get_job(job_id)
        if current is None:
            raise ContentConflict("content job not found")
        with self.job_result_guard(job_id,expected_revision=expected_revision,expected_stage=current["stage"],
                                   expected_generation=expected_generation,is_current=is_current) as db:
            self._write_job_result(db,job_id,expected_revision=expected_revision,stage=stage,status=status,state_json=self._json(dict(state)),error=error)
        return self.get_job(job_id)  # type: ignore[return-value]

    def apply_parse_result(self, job_id: str, parsed: Mapping[str, Any], *, expected_revision: int) -> dict[str, Any]:
        # The worker parses private bytes outside this transaction, then atomically applies a local result.
        with self.transaction() as db:
            job = db.execute("SELECT * FROM content_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["kind"] != "material_parse" or job["revision"] != expected_revision or job["status"] != "pending":
                raise ContentConflict("stale parse result")
            payload = json.loads(job["input_json"])
            material = db.execute("SELECT * FROM material_files WHERE id=?", (payload["file_id"],)).fetchone()
            self._assert_draft(db,material,job["owner_id"],payload["material_revision"])
            if parsed.get("status") not in {"parsed","manual_required"}:
                raise ValueError("invalid parse result")
            db.execute("DELETE FROM material_issues WHERE file_id=?", (material["id"],))
            db.execute("DELETE FROM source_blocks WHERE file_id=?", (material["id"],))
            self._insert_parse(db,material["id"],material["batch_id"],parsed)
            if not material["rights_confirmed"]:
                self._insert_issue(db,material["id"],material["batch_id"],"source_permission_required","来源许可尚未人工确认","blocking")
            status = "manual_required" if parsed["status"] == "manual_required" else "draft"
            db.execute("UPDATE material_files SET status=?,revision=revision+1,updated_at=? WHERE id=?",(status,time.time(),material["id"]))
            db.execute("UPDATE import_batches SET status=?,updated_at=? WHERE id=?",(status,time.time(),material["batch_id"]))
            db.execute("UPDATE content_jobs SET status='completed',stage='done',revision=revision+1,state_json=?,updated_at=? WHERE id=?",(self._json({"file_id":material["id"]}),time.time(),job_id))
        return self.get_job(job_id)  # type: ignore[return-value]
