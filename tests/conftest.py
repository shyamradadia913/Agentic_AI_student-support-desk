import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("PBKDF2_ITERS", "1000")  # fast hashing in tests only
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app import config, seed
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    seed.seed(path)
    return path


@pytest.fixture(autouse=True)
def _routing_off_by_default(monkeypatch):
    """Most tests drive tools directly with a scripted model and a throwaway question; routing is tested explicitly in test_token_saver.py."""
    from app import config
    monkeypatch.setattr(config, "TOOL_ROUTING", "off")
