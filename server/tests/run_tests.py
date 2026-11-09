"""Run backend tests without importing the application against local user data."""

import os
from pathlib import Path
import sys
import tempfile
import unittest


def main() -> int:
    server_dir = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="study-test-bootstrap-") as directory:
        os.environ.update(
            SKIP_ENV_FILE="1", APP_ENV="test", DATA_DIR=directory, STUDY_DATA_DIR=directory,
            PERSISTENCE_ENABLED="false", STUDY_PERSISTENCE_ENABLED="false",
            LLM_PROVIDERS_JSON="", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="",
            MAXKB_URL="", MAXKB_API_KEY="", MAXKB_DATASET_IDS="", MAXKB_COURSE_DATASETS="",
        )
        sys.path.insert(0, str(server_dir))
        suite = unittest.defaultTestLoader.discover(str(server_dir / "tests"), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
