"""Legacy-schema migration test: a database created by the pre-import version
of the app (plain UNIQUE(batch_id, t_s), no provenance columns) must upgrade
in place, keep every reading and gain the partial current-only index.
"""
import os
import sys

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

pytestmark = pytest.mark.skipif(
    os.environ.get("DATABASE_URL", "").startswith("postgres"),
    reason="legacy file rebuild path is SQLite-specific",
)


def test_sqlite_legacy_schema_migrates_with_data(client):
    from app import models

    legacy = models.Base.metadata.tables["samples"]
    # Build a throwaway legacy database on the SAME engine (file-backed sqlite
    # in tests): drop new schema, recreate the old shapes by hand.
    with models.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS samples")
        conn.exec_driver_sql("DROP TABLE IF EXISTS observation_imports")
        conn.exec_driver_sql(
            """
            CREATE TABLE samples (
                id INTEGER NOT NULL PRIMARY KEY,
                batch_id INTEGER NOT NULL,
                t_s FLOAT NOT NULL,
                sampled_at DATETIME,
                bean_temp_c FLOAT,
                env_temp_c FLOAT,
                CONSTRAINT uq_sample_batch_t UNIQUE (batch_id, t_s)
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO samples (batch_id, t_s, bean_temp_c, env_temp_c) "
            "VALUES (1, 0.0, 180.0, 190.0), (1, 60.0, 110.0, 198.0)"
        )

    models.init_db()  # runs the migration

    with models.engine.begin() as conn:
        rows = conn.exec_driver_sql(
            "SELECT t_s, source, superseded FROM samples ORDER BY t_s"
        ).all()
        assert rows == [(0.0, "synthetic", 0), (60.0, "synthetic", 0)]
        # provenance columns now exist
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(samples)")}
        assert {
            "source", "import_id", "import_package_id", "superseded",
            "superseded_by_sample_id",
        } <= cols
        # partial unique index active: two current rows at the same t fail,
        # while a superseded history row is allowed
        conn.exec_driver_sql(
            "INSERT INTO samples (batch_id, t_s, bean_temp_c, env_temp_c, source, superseded) "
            "VALUES (1, 60.0, 111.0, 198.0, 'x', 1)"
        )
        with pytest.raises(Exception):
            conn.exec_driver_sql(
                "INSERT INTO samples (batch_id, t_s, bean_temp_c, env_temp_c, source, superseded) "
                "VALUES (1, 60.0, 112.0, 198.0, 'y', 0)"
            )
        conn.exec_driver_sql("DELETE FROM samples WHERE t_s = 60.0 AND source = 'x'")

    # running init again is a harmless no-op
    models.init_db()
