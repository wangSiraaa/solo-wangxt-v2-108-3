"""FastAPI application: batch curves, sourced events, comparison, export,
and offline observation-package import (local files only)."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import importer, synth
from .analysis import RoRConfig, build_series, current_events, phase_metrics
from .config import CORS_ORIGINS, MAX_GAP_FILL_S
from .importer import PackageError, ValidPackage, apply_import, build_preview
from .models import Batch, Event, ObservationImport, Sample, engine, init_db
from .schemas import BatchMeta, EventIn, EventOut, ImportResolutions

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
    # Only CURRENT readings feed analysis.  Superseded readings stay in the
    # database as history but are excluded from the curve.
    return [
        {
            "t_s": s.t_s,
            "bean_temp_c": s.bean_temp_c,
            "env_temp_c": s.env_temp_c,
            "source": s.source,
            "import_package_id": s.import_package_id,
        }
        for s in batch.samples
        if not s.superseded
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
                "import_package_id": e.import_package_id,
                "created_at": e.created_at.isoformat(),
            }
        )
    return rows


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
                    source="synthetic",
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
        return payload


@app.post("/api/recompute")
def recompute(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-derive series + metrics from an export-style payload.

    Used to verify an export reproduces every stage metric without touching
    the database.  Body: {"samples": [...], "events": [...], "params": {...}}.

    Sample provenance (``source`` / ``import_package_id``) is passed through
    untouched and echoed back, so an independent recompute can verify that
    every measured point still names the package it came from.  Events are
    returned with full append-only history (superseded rows included).
    """
    try:
        samples = payload["samples"]
        events = payload.get("events", [])
        params = payload.get("params", {})
    except KeyError as exc:
        raise HTTPException(422, f"missing field: {exc}")
    cfg = RoRConfig(
        window_s=float(params.get("ror_window_s", 30.0)),
        display_smooth_s=float(params.get("ror_display_smooth_s", 12.0)),
    )
    series = build_series(
        samples,
        ror_cfg=cfg,
        max_gap_fill_s=float(params.get("max_gap_fill_s", MAX_GAP_FILL_S)),
    )
    sample_origins = [
        {
            "t_s": s.get("t_s"),
            "source": s.get("source", "synthetic"),
            "import_package_id": s.get("import_package_id"),
        }
        for s in samples
    ]
    return {
        "series": series,
        "metrics": phase_metrics(events),
        "current_events": current_events(events),
        "event_history": events,
        "sample_origins": sample_origins,
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "machine_connection": "none (synthetic/offline only)"}


# ---------------------------------------------------------------------------
# offline observation packages — local-file import, never uploaded
# ---------------------------------------------------------------------------

def _decode_identity(raw_bytes: bytes) -> dict[str, Any]:
    """Best-effort fields for the audit row of an unparseable delivery."""
    out: dict[str, Any] = {"package_id": None, "format_version": None,
                           "generated_at": None}
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return out
    if isinstance(data, dict):
        pid = data.get("package_id")
        if isinstance(pid, str):
            out["package_id"] = pid.strip()[:120]
        fv = data.get("format_version")
        if isinstance(fv, int) and not isinstance(fv, bool):
            out["format_version"] = fv
        ga = data.get("generated_at")
        if isinstance(ga, str):
            try:
                out["generated_at"] = datetime.fromisoformat(
                    ga.strip().rstrip("Z") + "+00:00" if ga.strip().endswith("Z") else ga.strip()
                )
            except ValueError:
                out["generated_at"] = None
    return out


def _import_out(imp: ObservationImport, *, preview: dict | None = None) -> dict[str, Any]:
    out = {
        "id": imp.id,
        "package_id": imp.package_id,
        "format_version": imp.format_version,
        "content_sha256": imp.content_sha256,
        "status": imp.status,
        "batch_id": imp.batch_id,
        "batch_name": imp.batch_name,
        "generated_at": imp.generated_at.isoformat() if imp.generated_at else None,
        "summary": json.loads(imp.summary_json or "{}"),
        "resolutions": json.loads(imp.resolutions_json or "{}"),
        "error_code": imp.error_code,
        "error_detail": imp.error_detail,
        "created_at": imp.created_at.isoformat() if imp.created_at else None,
        "applied_at": imp.applied_at.isoformat() if imp.applied_at else None,
    }
    if preview is not None:
        out["preview"] = preview
    return out


def _record_rejected(session: Session, raw_bytes: bytes, err: PackageError) -> ObservationImport:
    """Audit row ONLY.  A whole-package failure never touches samples/events."""
    ident = _decode_identity(raw_bytes)
    imp = ObservationImport(
        package_id=ident["package_id"],
        format_version=ident["format_version"],
        content_sha256=None,
        status="rejected",
        batch_name=None,
        generated_at=ident["generated_at"],
        raw_payload=raw_bytes.decode("utf-8", errors="replace"),
        summary_json="{}",
        conflict_report_json="{}",
        resolutions_json="{}",
        error_code=err.code,
        error_detail=err.detail,
    )
    session.add(imp)
    session.flush()
    return imp


@app.post("/api/imports", status_code=201)
async def import_observation_package(request: Request) -> dict[str, Any]:
    """Deliver one offline observation package (raw bytes of a LOCAL file).

    The browser reads the file via FileReader — nothing is uploaded anywhere;
    this endpoint only receives bytes the operator chose locally.

    Outcomes:
      * 201 ``pending_review`` — validated whole-package, preview attached;
      * 200 ``applied``/``pending_review``/``rejected`` — an exact redelivery
        (same package_id AND content digest) returns the ORIGINAL result with
        ``redelivered: true`` and never adds rows;
      * 409 ``package_id_conflict`` — same stable package id, altered content;
      * 200 ``rejected`` — parse / invalid content (a new audit row, or the
        original row on exact retry); no sample/event/batch row is written.
    """
    raw_bytes = await request.body()
    with Session(engine) as s:
        try:
            pkg = importer.decode_package(raw_bytes)
        except PackageError as err:
            # Exact retry of an already-rejected delivery: same bytes -> the
            # same audit row.  Match on raw payload (package_id may be
            # unextractable when the file does not even parse).
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            prior = s.scalars(
                select(ObservationImport)
                .where(
                    ObservationImport.status == "rejected",
                    ObservationImport.raw_payload == raw_text,
                )
                .order_by(ObservationImport.id)
            ).first()
            if prior is not None:
                return JSONResponse(
                    {
                        **_import_out(prior),
                        "redelivered": True,
                        "message": "完全相同的失败包重试：返回原拒绝记录，无任何写入",
                    },
                    status_code=200,
                )
            rec = _record_rejected(s, raw_bytes, err)
            s.commit()
            s.refresh(rec)
            return JSONResponse(
                {
                    **_import_out(rec),
                    "message": f"整包失败（{err.code}）：{err.detail}；未写入任何样本/事件",
                },
                status_code=200,
            )

        # Exact redelivery: package_id + digest is the idempotency key.
        existing = s.scalar(
            select(ObservationImport).where(
                ObservationImport.package_id == pkg.package_id,
                ObservationImport.content_sha256 == pkg.digest,
            )
        )
        if existing is not None:
            preview = None
            if existing.status == "pending_review":
                resolutions = json.loads(existing.resolutions_json or "{}")
                preview = build_preview(pkg, s, resolutions=resolutions)
            return JSONResponse(
                {
                    **_import_out(existing, preview=preview),
                    "redelivered": True,
                    "message": "完全相同的包重试：返回原导入结果，不新增样本或事件",
                },
                status_code=200,
            )

        # Same stable id, different content = refused redelivery (auditable).
        clash = s.scalar(
            select(ObservationImport).where(
                ObservationImport.package_id == pkg.package_id
            )
        )
        if clash is not None:
            raise HTTPException(
                409,
                f"package_id={pkg.package_id!r} 已存在但内容摘要不同"
                f"（原 sha256={clash.content_sha256}，新 sha256={pkg.digest}）。"
                "稳定包标识不可指向不同内容；如需修订请使用新的 package_id。",
            )

        imp = ObservationImport(
            package_id=pkg.package_id,
            format_version=importer.FORMAT_VERSION,
            content_sha256=pkg.digest,
            status="pending_review",
            batch_name=pkg.batch["name"],
            generated_at=pkg.generated_at,
            raw_payload=raw_bytes.decode("utf-8"),
            summary_json=json.dumps(pkg.summary, ensure_ascii=False),
            conflict_report_json="{}",
            resolutions_json="{}",
        )
        s.add(imp)
        s.flush()
        preview = build_preview(pkg, s)
        imp.conflict_report_json = json.dumps(
            preview["conflicts"], ensure_ascii=False
        )
        s.commit()
        s.refresh(imp)
        return {**_import_out(imp, preview=preview), "redelivered": False}


