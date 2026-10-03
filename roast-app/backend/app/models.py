"""SQLAlchemy models: batches, *raw* samples, sourced events, and the
offline observation-package import ledger.

Design rules enforced at the storage layer:

* ``samples`` only ever contains measured samples.  Interpolation for gaps is
  computed at query time and is returned with ``is_interpolated=True`` — it is
  never written back here, so a plotting convenience can never masquerade as a
  measurement.
* Events (turning point, first crack, damper change, drop ...) are append-only.
  A manual correction supersedes the previous row instead of deleting it, so
  every value keeps its ``source`` / ``created_by`` provenance.
* Every imported row keeps its origin: ``source`` says what produced it and
  ``source_package_id`` carries the stable id of the offline observation
  package.  Imported events additionally keep the package-local ``event_uid``
  so repeated/out-of-order deliveries are deduplicated and supersede chains
  stay intact.
* Observation packages never touch ``samples``/``events`` while under review:
  the raw package sits in ``import_batches`` (status ``pending_review``) and
  per-reading adjudication rows live in ``import_conflicts``.  Application is
  one transaction — there is never a half-written curve.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
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
    briefly lost — the missing reading is preserved as missing, not invented."""

    __tablename__ = "samples"
    __table_args__ = (UniqueConstraint("batch_id", "t_s", name="uq_sample_batch_t"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # Seconds since charge.  Intervals are intentionally uneven.
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    sampled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bean_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    env_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Provenance: synthetic generator, or the stable id of the offline
    # observation package that delivered this measured reading.
    source: Mapped[str] = mapped_column(String(20), default="synthetic", nullable=False)
    source_package_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    batch: Mapped[Batch] = relationship(back_populates="samples")


class Event(Base):
    __tablename__ = "events"
    # Uniqueness on (batch_id, event_uid) is provided by the named index
    # created in init_db/_upgrade_existing_schema — declared once so fresh and
    # upgraded databases (SQLite and PostgreSQL) share the exact same object.

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Offline-package provenance.  event_uid is the stable id assigned by the
    # field recorder inside one package; re-delivering the same package (or the
    # same event from a retried delivery) is recognised by it.
    event_uid: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_package_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )

    batch: Mapped[Batch] = relationship(back_populates="events")


class ImportBatch(Base):
    """Ledger row for one received observation package.

    Lifecycle: ``pending_review`` -> (``applied`` | ``aborted``); structurally
    invalid or digest-mismatched deliveries are recorded as ``failed``.  The
    original package text is retained verbatim for audit; ``result_json``
    holds the apply result so an identical retry is answered with the same
    answer instead of writing anything twice.
    """

    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # pending_review | applied | failed | aborted
    status: Mapped[str] = mapped_column(String(20), default="pending_review", index=True)

    # Merge target: an existing batch, or a batch created at apply time.
    target_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("batches.id"), nullable=True
    )
    created_batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    digest_algorithm: Mapped[str] = mapped_column(String(20), default="sha256")
    digest_expected: Mapped[str | None] = mapped_column(String(128), nullable=True)
    digest_actual: Mapped[str | None] = mapped_column(String(128), nullable=True)

    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80), default="operator")

    n_samples: Mapped[int] = mapped_column(Integer, default=0)
    n_events: Mapped[int] = mapped_column(Integer, default=0)

    # Structured failure detail (code + per-finding list) for failed rows.
    error_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The exact answer returned when the package was applied — idempotency key.
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    conflicts: Mapped[list["ImportConflict"]] = relationship(
        back_populates="import_batch",
        cascade="all, delete-orphan",
        order_by="ImportConflict.t_s",
    )


class ImportConflict(Base):
    """One reading that needs a human decision before a package is applied.

    A conflict is only about *measured values at the same second*.  New time
    points and identical re-deliveries are not conflicts.  The incoming and
    existing values are kept verbatim so the decision is auditable even after
    the winner has been written to ``samples``.
    """

    __tablename__ = "import_conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batches.id"), index=True
    )
    # value_mismatch: both measured, readings differ
    # missing_fill:  stored reading was NULL, package offers a measurement
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    # bean | env
    channel: Mapped[str] = mapped_column(String(10), nullable=False)
    existing_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    incoming_value: Mapped[float | None] = mapped_column(Float, nullable=True)

    # null while pending; keep_existing | use_incoming once decided
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    resolved_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    import_batch: Mapped[ImportBatch] = relationship(back_populates="conflicts")


_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)

# Columns added after the first release; existing databases (notably the local
# SQLite demo/test files created by older code) are upgraded in place.  New
# databases simply get them from the model definitions.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "samples": {
        "source": "VARCHAR(20) NOT NULL DEFAULT 'synthetic'",
        "source_package_id": "VARCHAR(64)",
    },
    "events": {
        "event_uid": "VARCHAR(120)",
        "source_package_id": "VARCHAR(64)",
    },
}


def _upgrade_existing_schema() -> None:
    """Add provenance columns / unique index to a pre-existing database."""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table_name, columns in _ADDED_COLUMNS.items():
            if table_name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table_name)}
            for col_name, col_ddl in columns.items():
                if col_name not in present:
                    conn.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_ddl}")
                    )
        if "events" in existing_tables:
            indexes = {ix["name"]: ix for ix in inspector.get_indexes("events")}
            # Earlier schema had a globally-unique event_uid; a recorder uid is
            # only meaningful within its batch, so replace it with the
            # composite index.  Fresh tables get the same named index here.
            # NULLs never collide in SQLite or PostgreSQL.
            if "ux_events_event_uid" in indexes:
                conn.execute(text("DROP INDEX IF EXISTS ux_events_event_uid"))
            if "ux_events_batch_event_uid" not in indexes:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX ux_events_batch_event_uid "
                        "ON events (batch_id, event_uid)"
                    )
                )


def init_db() -> None:
    Base.metadata.create_all(engine)
    _upgrade_existing_schema()
