import pytest

from app import seed
from app.db import connect


@pytest.fixture
def db(monkeypatch, tmp_path):
    """A seeded database in an isolated temp dir; never touches real data."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    c = connect()
    yield c
    c.close()
