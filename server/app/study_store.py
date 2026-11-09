from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
import json
from typing import Any

from .config import Settings
from .store import Store


class TeachingPersistenceError(OSError):
    """A teaching change was rolled back because it could not be saved."""


class StudyStore(Store):
    """Keep teaching facts outside report caches in the upstream SQLite snapshot.

    Mutate teaching_state only inside teaching_transaction(), and read through
    teaching_snapshot(). The transaction owns both store locks until save finishes;
    callers must copy any returned objects before exposing them outside that scope.
    Like Store, this repository supports one application process, not many workers.
    """

    TEACHING_SCHEMA_VERSION = 1
    _TEACHING_COLLECTIONS = (
        "reviews", "followups", "tasks", "attempts", "feedback", "idempotency",
        "known_last_choices", "migrations",
    )

    def __init__(self, settings: Settings):
        # Store.__init__ dispatches to our load/snapshot overrides.
        self.teaching_state = self._empty_teaching_state()
        self._teaching_transaction_active = False
        super().__init__(settings)

    def _seed(self) -> None:
        pass

    @classmethod
    def _empty_teaching_state(cls) -> dict[str, Any]:
        return {
            "schema_version": cls.TEACHING_SCHEMA_VERSION,
            **{key: {} for key in cls._TEACHING_COLLECTIONS},
        }

    @classmethod
    def _validate_teaching_state(cls, state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            raise ValueError("invalid teaching state")
        version = state.get("schema_version")
        if type(version) is not int or version != cls.TEACHING_SCHEMA_VERSION:
            raise ValueError("unsupported teaching schema version")
        if any(not isinstance(state.get(key), dict) for key in cls._TEACHING_COLLECTIONS):
            raise ValueError("invalid teaching collection")
        # Reject non-JSON objects and NaN even when persistence is disabled.
        json.dumps(state, ensure_ascii=False, allow_nan=False)

    @classmethod
    def _teaching_from_state(cls, state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        teaching = deepcopy(state["teaching_state"]) if "teaching_state" in state else cls._empty_teaching_state()
        if not isinstance(teaching, dict):
            raise ValueError("invalid teaching state")
        for key in cls._TEACHING_COLLECTIONS:
            teaching.setdefault(key, {})
        cls._validate_teaching_state(teaching)

        reports = state.get("reports", {})
        if not isinstance(reports, dict):
            raise ValueError("invalid reports state")
        changed = "teaching_state" not in state
        if "study-practice" in reports:
            changed = True
            if "study-practice" not in teaching["migrations"]:
                legacy = reports["study-practice"]
                if not isinstance(legacy, dict):
                    raise ValueError("invalid legacy practice state")
                for student_id, choices in legacy.items():
                    if not isinstance(student_id, str) or not isinstance(choices, dict):
                        raise ValueError("invalid legacy practice state")
                    if any(not isinstance(key, str) or type(value) is not int for key, value in choices.items()):
                        raise ValueError("invalid legacy practice choice")
                    current = teaching["known_last_choices"].setdefault(student_id, {})
                    if not isinstance(current, dict):
                        raise ValueError("invalid known practice choices")
                    # A restored old cache must never overwrite newer saved choices.
                    for practice_id, answer in choices.items():
                        current.setdefault(practice_id, answer)
                teaching["migrations"]["study-practice"] = {
                    "source": "reports.study-practice",
                    "original": deepcopy(legacy),
                }
        return teaching, changed

    def teaching_snapshot(self) -> dict[str, Any]:
        """Return an isolated JSON-compatible copy, never uncommitted teaching data."""
        with self.lock:
            return deepcopy(self.teaching_state)

    @contextmanager
    def teaching_transaction(self) -> Iterator[dict[str, Any]]:
        """Atomically save teaching facts, rolling back on business or save errors.

        Business exceptions keep their original type. Save/encoding failures raise
        TeachingPersistenceError, which HTTP adapters can translate to status 503.
        Version checks and idempotency decisions belong inside this transaction.
        Nested transactions are rejected to prevent an inner save escaping rollback.
        """
        with self._save_lock, self.lock:
            if self._teaching_transaction_active:
                raise RuntimeError("nested teaching transactions are not supported")
            previous = deepcopy(self.teaching_state)
            previous_dirty = self._dirty
            self._teaching_transaction_active = True
            try:
                yield self.teaching_state
                try:
                    self._validate_teaching_state(self.teaching_state)
                    if not self.save():
                        raise TeachingPersistenceError("teaching state could not be persisted")
                except TeachingPersistenceError:
                    raise
                except Exception as error:
                    raise TeachingPersistenceError("teaching state could not be persisted") from error
            except BaseException:
                self.teaching_state = previous
                self._dirty = previous_dirty
                raise
            finally:
                self._teaching_transaction_active = False

    def _snapshot(self) -> dict[str, Any]:
        with self.lock:
            snapshot = super()._snapshot()
            snapshot["teaching_state"] = deepcopy(self.teaching_state)
            return snapshot

    def _encode_state(self, snapshot: dict[str, Any], *, version: int) -> str:
        state = json.loads(super()._encode_state(snapshot, version=version))
        state["teaching_state"] = snapshot["teaching_state"]
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def _state(self, *, include_runtime: bool = True) -> dict[str, Any]:
        with self.lock:
            state = super()._state(include_runtime=include_runtime)
            state["teaching_state"] = deepcopy(self.teaching_state)
            return state

    def _apply_state(self, state: dict[str, Any], *, include_runtime: bool = True) -> None:
        if not isinstance(state, dict):
            raise ValueError("invalid store state")
        # Capture legacy facts before Store's version-1 migration clears reports.
        teaching, changed = self._teaching_from_state(state)
        with self.lock:
            super()._apply_state(state, include_runtime=include_runtime)
            self.teaching_state = teaching
            self.reports.pop("study-practice", None)
            self._needs_migration = self._needs_migration or changed

    def _restore_state(self, state: dict[str, Any]) -> None:
        # Upstream restore captures/applies/saves/rolls back via the overrides above.
        # Hold the same locks as teaching transactions throughout that sequence.
        with self._save_lock, self.lock:
            previous_dirty = self._dirty
            previous_migration = self._needs_migration
            try:
                super()._restore_state(state)
            except BaseException:
                self._dirty = previous_dirty
                self._needs_migration = previous_migration
                raise

    def practice_answers(self, student_id: str) -> dict[str, int]:
        with self.lock:
            return deepcopy(self.teaching_state["known_last_choices"].get(student_id, {}))

    def save_practice(self, student_id: str, practice_id: str, answer: int) -> bool:
        try:
            with self.teaching_transaction() as state:
                state["known_last_choices"].setdefault(student_id, {})[practice_id] = answer
        except TeachingPersistenceError:
            return False
        return True
