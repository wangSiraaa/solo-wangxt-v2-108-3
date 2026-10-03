"""Import ledger service: receive (pending), preview, adjudicate, apply.

Guarantees:

* An accepted package first becomes ``pending_review``.  Its samples/events
  are written to **no** batch table until the operator applies it.
* Apply is one transaction.  Any error rolls everything back — a package can
  never leave a half-written curve.
* Same ``package_id`` + same content is idempotent: the original ledger row
  (and, after apply, the original result) is returned.  Same id with changed
  content is rejected (409) instead of silently treated as a retry.
* Every decision (conflict, resolution, failure finding, apply counts) stays
  in the ledger for audit.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analysis import RoRConfig, build_series, phase_metrics
from ..models import Batch, Event, ImportBatch, ImportConflict, Sample
from ..schemas import ObservationPackage
from .package import VALUE_EPS
from .planner import build_projection

# ---------------------------------------------------------------------------
# serialisers
# ---------------------------------------------------------------------------


def _conflict_out(c: ImportConflict) -> dict[str, Any]:
    return {
        "id": c.id,
        "kind": c.kind,
        "t_s": c.t_s,
        "channel": c.channel,
        "existing_value": c.existing_value,
        "incoming_value": c.incoming_value,
        "resolution": c.resolution,
        "resolved_value": c.resolved_value,
        "resolved_by": c.resolved_by,
        "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
        "note": c.note,
    }


def _proposed_batch_meta(pkg: ObservationPackage) -> dict[str, Any]:
    b = pkg.batch
    return {
        "id": None,
        "name": b.name,
        "roaster": b.roaster,
        "bean": b.bean,
        "charge_at": b.charge_at.isoformat(),
        "charge_temp_c": b.charge_temp_c,
        "ambient_temp_c": b.ambient_temp_c,
        "target_drop_temp_c": b.target_drop_temp_c,
        "note": b.note,
    }


def ledger_summary(imp: ImportBatch) -> dict[str, Any]:
    batch_id = imp.created_batch_id or imp.target_batch_id
    return {
        "id": imp.id,
        "package_id": imp.package_id,
        "format_version": imp.format_version,
        "status": imp.status,
        "target_batch_id": imp.target_batch_id,
        "created_batch_id": imp.created_batch_id,
        "batch_id": batch_id,
        "generated_at": imp.generated_at.isoformat() if imp.generated_at else None,
        "received_at": imp.received_at.isoformat() if imp.received_at else None,
        "applied_at": imp.applied_at.isoformat() if imp.applied_at else None,
        "created_by": imp.created_by,
        "n_samples": imp.n_samples,
        "n_events": imp.n_events,
        "error_code": imp.error_code,
        "digest": {
            "algorithm": imp.digest_algorithm,
            "expected": imp.digest_expected,
            "actual": imp.digest_actual,
            "ok": imp.digest_expected is not None
            and imp.digest_expected == imp.digest_actual,
        },
    }


# ---------------------------------------------------------------------------
# failure ledger (atomic: validation failure creates *only* a ledger row)
# ---------------------------------------------------------------------------


def record_failure(
    session: Session,
    *,
    raw_text: str,
    raw: Any,
    error_code: str,
    findings: list[dict[str, Any]],
    actual_digest: str | None = None,
) -> ImportBatch:
    """Persist a rejected delivery for audit.  Touches no batch/sample/event."""
    if isinstance(raw, dict):
        pid = raw.get("package_id")
        version = raw.get("format_version")
        gen = raw.get("generated_at")
    else:
        pid = version = gen = None
    if not isinstance(pid, str) or not pid:
        pid = "malformed:" + hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]
    if not isinstance(version, int) or isinstance(version, bool):
        version = 0
    generated_at = _parse_dt(gen)

    existing = session.scalar(select(ImportBatch).where(ImportBatch.package_id == pid))
    if existing is not None:
        # Re-delivery of the same bad bytes: return the same auditable answer.
        if actual_digest is None or existing.digest_actual == actual_digest:
            return existing
        raise HTTPException(
            409,
            f"package_id {pid!r} 已被不同内容的投递占用",
        )

    imp = ImportBatch(
        package_id=pid,
        format_version=version,
        status="failed",
        payload_json=raw_text,
        digest_actual=actual_digest,
        generated_at=generated_at,
        error_code=error_code,
        error_json=json.dumps({"code": error_code, "findings": findings}, ensure_ascii=False),
    )
    session.add(imp)
    session.commit()
    session.refresh(imp)
    return imp


def _parse_dt(v: Any) -> datetime | None:
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(v, datetime):
        return v
    return None


# ---------------------------------------------------------------------------
# receive
# ---------------------------------------------------------------------------


def _detect_conflicts(
    existing_samples: list[Sample], pkg: ObservationPackage
) -> list[ImportConflict]:
    by_t = {float(s.t_s): s for s in existing_samples}
    rows: list[ImportConflict] = []
    for sp in pkg.samples:
        cur = by_t.get(float(sp.t_s))
        if cur is None:
            continue
        for channel, key, incoming in (
            ("bean", "bean_temp_c", sp.bean_temp_c),
            ("env", "env_temp_c", sp.env_temp_c),
        ):
            if incoming is None:
                continue
            old = getattr(cur, key)
            if old is None:
                kind = "missing_fill"
            elif abs(float(old) - float(incoming)) > VALUE_EPS:
                kind = "value_mismatch"
            else:
                continue  # identical re-delivery
            rows.append(
                ImportConflict(
                    kind=kind,
                    t_s=float(sp.t_s),
                    channel=channel,
                    existing_value=old,
                    incoming_value=float(incoming),
                    note=(
                        "已存储读数为空，包内补来实测值，需裁决"
                        if kind == "missing_fill"
                        else "同一时刻存在不同实测值，需裁决"
                    ),
                )
            )
    return rows


def receive_package(
    session: Session,
    *,
    raw: dict[str, Any],
    raw_text: str,
    pkg: ObservationPackage,
    target_batch_id: int | None,
    created_by: str,
    actual_digest: str,
) -> ImportBatch:
    """Idempotent ledger insert.  Returns the (possibly existing) ledger row."""
    pid = pkg.package_id
    existing = session.scalar(select(ImportBatch).where(ImportBatch.package_id == pid))
    if existing is not None:
        if existing.digest_actual == actual_digest:
            return existing  # exact re-delivery: same answer, no new rows
        raise HTTPException(
            409,
            f"package_id {pid!r} 已存在，但内容摘要不同；拒绝作为重复投递",
        )

    if target_batch_id is not None:
        target = session.get(Batch, target_batch_id)
        if target is None:
            raise HTTPException(404, f"batch {target_batch_id} not found")
        existing_samples = list(target.samples)
    else:
        existing_samples = []

    imp = ImportBatch(
        package_id=pid,
        format_version=pkg.format_version,
        status="pending_review",
        target_batch_id=target_batch_id,
        payload_json=raw_text,
        digest_expected=pkg.digest.sha256.lower(),
        digest_actual=actual_digest,
        generated_at=pkg.generated_at.replace(tzinfo=None),
        created_by=created_by,
        n_samples=len(pkg.samples),
        n_events=len(pkg.events),
    )
    imp.conflicts = _detect_conflicts(existing_samples, pkg)
    session.add(imp)
    session.commit()
    session.refresh(imp)
    return imp


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


def _load_package(imp: ImportBatch) -> ObservationPackage:
    from .package import validate_package

    # Stored verbatim; re-validating means a preview can never be built from a
    # package whose signature no longer verifies.
    return validate_package(json.loads(imp.payload_json))


def preview_import(
    session: Session,
    imp: ImportBatch,
    *,
    ror_cfg: RoRConfig,
    max_gap_fill_s: float,
) -> dict[str, Any]:
    if imp.status in ("failed", "aborted"):
        out = ledger_summary(imp)
        out["batch"] = None
        out["conflicts"] = []
        out["resolutions"] = {}
        out["projection"] = None
        out["current"] = None
        out["findings"] = (
            json.loads(imp.error_json).get("findings", []) if imp.error_json else []
        )
        return out
    pkg = _load_package(imp)

    if imp.status == "applied" and (imp.created_batch_id or imp.target_batch_id):
        batch_id = imp.created_batch_id or imp.target_batch_id
        batch = session.get(Batch, batch_id)
        projection = _projection_for_batch(session, batch, ror_cfg, max_gap_fill_s)
    else:
        target = (
            session.get(Batch, imp.target_batch_id)
            if imp.target_batch_id is not None
            else None
        )
        resolutions = {
            c.id: c.resolution for c in imp.conflicts if c.resolution is not None
        }
        projection = build_projection(
            list(target.samples) if target else [],
            list(target.events) if target else [],
            pkg,
            list(imp.conflicts),
            resolutions,
            ror_cfg=ror_cfg,
            max_gap_fill_s=max_gap_fill_s,
        )

    # Current analysis *before* apply: undecided packages must not have moved
    # it, and the UI shows both for comparison.
    current = None
    if imp.target_batch_id is not None:
        target = session.get(Batch, imp.target_batch_id)
        current = {
            "metrics": phase_metrics(
                [_event_dict(e) for e in target.events]
            ),
            "n_samples": len(target.samples),
        }

    out = ledger_summary(imp)
    out["batch"] = (
        {"id": target.id, "name": target.name}
        if imp.target_batch_id is not None and target is not None
        else _proposed_batch_meta(pkg)
    )
    out["conflicts"] = [_conflict_out(c) for c in imp.conflicts]
    out["resolutions"] = {
        c.id: c.resolution for c in imp.conflicts if c.resolution is not None
    }
    out["projection"] = projection
    out["current"] = current
    out["params"] = {
        "ror_window_s": ror_cfg.window_s,
        "ror_display_smooth_s": ror_cfg.display_smooth_s,
        "max_gap_fill_s": max_gap_fill_s,
    }
    return out


def _event_dict(e: Event) -> dict[str, Any]:
    return {
        "id": e.id,
        "event_type": e.event_type,
        "t_s": e.t_s,
        "label": e.label,
        "source": e.source,
        "created_by": e.created_by,
        "value_num": e.value_num,
        "note": e.note,
        "superseded": e.superseded,
        "superseded_by_id": e.superseded_by_id,
        "event_uid": e.event_uid,
        "source_package_id": e.source_package_id,
    }


def _projection_for_batch(
    session: Session, batch: Batch, ror_cfg: RoRConfig, max_gap_fill_s: float
) -> dict[str, Any]:
    samples = [
        {
            "t_s": s.t_s,
            "bean_temp_c": s.bean_temp_c,
            "env_temp_c": s.env_temp_c,
            "source": s.source,
            "source_package_id": s.source_package_id,
        }
        for s in batch.samples
    ]
    events = [_event_dict(e) for e in batch.events]
    series = build_series(samples, ror_cfg=ror_cfg, max_gap_fill_s=max_gap_fill_s)
    return {
        "series": series,
        "events": events,
        "metrics": phase_metrics(events),
    }


# ---------------------------------------------------------------------------
# conflict adjudication
# ---------------------------------------------------------------------------


def resolve_conflicts(
    session: Session,
    imp: ImportBatch,
    *,
    resolutions: dict[int, str],
    resolved_by: str,
) -> ImportBatch:
    if imp.status != "pending_review":
        raise HTTPException(409, f"导入 {imp.package_id} 状态为 {imp.status}，不可裁决")
    by_id = {c.id: c for c in imp.conflicts}
    unknown = sorted(set(resolutions) - set(by_id))
    if unknown:
        raise HTTPException(404, f"冲突不存在: {unknown}")
    now = datetime.utcnow()
    for cid, choice in resolutions.items():
        c = by_id[cid]
        c.resolution = choice
        c.resolved_value = (
            c.incoming_value if choice == "use_incoming" else c.existing_value
        )
        c.resolved_by = resolved_by
        c.resolved_at = now
    session.commit()
    session.refresh(imp)
    return imp


# ---------------------------------------------------------------------------
# apply (single transaction) / abort
# ---------------------------------------------------------------------------


def apply_import(session: Session, imp: ImportBatch, *, applied_by: str) -> dict[str, Any]:
    if imp.status == "applied":
        # idempotent: re-apply returns the exact original result
        return json.loads(imp.result_json)
    if imp.status in ("failed", "aborted"):
        raise HTTPException(409, f"导入 {imp.package_id} 状态为 {imp.status}，不可应用")

    pkg = _load_package(imp)
    conflicts = list(imp.conflicts)
    pending = [c for c in conflicts if c.resolution is None]
    if pending:
        raise HTTPException(
            409,
            f"仍有 {len(pending)} 个冲突未裁决，导入保持待核验",
        )

    try:
        result = _apply_in_transaction(
            session, imp, pkg, conflicts, applied_by=applied_by
        )
    except HTTPException:
        session.rollback()
        raise
    except Exception as exc:  # never leave a half-written curve
        session.rollback()
        raise HTTPException(500, f"应用失败，已整体回滚: {exc}")
    return result


def _apply_in_transaction(
    session: Session,
    imp: ImportBatch,
    pkg: ObservationPackage,
    conflicts: list[ImportConflict],
    *,
    applied_by: str,
) -> dict[str, Any]:
    # --- resolve the target batch (create when merging into nothing) --------
    if imp.target_batch_id is not None:
        batch = session.get(Batch, imp.target_batch_id)
        if batch is None:
            raise HTTPException(404, "目标批次已不存在")
        pending_new_batch = False
    else:
        b = pkg.batch
        clash = session.scalar(select(Batch).where(Batch.name == b.name))
        if clash is not None:
            raise HTTPException(409, f"批次名 {b.name!r} 已存在，请改投到该批次")
        batch = Batch(
            name=b.name,
            roaster=b.roaster,
            bean=b.bean,
            charge_at=b.charge_at.replace(tzinfo=None),
            charge_temp_c=b.charge_temp_c,
            ambient_temp_c=b.ambient_temp_c,
            target_drop_temp_c=b.target_drop_temp_c,
            note=b.note or "来自离线现场观察包导入",
        )
        pending_new_batch = True

    # --- drift check BEFORE anything is written: conflicts that appeared
    # since the preview (a later delivery / another correction).  A new batch
    # has no stored rows, so this is necessarily empty for it. ----------------
    decision = {(float(c.t_s), c.channel): c.resolution for c in conflicts}
    by_t = {float(s.t_s): s for s in batch.samples}
    drift: list[ImportConflict] = []
    for sp in pkg.samples:
        cur = by_t.get(float(sp.t_s))
        if cur is None:
            continue
        for channel, key, incoming in (
            ("bean", "bean_temp_c", sp.bean_temp_c),
            ("env", "env_temp_c", sp.env_temp_c),
        ):
            if incoming is None:
                continue
            old = getattr(cur, key)
            if old is None:
                kind = "missing_fill"
            elif abs(float(old) - float(incoming)) > VALUE_EPS:
                kind = "value_mismatch"
            else:
                continue
            if (float(sp.t_s), channel) not in decision:
                drift.append(
                    ImportConflict(
                        import_batch_id=imp.id,
                        kind=kind,
                        t_s=float(sp.t_s),
                        channel=channel,
                        existing_value=old,
                        incoming_value=float(incoming),
                        note="应用前检测到的新增冲突（其他导入或修正改变了数据）",
                    )
                )
    if drift:
        # Nothing has been mutated yet; record the new conflicts and keep the
        # package pending.  The (possibly created) batch is rolled back.
        session.rollback()
        imp = session.get(ImportBatch, imp.id)
        for d in drift:
            session.add(d)
        session.commit()
        raise HTTPException(409, f"应用前出现 {len(drift)} 个新冲突，仍待裁决")

    if pending_new_batch:
        session.add(batch)
        session.flush()  # get batch.id; still inside the transaction

    # --- samples ------------------------------------------------------------
    sample_stats = {
        "added": 0,
        "updated": 0,
        "identical_skipped": 0,
        "missing_kept": 0,
        "existing_kept": 0,
        "dropout_noop": 0,
    }
    for sp in pkg.samples:
        t = float(sp.t_s)
        cur = by_t.get(t)
        if cur is None:
            session.add(
                Sample(
                    batch_id=batch.id,
                    t_s=t,
                    sampled_at=sp.sampled_at.replace(tzinfo=None) if sp.sampled_at else None,
                    bean_temp_c=sp.bean_temp_c,
                    env_temp_c=sp.env_temp_c,
                    source="imported",
                    source_package_id=pkg.package_id,
                )
            )
            sample_stats["added"] += 1
            continue

        for channel, key, incoming in (
            ("bean", "bean_temp_c", sp.bean_temp_c),
            ("env", "env_temp_c", sp.env_temp_c),
        ):
            if incoming is None:
                # A recorded dropout never erases an already-stored value.
                sample_stats["dropout_noop"] += 1
                continue
            old = getattr(cur, key)
            if old is not None and abs(float(old) - float(incoming)) <= VALUE_EPS:
                sample_stats["identical_skipped"] += 1
                continue
            choice = decision.get((t, channel))
            if choice == "use_incoming":
                setattr(cur, key, float(incoming))
                cur.source = "imported"
                cur.source_package_id = pkg.package_id
                sample_stats["updated"] += 1
            elif old is None:
                sample_stats["missing_kept"] += 1
            else:
                sample_stats["existing_kept"] += 1

    # --- events (append-only; uids dedupe; same supersede rule as the API) --
    known_uids = {
        e.event_uid
        for e in session.scalars(
            select(Event).where(Event.batch_id == batch.id)
        ).all()
        if e.event_uid is not None
    }
    event_stats = {"added": 0, "duplicates": 0, "superseded": 0}
    for e in pkg.events:
        if e.event_uid in known_uids:
            event_stats["duplicates"] += 1
            continue
        row = Event(
            batch_id=batch.id,
            event_type=e.event_type,
            t_s=float(e.t_s),
            label=e.label,
            source=e.source,
            created_by=e.created_by,
            value_num=e.value_num,
            note=e.note,
            event_uid=e.event_uid,
            source_package_id=pkg.package_id,
        )
        session.add(row)
        session.flush()
        if e.event_type != "damper_change":
            prev = session.scalars(
                select(Event).where(
                    Event.batch_id == batch.id,
                    Event.event_type == row.event_type,
                    Event.superseded.is_(False),
                    Event.id != row.id,
                )
            ).all()
            for p in prev:
                p.superseded = True
                p.superseded_by_id = row.id
                event_stats["superseded"] += 1
        known_uids.add(e.event_uid)
        event_stats["added"] += 1

    # --- ledger bookkeeping + commit (all of the above, atomically) ---------
    imp.status = "applied"
    imp.applied_at = datetime.utcnow()
    if imp.target_batch_id is None:
        imp.created_batch_id = batch.id
    imp.created_by = applied_by or imp.created_by
    result = {
        "import_id": imp.id,
        "package_id": pkg.package_id,
        "status": "applied",
        "batch_id": batch.id,
        "batch_name": batch.name,
        "applied_at": imp.applied_at.isoformat(),
        "samples": sample_stats,
        "events": event_stats,
        "conflicts": {
            "total": len(conflicts),
            "use_incoming": sum(1 for c in conflicts if c.resolution == "use_incoming"),
            "keep_existing": sum(1 for c in conflicts if c.resolution == "keep_existing"),
        },
    }
    imp.result_json = json.dumps(result, ensure_ascii=False)
    session.commit()
    return result


def abort_import(session: Session, imp: ImportBatch) -> dict[str, Any]:
    if imp.status == "aborted":
        return ledger_summary(imp)
    if imp.status != "pending_review":
        raise HTTPException(409, f"导入 {imp.package_id} 状态为 {imp.status}，不可放弃")
    imp.status = "aborted"
    imp.error_json = json.dumps(
        {"code": "aborted_by_operator", "findings": []}, ensure_ascii=False
    )
    session.commit()
    session.refresh(imp)
    return ledger_summary(imp)
