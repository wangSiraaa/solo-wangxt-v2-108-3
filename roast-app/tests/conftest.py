"""Pytest fixtures.

Defaults to a local SQLite file so ``pytest`` needs no infrastructure; point
DATABASE_URL at PostgreSQL to run the identical suite against it:

    DATABASE_URL=postgresql+psycopg2://roast:roast@localhost:5432/roast pytest

The database is recreated for every test function so audit-ledger and batch
state never leaks between cases (and each run also exercises the schema
bootstrap).
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


@pytest.fixture()
def client():
    # deterministic start: recreate every table for every test
    models.Base.metadata.drop_all(models.engine)
    models.Base.metadata.create_all(models.engine)
    with TestClient(app) as c:
        yield c
