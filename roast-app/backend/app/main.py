"""FastAPI application: batch curves, sourced events, comparison, export,
and offline observation-package import."""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import synth
from .analysis import RoRConfig, build_series, current_events, phase_metrics
from .config import CORS_ORIGINS, MAX_GAP_FILL_S
from .importer import package as pkgmod
from .importer import service as import_service
from .models import Batch, Event, ImportBatch, Sample, engine, init_db
from .schemas import BatchMeta, ConflictResolutionIn, EventIn, EventOut, ImportRequest

app = FastAPI(title="Coffee Roast Batch Explorer", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get_batch(session: Session, batch_id: int) -> Batch:
    b = session.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"batch {batch_id} not found")
    return b


def _samples_as_dicts(batch: Batch) -> list[dict]:
    return [
        {
            "t_s": s.t_s,
            "bean_temp_c": s.bean_temp_c,
            "env_temp_c": s.env_temp_c,
            "source": s.source,
            "source_package_id": s.source_package_id,
        }
        for s in batch.samples
    ]


def _events_as_dicts(batch: Batch, *, include_history: bool) -> list[dict]:
    rows = []
    for e in batch.events:
        if not include_history and e.superseded:
            continue
        rows.append(
            {
                "id": e.id,
                "batch_id": e.batch_id,
                "event_type": e.event_type,
                "t_s": e.t_s,
                "label": e.label,
                "source": e.source,
                "created_by": e.created_by,
                "value_num": e.value_num,
                "note": e.note,
                "superseded": e.superseded,
                "superseded_by_id": e.superseded_by_id,
                "created_at": e.created_at.isoformat(),
                "event_uid": e.event_uid,
                "source_package_id": e.source_package_id,
            }
        )
    return rows


def _provenance(batch: Batch) -> dict[str, Any]:
    """Where every measured point/event on this batch came from."""
    by_pkg_samples: dict[str | None, int] = {}
    for s in batch.samples:
        key = s.source_package_id
        by_pkg_samples[key] = by_pkg_samples.get(key, 0) + 1
    by_pkg_events: dict[str | None, int] = {}
    for e in batch.events:
        key = e.source_package_id
        by_pkg_events[key] = by_pkg_events.get(key, 0) + 1
    return {
        "samples_by_source": [
            {"source_package_id": pid, "count": n}
            for pid, n in sorted(by_pkg_samples.items(), key=lambda kv: (kv[0] is None, str(kv[0])))
        ],
        "events_by_source_package": [
            {"source_package_id": pid, "count": n}
            for pid, n in sorted(by_pkg_events.items(), key=lambda kv: (kv[0] is None, str(kv[0])))
        ],
        "imported_sample_count": sum(
            n for pid, n in by_pkg_samples.items() if pid is not None
        ),
    }


def _series_payload(
    batch: Batch,
    *,
    window_s: float,
    display_smooth_s: float,
    max_gap_fill_s: float,
    include_history: bool,
) -> dict[str, Any]:
    series = build_series(
        _samples_as_dicts(batch),
        ror_cfg=RoRConfig(window_s=window_s, display_smooth_s=display_smooth_s),
        max_gap_fill_s=max_gap_fill_s,
    )
    events = _events_as_dicts(batch, include_history=include_history)
    return {
        "batch": BatchMeta.model_validate(batch).model_dump(mode="json"),
        "series": series,
        "events": events,
        "metrics": phase_metrics(events),
        "provenance": _provenance(batch),
        "params": {
            "ror_window_s": window_s,
            "ror_display_smooth_s": display_smooth_s,
            "max_gap_fill_s": max_gap_fill_s,
            "raw_is_immutable": True,
        },
    }


# ---------------------------------------------------------------------------
# batches / seeding
# ---------------------------------------------------------------------------

@app.get("/api/batches", response_model=list[BatchMeta])
def list_batches() -> list[Batch]:
    with Session(engine) as s:
        return list(s.scalars(select(Batch).order_by(Batch.id)))