@app.get("/api/imports")
def list_imports(status: str | None = Query(None)) -> list[dict[str, Any]]:
    with Session(engine) as s:
        q = select(ObservationImport).order_by(ObservationImport.id.desc())
        if status:
            q = q.where(ObservationImport.status == status)
        return [_import_out(r) for r in s.scalars(q)]


def _get_import(session: Session, import_id: int) -> ObservationImport:
    imp = session.get(ObservationImport, import_id)
    if imp is None:
        raise HTTPException(404, f"import {import_id} not found")
    return imp


def _load_valid_package(imp: ObservationImport) -> ValidPackage:
    try:
        return importer.decode_package(imp.raw_payload.encode("utf-8"))
    except PackageError as err:
        raise HTTPException(422, f"账本内包无法重新解析：{err.code}: {err.detail}")


@app.get("/api/imports/{import_id}")
def get_import(import_id: int) -> dict[str, Any]:
    """Preview for one delivery.  The conflict report is rebuilt against the
    CURRENT stored rows every call, so late packages are always reflected."""
    with Session(engine) as s:
        imp = _get_import(s, import_id)
        out = _import_out(imp)
        if imp.status == "pending_review":
            pkg = _load_valid_package(imp)
            resolutions = json.loads(imp.resolutions_json or "{}")
            out["preview"] = build_preview(pkg, s, resolutions=resolutions)
        elif imp.status == "applied":
            out["conflict_report"] = json.loads(imp.conflict_report_json or "{}")
        return out


@app.put("/api/imports/{import_id}/resolutions")
def resolve_import(import_id: int, body: ImportResolutions) -> dict[str, Any]:
    """Save conflict adjudication.  Nothing is applied yet; this only updates
    the ledger and returns the re-projected preview."""
    with Session(engine) as s:
        imp = _get_import(s, import_id)
        if imp.status != "pending_review":
            raise HTTPException(409, f"导入状态为 {imp.status}，不能再裁决")
        pkg = _load_valid_package(imp)
        resolutions = {str(k): str(v) for k, v in body.resolutions.items()}
        report = importer.analyze_conflicts(s, pkg)
        try:
            # Partial adjudication may be saved; applicability is checked at
            # apply time only.
            importer.validate_resolution_values(report, resolutions)
        except PackageError as err:
            raise HTTPException(409, f"{err.code}: {err.detail}")
        imp.resolutions_json = json.dumps(resolutions, ensure_ascii=False)
        imp.conflict_report_json = json.dumps(report.to_dict(), ensure_ascii=False)
        preview = build_preview(pkg, s, resolutions=resolutions)
        s.commit()
        s.refresh(imp)
        return _import_out(imp, preview=preview)


@app.post("/api/imports/{import_id}/apply")
def apply_import_endpoint(import_id: int) -> dict[str, Any]:
    """One-shot atomic application.  Re-validates everything against current
    rows inside a single transaction; any failure rolls ALL of it back."""
    with Session(engine) as s:
        imp = _get_import(s, import_id)
        if imp.status == "applied":
            return {**_import_out(imp), "redelivered": True,
                    "message": "该包已应用：无重复写入"}
        if imp.status != "pending_review":
            raise HTTPException(409, f"导入状态为 {imp.status}，无法应用")
        pkg = _load_valid_package(imp)
        resolutions = json.loads(imp.resolutions_json or "{}")

        # Final guard: conflicts must still be fully adjudicated.
        report = importer.analyze_conflicts(s, pkg)
        try:
            importer.validate_resolutions(report, resolutions)
            result = apply_import(s, imp, pkg)
            s.commit()
        except PackageError as err:
            s.rollback()
            raise HTTPException(409, f"{err.code}: {err.detail}")
        except Exception:
            s.rollback()
            raise
        s.refresh(imp)
        # Refresh preview data from the now-applied state for UI verification.
        applied_preview = build_preview(pkg, s, resolutions=resolutions)
        return {
            **_import_out(imp),
            "redelivered": False,
            "applied_result": result,
            "preview": applied_preview,
        }


@app.post("/api/imports/{import_id}/discard")
def discard_import(import_id: int) -> dict[str, Any]:
    """Dismiss a pending package without applying it (audit row retained)."""
    with Session(engine) as s:
        imp = _get_import(s, import_id)
        if imp.status != "pending_review":
            raise HTTPException(409, f"导入状态为 {imp.status}，不能丢弃")
        imp.status = "discarded"
        s.commit()
        s.refresh(imp)
        return _import_out(imp)
