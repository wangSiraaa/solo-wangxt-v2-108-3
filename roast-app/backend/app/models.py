"""SQLAlchemy models: batches, *raw* samples, sourced events and the
offline observation-package import ledger.

Design rules enforced at the storage layer:

* ``samples`` only ever contains measured samples.  Interpolation for gaps is
  computed at query time and is returned with ``is_interpolated=True`` — it is
  never written back here, so a plotting convenience can never masquerade as a
  measurement.
* Every measured sample keeps its provenance: ``source`` (e.g. ``synthetic``
  or ``observation_package``) and the stable ``import_package_id`` of the
  offline observation package it arrived in.  Samples are immutable history as
  well: when an adjudicated conflict replaces a reading at the same timestamp
  the old row is marked ``superseded`` and linked, never deleted.  A partial
  unique index guarantees at most one *current* reading per (batch, t_s).
* Events (turning point, first crack, damper change, drop ...) are append-only.
  A correction — manual or imported — supersedes the previous row instead of
  deleting it, so every value keeps its ``source`` / ``created_by`` /
  package provenance.
* ``observation_imports`` is the audit ledger for offline package delivery:
  one row per delivered (package_id, content digest), with its lifecycle state
  (pending review / applied / rejected / discarded), the original payload and
  the adjudication decisions.  Rejected packages leave rows here and nothing
  anywhere else.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import DATABASE_URL


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    roaster: Mapped[str] = mapped_column(String(120), default="synthetic")
    bean: Mapped[str] = mapped_column(String(120), default="")
    charge_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    charge_temp_c: Mapped[float] = mapped_column(Float)
    ambient_temp_c: Mapped[float] = mapped_column(Float)
    target_drop_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    samples: Mapped[list["Sample"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Sample.t_s"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Event.t_s"
    )


class Sample(Base):
    """One raw probe reading.  Temperatures are NULL when the probe was
    briefly lost — the missing reading is preserved as missing, not invented.

    Readings never disappear: a same-timestamp conflict resolved in favour of a
    later observation package marks the previous row ``superseded`` (linked via
    ``superseded_by_sample_id``).  Only current rows feed analysis/export."""

    __tablename__ = "samples"
    __table_args__ = (
        # At most one CURRENT reading per (batch, t_s).  Superseded readings are
        # retained as history and exempt from the constraint.  Declared for
        # both dialects the project ships on.
        Index(
            "uq_sample_batch_t_current",
            "batch_id",
            "t_s",
            unique=True,
            sqlite_where=text("superseded = 0"),
            postgresql_where=text("superseded = FALSE"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # Seconds since charge.  Intervals are intentionally uneven.
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    sampled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bean_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    env_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Provenance: where the reading was recorded and which offline package
    # delivered it.  Denormalised package id so export/recompute keep the
    # stable identifier even if the ledger row is later pruned.
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="synthetic")
    import_id: Mapped[int | None] = mapped_column(
        ForeignKey("observation_imports.id"), nullable=True
    )
    import_package_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True, index=True
    )
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    superseded_by_sample_id: Mapped[int | None] = mapped_column(
        ForeignKey("samples.id"), nullable=True
    )

    batch: Mapped[Batch] = relationship(back_populates="samples")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # turning_point | first_crack_start | first_crack_end | drop |
    # damper_change | charge | custom
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    # auto = detected from the raw series; manual = operator entry.
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    # Numeric payload, e.g. new damper position (%) for damper_change.
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id"), nullable=True
    )
    # Offline-package provenance (NULL for UI-entered corrections and seeds).
    import_id: Mapped[int | None] = mapped_column(
        ForeignKey("observation_imports.id"), nullable=True
    )
    import_package_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped[Batch] = relationship(back_populates="events")


class ObservationImport(Base):
    """Audit ledger for one delivery of an offline observation package.

    The natural idempotency key is (package_id, content_sha256): an exact
    redelivery is the same delivery and returns the recorded result; the same
    stable package id with different content is a tamper/version conflict and
    is refused.  Lifecycle:

    * ``pending_review`` — parsed, whole-package validation passed, awaiting
      preview/conflict adjudication and the explicit one-shot apply;
    * ``applied`` — samples/events written in a single transaction;
    * ``rejected`` — whole-package failure (parse / invalid content); the raw
      payload and structured error are kept here, nothing else was written;
    * ``discarded`` — operator dismissed a pending package (audit row kept).
    """

    __tablename__ = "observation_imports"
    __table_args__ = (
        UniqueConstraint(
            "package_id", "content_sha256", name="uq_import_package_content"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Nullable only for unparseable payloads where no id can be extracted.
    package_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    format_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Resolved lazily on apply (batch matched by name; created on first apply).
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("batches.id"), nullable=True, index=True
    )
    batch_name: Mapped[str | None] = mapped_column(String(120), nullable=True)

    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    conflict_report_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # ref -> "keep_existing" | "use_incoming" | "supersede"
    resolutions_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_detail: Mapped[str] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)


# ---------------------------------------------------------------------------
# Lightweight, dependency-free schema migration for existing databases.
# Fresh databases are created directly from the metadata above; this only
# upgrades older files (added provenance columns, sample supersede history).
# ---------------------------------------------------------------------------

_SAMPLE_NEW_COLUMNS = [
    ("source", "VARCHAR(40) NOT NULL DEFAULT 'synthetic'"),
    ("import_id", "INTEGER"),
    ("import_package_id", "VARCHAR(120)"),
    ("superseded", "BOOLEAN NOT NULL DEFAULT 0"),
    ("superseded_by_sample_id", "INTEGER"),
]
_EVENT_NEW_COLUMNS = [
    ("import_id", "INTEGER"),
    ("import_package_id", "VARCHAR(120)"),
]


def _migrate_sqlite(conn) -> None:
    def table_cols(table: str) -> set[str]:
        return {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}

    if "samples" in inspect(conn).get_table_names():
        cols = table_cols("samples")
        idx_rows = conn.exec_driver_sql("PRAGMA index_list(samples)").all()
        # Old schema enforced UNIQUE(batch_id, t_s) via an auto index.  It must
        # be replaced by the partial index, which requires a table rebuild.
        legacy_unique = any(
            r[1].startswith("sqlite_autoindex") and r[2] == 1 for r in idx_rows
        )
        if legacy_unique:
            # SQLite keeps explicitly-named indexes on a renamed table, so
            # drop those first (table-constraint auto indexes move with — and
            # are removed by — the DROP TABLE).
            for idx in conn.exec_driver_sql("PRAGMA index_list(samples)").all():
                if not idx[1].startswith("sqlite_autoindex"):
                    conn.exec_driver_sql(f"DROP INDEX IF EXISTS {idx[1]}")
            conn.exec_driver_sql("ALTER TABLE samples RENAME TO samples_legacy")
            Sample.__table__.create(conn, checkfirst=True)
            conn.exec_driver_sql(
                """
                INSERT INTO samples
                  (id, batch_id, t_s, sampled_at, bean_temp_c, env_temp_c,
                   source, import_id, import_package_id, superseded,
                   superseded_by_sample_id)
                SELECT id, batch_id, t_s, sampled_at, bean_temp_c, env_temp_c,
                       'synthetic', NULL, NULL, 0, NULL
                  FROM samples_legacy
                """
            )
            conn.exec_driver_sql("DROP TABLE samples_legacy")
        else:
            for name, ddl in _SAMPLE_NEW_COLUMNS:
                if name not in cols:
                    conn.exec_driver_sql(
                        f"ALTER TABLE samples ADD COLUMN {name} {ddl}"
                    )
            present = {r[1] for r in conn.exec_driver_sql("PRAGMA index_list(samples)")}
            if "uq_sample_batch_t_current" not in present:
                conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sample_batch_t_current "
                    "ON samples (batch_id, t_s) WHERE superseded = 0"
                )

    if "events" in inspect(conn).get_table_names():
        cols = table_cols("events")
        for name, ddl in _EVENT_NEW_COLUMNS:
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE events ADD COLUMN {name} {ddl}")


def _migrate_postgres(conn) -> None:
    stmts = [
        "ALTER TABLE samples ADD COLUMN IF NOT EXISTS source VARCHAR(40) NOT NULL DEFAULT 'synthetic'",
        "ALTER TABLE samples ADD COLUMN IF NOT EXISTS import_id INTEGER",
        "ALTER TABLE samples ADD COLUMN IF NOT EXISTS import_package_id VARCHAR(120)",
        "ALTER TABLE samples ADD COLUMN IF NOT EXISTS superseded BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE samples ADD COLUMN IF NOT EXISTS superseded_by_sample_id INTEGER",
        "ALTER TABLE samples DROP CONSTRAINT IF EXISTS uq_sample_batch_t",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_sample_batch_t_current "
        "ON samples (batch_id, t_s) WHERE superseded = FALSE",
        "CREATE INDEX IF NOT EXISTS ix_samples_import_package_id "
        "ON samples (import_package_id)",
        "CREATE INDEX IF NOT EXISTS ix_samples_superseded ON samples (superseded)",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS import_id INTEGER",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS import_package_id VARCHAR(120)",
    ]
    for stmt in stmts:
        conn.exec_driver_sql(stmt)


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            _migrate_sqlite(conn)
        else:
            _migrate_postgres(conn)