@app.post("/api/seed", response_model=list[BatchMeta])
def seed_demo() -> list[Batch]:
    """Load the two synthetic demo batches (noise + dropouts, no machine)."""
    with Session(engine) as s:
        created: list[Batch] = []
        for spec in synth.two_demo_batches():
            existing = s.scalar(select(Batch).where(Batch.name == spec["name"]))
            if existing is not None:
                created.append(existing)
                continue
            b = Batch(
                name=spec["name"],
                roaster=spec["roaster"],
                bean=spec["bean"],
                charge_at=spec["charge_at"],
                charge_temp_c=spec["charge_temp_c"],
                ambient_temp_c=spec["ambient_temp_c"],
                target_drop_temp_c=spec["target_drop_temp_c"],
                note=spec["note"],
            )
            b.samples = [
                Sample(
                    t_s=sp["t_s"],
                    bean_temp_c=sp["bean_temp_c"],
                    env_temp_c=sp["env_temp_c"],
                )
                for sp in spec["samples"]
            ]
            b.events = [Event(**ev) for ev in spec["events"]]
            s.add(b)
            created.append(b)
        s.commit()
        for b in created:
            s.refresh(b)
        return created


@app.get("/api/batches/{batch_id}/series")
def get_series(
    batch_id: int,
    window_s: float = Query(30.0, gt=0, le=300),
    display_smooth_s: float = Query(12.0, ge=0, le=180),
    max_gap_fill_s: float = Query(MAX_GAP_FILL_S, gt=0, le=600),
    include_history: bool = Query(False),
) -> dict[str, Any]:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        return _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=max_gap_fill_s,
            include_history=include_history,
        )


# ---------------------------------------------------------------------------
# events: append-only corrections with provenance
# ---------------------------------------------------------------------------

@app.post("/api/batches/{batch_id}/events", response_model=EventOut)
def add_event(batch_id: int, ev: EventIn) -> Event:
    with Session(engine) as s:
        _get_batch(s, batch_id)
        row = Event(batch_id=batch_id, **ev.model_dump())
        s.add(row)
        s.flush()
        # Only one *current* event per type: supersede the previous current one.
        if row.event_type != "damper_change":
            prev = s.scalars(
                select(Event).where(
                    Event.batch_id == batch_id,
                    Event.event_type == row.event_type,
                    Event.superseded.is_(False),
                    Event.id != row.id,
                )
            ).all()
            for p in prev:
                p.superseded = True
                p.superseded_by_id = row.id
        s.commit()
        s.refresh(row)
        return row


@app.get("/api/batches/{batch_id}/events", response_model=list[EventOut])
def list_events(batch_id: int, include_history: bool = Query(False)) -> list[Event]:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        q = select(Event).where(Event.batch_id == batch_id)
        if not include_history:
            q = q.where(Event.superseded.is_(False))
        return list(s.scalars(q.order_by(Event.t_s)))


# ---------------------------------------------------------------------------
# comparison (no causal claims) + export / recompute
# ---------------------------------------------------------------------------

@app.get("/api/compare")
def compare(
    a: int = Query(..., description="first batch id"),
    b: int = Query(..., description="second batch id"),
    window_s: float = Query(30.0, gt=0, le=300),
    display_smooth_s: float = Query(12.0, ge=0, le=180),
    max_gap_fill_s: float = Query(MAX_GAP_FILL_S, gt=0, le=600),
) -> dict[str, Any]:
    """Overlay two batches on charge-relative time. Damper changes are shown
    as marks so the operator can eyeball before/after shape; the API attaches
    an explicit non-causal note."""
    with Session(engine) as s:
        ba, bb = _get_batch(s, a), _get_batch(s, b)
        payload = {
            "batches": [
                _series_payload(
                    ba,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                ),
                _series_payload(
                    bb,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                ),
            ],
            "interpretation": (
                "曲线按开火/下豆时刻对齐叠加。风门变化以标记线显示，"
                "前后形态仅供观察对比，不构成因果结论（无对照、无重复、无统计检验）。"
            ),
        }
        return payload


