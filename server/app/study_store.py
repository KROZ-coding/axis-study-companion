from copy import deepcopy

from .store import Store


class StudyStore(Store):
    """Reuse the upstream SQLite snapshots without seeding audit-course data."""

    def _seed(self) -> None:
        pass

    def practice_answers(self, student_id: str) -> dict[str, int]:
        with self.lock:
            return dict(self.reports.get("study-practice", {}).get(student_id, {}))

    def save_practice(self, student_id: str, practice_id: str, answer: int) -> bool:
        with self._save_lock, self.lock:
            previous = self.reports.get("study-practice", {})
            updated = deepcopy(previous)
            updated.setdefault(student_id, {})[practice_id] = answer
            self.reports["study-practice"] = updated
            if not self.save():
                self.reports["study-practice"] = previous
                return False
            return True
