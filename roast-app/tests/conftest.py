"""Pytest fixtures.

Defaults to a local SQLite file so ``pytest`` needs no infrastructure; point
DATABASE_URL at PostgreSQL to run the identical suite against it:

    DATABASE_URL=postgresql+psycopg2://roast:roast@localhost:5432/roast pytest
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

# Must be set BEFORE importing the app (engine is created at import time).
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app import models  # noqa: E402
from app.main import app  # noqa: E402

# Schema is created (and migrated) once at import time, before any test or
# fixture runs; per-test cleanup only deletes data rows.
models.Base.metadata.drop_all(models.engine)
models.init_db()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_tables():
    """Each test starts from empty data tables (schema stays in place)."""
    with models.engine.begin() as conn:
        for table in reversed(models.Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield
