"""Pure merge planning shared by preview and apply.

The same planning function drives the *preview* (nothing written) and the
*apply* transaction (written), so the numbers the operator reviews are the
numbers that land.  Inputs are plain dicts / validated package objects; the
only derived-data call is the existing numpy analysis pipeline.

Conflict model (measured readings at the same second):

* package has no point at ``t``                -> new sample
* equal readings (within VALUE_EPS)            -> duplicate, nothing to do
* existing NULL, package measured              -> ``missing_fill`` conflict
* two different finite readings                -> ``value_mismatch`` conflict

Until every conflict is decided the projection keeps the *existing* reading,
so an undecided late package cannot move the current analysis.
"""
from __future__ import annotations

from typing import Any

from ..analysis import RoRConfig, build_series, phase_metrics
from ..schemas import ObservationPackage
from .package import VALUE_EPS

_DAMPER = "damper_change"


def _existing_sample_dict(s: Any) -> dict[str, Any]:
    return {
        "t_s": s.t_s,
        "bean_temp_c": s.bean_temp_c,
        "env_temp_c": s.env_temp_c,
        "source": s.source,
        "source_package_id": s.source_package_id,
    }


def _existing_event_dict(e: Any) -> dict[str, Any]:
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


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= VALUE_EPS


def plan_samples(
    existing_samples: list[Any],
    pkg: ObservationPackage,
    conflict_rows: list[Any],
    resolutions: dict[int, str],
) -> dict[str, Any]:
    """Merge package samples onto the stored ones for projection.

    ``resolutions`` maps ``import_conflicts.id`` -> ``keep_existing``/
    ``use_incoming``.  Undecided conflicts keep the stored reading.
    """
    by_t: dict[float, dict[str, Any]] = {}
    for s in existing_samples:
        by_t[float(s.t_s)] = _existing_sample_dict(s)

    decision: dict[tuple[float, str], str] = {}
    for c in conflict_rows:
        choice = resolutions.get(c.id)
        if choice is not None:
            decision[(float(c.t_s), c.channel)] = choice

    counts = {
        "added": 0,
        "updated": 0,
        "identical_skipped": 0,
        "missing_kept": 0,
        "existing_kept": 0,
        "dropout_noop": 0,
        "conflicted": 0,
    }
    actions: list[dict[str, Any]] = []
    for sp in pkg.samples:
        t = float(sp.t_s)
        row = by_t.get(t)
        if row is None:
            by_t[t] = {
                "t_s": t,
                "bean_temp_c": sp.bean_temp_c,
                "env_temp_c": sp.env_temp_c,
                "source": "imported",
                "source_package_id": pkg.package_id,
            }
            counts["added"] += 1
            actions.append({"t_s": t, "action": "new"})
            continue

        per_channel: dict[str, str] = {}
        for channel, key in (("bean", "bean_temp_c"), ("env", "env_temp_c")):
            incoming = getattr(sp, key)
            existing_val = row[key]
            if incoming is None:
                # A dropout never erases an already-stored measurement.
                per_channel[channel] = "noop"
                counts["dropout_noop"] += 1
                continue
            if existing_val is not None and _same(existing_val, incoming):
                per_channel[channel] = "duplicate"
                counts["identical_skipped"] += 1
                continue

            # existing NULL with incoming measurement, or two different
            # finite readings: a human decision is required.
            counts["conflicted"] += 1
            choice = decision.get((t, channel))
            if choice == "use_incoming":
                row[key] = incoming
                row["source"] = "imported"
                row["source_package_id"] = pkg.package_id
                per_channel[channel] = "update"
                counts["updated"] += 1
            elif existing_val is None:
                per_channel[channel] = "missing_kept"
                counts["missing_kept"] += 1
            elif choice == "keep_existing":
                per_channel[channel] = "existing_kept"
                counts["existing_kept"] += 1
            else:
                per_channel[channel] = "pending"
        actions.append(
            {
                "t_s": t,
                "action": "merge",
                "channels": per_channel,
                "pending": any(v == "pending" for v in per_channel.values()),
            }
        )

    merged = sorted(by_t.values(), key=lambda r: r["t_s"])
    return {"samples": merged, "counts": counts, "actions": actions}


def plan_events(
    existing_events: list[Any],
    pkg: ObservationPackage,
) -> dict[str, Any]:
    """Append package events using the same append-only/supersede semantics as
    the manual correction API.  Events already present (matched by their
    stable ``event_uid``) are skipped — that is the repeated-delivery rule.
    """
    rows: list[dict[str, Any]] = [_existing_event_dict(e) for e in existing_events]
    existing_uids = {
        e.event_uid for e in existing_events if e.event_uid is not None
    }

    counts = {"added": 0, "duplicate": 0, "supersedes": 0}
    actions: list[dict[str, Any]] = []
    # Package events are validated non-decreasing, so insertion order is
    # exactly "appended after everything already stored".
    for i, e in enumerate(pkg.events):
        if e.event_uid in existing_uids:
            counts["duplicate"] += 1
            actions.append(
                {"event_uid": e.event_uid, "t_s": e.t_s, "action": "duplicate"}
            )
            continue

        fake_id = -(i + 1)
        new_row = {
            "id": fake_id,
            "event_type": e.event_type,
            "t_s": float(e.t_s),
            "label": e.label,
            # The stored source stays faithful to the recorder.
            "source": e.source,
            "created_by": e.created_by,
            "value_num": e.value_num,
            "note": e.note,
            "superseded": False,
            "superseded_by_id": None,
            "event_uid": e.event_uid,
            "source_package_id": pkg.package_id,
        }
        superseded_ids: list[int] = []
        if e.event_type != _DAMPER:
            for r in rows:
                if (
                    r["event_type"] == e.event_type
                    and not r["superseded"]
                    and r["id"] != fake_id
                ):
                    r["superseded"] = True
                    r["superseded_by_id"] = fake_id
                    superseded_ids.append(r["id"])
        rows.append(new_row)
        counts["added"] += 1
        counts["supersedes"] += len(superseded_ids)
        actions.append(
            {
                "event_uid": e.event_uid,
                "t_s": e.t_s,
                "action": "add",
                "supersedes_ids": superseded_ids,
            }
        )

    return {"events": rows, "counts": counts, "actions": actions}


def build_projection(
    existing_samples: list[Any],
    existing_events: list[Any],
    pkg: ObservationPackage,
    conflict_rows: list[Any],
    resolutions: dict[int, str],
    *,
    ror_cfg: RoRConfig,
    max_gap_fill_s: float,
) -> dict[str, Any]:
    """Assemble the projected series + event history + phase metrics."""
    sp = plan_samples(existing_samples, pkg, conflict_rows, resolutions)
    ev = plan_events(existing_events, pkg)
    series = build_series(
        sp["samples"], ror_cfg=ror_cfg, max_gap_fill_s=max_gap_fill_s
    )
    metrics = phase_metrics(ev["events"])
    return {
        "series": series,
        "events": ev["events"],
        "metrics": metrics,
        "sample_actions": sp["actions"],
        "event_actions": ev["actions"],
        "sample_counts": sp["counts"],
        "event_counts": ev["counts"],
    }