@app.get("/api/batches/{batch_id}/export")
def export_batch(batch_id: int, window_s: float = 30.0, display_smooth_s: float = 12.0) -> dict[str, Any]:
    """Self-contained export: raw samples, sourced events, parameters, and the
    derived phase metrics.  The metrics can be reproduced from raw + events +
    the stated window (see /api/recompute)."""
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        payload = _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=MAX_GAP_FILL_S,
            include_history=True,
        )
        payload["export_version"] = 1
        payload["reproducibility"] = {
            "raw_samples_are_source_of_truth": True,
            "metrics_depend_on": ["raw_samples", "current(non-superseded) events", "ror_window_s"],
            "pipeline": "numpy centred least-squares RoR; linear gap fill flagged",
        }
        payload["provenance"]["event_history_by_source_package"] = _event_provenance(b)
        return payload


def _event_provenance(batch: Batch) -> list[dict[str, Any]]:
    """Full append-only event history grouped by originating package."""
    counts: dict[str | None, dict[str, int]] = {}
    for e in batch.events:
        d = counts.setdefault(e.source_package_id, {"total": 0, "superseded": 0})
        d["total"] += 1
        if e.superseded:
            d["superseded"] += 1
    return [
        {"source_package_id": pid, "total": d["total"], "superseded": d["superseded"]}
        for pid, d in sorted(counts.items(), key=lambda kv: (kv[0] is not None, str(kv[0])))
    ]


@app.post("/api/recompute")
def recompute(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-derive series + metrics from an export-style payload.

    Used to verify an export reproduces every stage metric without touching
    the database.  Body: {"samples": [...], "events": [...], "params": {...}}.
    """
    try:
        samples = payload["samples"]
        events = payload.get("events", [])
        params = payload.get("params", {})
    except KeyError as exc:
        raise HTTPException(422, f"missing field: {exc}")
    # Preserve per-sample provenance if the export carried it; recompute must
    # be able to reproduce *which package* every reading came from.
    samples = [
        {
            "t_s": s["t_s"],
            "bean_temp_c": s.get("bean_temp_c"),
            "env_temp_c": s.get("env_temp_c"),
            "source": s.get("source"),
            "source_package_id": s.get("source_package_id"),
        }
        for s in samples
    ]
    cfg = RoRConfig(
        window_s=float(params.get("ror_window_s", 30.0)),
        display_smooth_s=float(params.get("ror_display_smooth_s", 12.0)),
    )
    series = build_series(
        samples,
        ror_cfg=cfg,
        max_gap_fill_s=float(params.get("max_gap_fill_s", MAX_GAP_FILL_S)),
    )
    return {
        "series": series,
        "metrics": phase_metrics(events),
        "current_events": current_events(events),
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "machine_connection": "none (synthetic/offline only)"}


# ---------------------------------------------------------------------------
# offline observation-package import (local files only; never uploaded)
# ---------------------------------------------------------------------------
#
# Lifecycle:
#   POST /api/imports                 receive  -> pending_review (or failed)
#   GET  /api/imports                 ledger
#   GET  /api/imports/{id}/preview    projection + conflicts (writes nothing)
#   POST /api/imports/{id}/resolve    adjudicate conflicts
#   POST /api/imports/{id}/apply      atomic apply (one transaction)
#   POST /api/imports/{id}/abort      discard a pending delivery
#
# The browser reads a locally chosen file and POSTs the parsed JSON to this
# same-origin API.  Nothing is sent to any machine or external service.


@app.post("/api/imports")
def import_receive(req: ImportRequest) -> dict[str, Any]:
    """Accept one observation package into the ledger (pending review).

    Invalid packages fail *as a whole*: the only write is a ``failed`` ledger
    row carrying every validation finding — no batch/sample/event changes.
    Re-posting the identical package returns the original ledger row; the
    same ``package_id`` with different content is rejected with 409.
    """
    raw = req.package
    raw_text = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    with Session(engine) as s:
        actual_digest: str | None = None
        if isinstance(raw, dict):
            try:
                actual_digest = pkgmod.canonical_digest(raw)
            except (TypeError, ValueError):
                actual_digest = None
        try:
            pkg = pkgmod.validate_package(raw)
        except pkgmod.PackageError as exc:
            imp = import_service.record_failure(
                s,
                raw_text=raw_text,
                raw=raw,
                error_code=exc.code,
                findings=exc.findings,
                actual_digest=actual_digest,
            )
            # The failed ledger row is already committed (audit trail); the
            # 422 only signals that nothing was applied.
            raise HTTPException(
                status_code=422,
                detail={
                    "ok": False,
                    "import": import_service.ledger_summary(imp),
                    "error_code": exc.code,
                    "findings": exc.findings,
                },
            )
        imp = import_service.receive_package(
            s,
            raw=raw,
            raw_text=raw_text,
            pkg=pkg,
            target_batch_id=req.target_batch_id,
            created_by=req.created_by,
            actual_digest=actual_digest,
        )
        n_conflicts = len(imp.conflicts)
        return {
            **import_service.ledger_summary(imp),
            "ok": True,
            "conflict_count": n_conflicts,
            "next_step": (
                "apply"
                if n_conflicts == 0 and imp.status == "pending_review"
                else "review_conflicts"
            ),
        }


@app.get("/api/imports")
def import_list(status: str | None = Query(None)) -> list[dict[str, Any]]:
    with Session(engine) as s:
        q = select(ImportBatch).order_by(ImportBatch.received_at.desc(), ImportBatch.id.desc())
        rows = list(s.scalars(q))
        if status:
            rows = [r for r in rows if r.status == status]
        out = []
        for r in rows:
            d = import_service.ledger_summary(r)
            d["conflict_count"] = len(r.conflicts)
            d["unresolved_count"] = sum(
                1 for c in r.conflicts if c.resolution is None
            )
            out.append(d)
        return out


def _get_import(session: Session, import_id: int) -> ImportBatch:
    imp = session.get(ImportBatch, import_id)
    if imp is None:
        raise HTTPException(404, f"import {import_id} not found")
    return imp


@app.get("/api/imports/{import_id}/preview")
def import_preview(
    import_id: int,
    window_s: float = Query(30.0, gt=0, le=300),
    display_smooth_s: float = Query(12.0, ge=0, le=180),
    max_gap_fill_s: float = Query(MAX_GAP_FILL_S, gt=0, le=600),
) -> dict[str, Any]:
    with Session(engine) as s:
        imp = _get_import(s, import_id)
        return import_service.preview_import(
            s,
            imp,
            ror_cfg=RoRConfig(window_s=window_s, display_smooth_s=display_smooth_s),
            max_gap_fill_s=max_gap_fill_s,
        )


@app.post("/api/imports/{import_id}/resolve")
def import_resolve(import_id: int, req: ConflictResolutionIn) -> dict[str, Any]:
    with Session(engine) as s:
        imp = _get_import(s, import_id)
        imp = import_service.resolve_conflicts(
            s,
            imp,
            resolutions=req.resolutions,
            resolved_by=req.resolved_by,
        )
        d = import_service.ledger_summary(imp)
        d["conflicts"] = [import_service._conflict_out(c) for c in imp.conflicts]
        d["unresolved_count"] = sum(
            1 for c in imp.conflicts if c.resolution is None
        )
        return d


@app.post("/api/imports/{import_id}/apply")
def import_apply(import_id: int) -> dict[str, Any]:
    """Apply one reviewed package atomically.

    Requires zero unresolved conflicts.  Samples/events are written in a
    single transaction; any failure rolls back completely.  Re-applying an
    already-applied package returns the original result (idempotency)."""
    with Session(engine) as s:
        imp = _get_import(s, import_id)
        return import_service.apply_import(s, imp, applied_by=imp.created_by or "operator")


@app.post("/api/imports/{import_id}/abort")
def import_abort(import_id: int) -> dict[str, Any]:
    with Session(engine) as s:
        imp = _get_import(s, import_id)
        return import_service.abort_import(s, imp)
