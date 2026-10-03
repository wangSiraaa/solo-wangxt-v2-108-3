"""Tests for offline observation-package import (acceptance criteria ①–⑤).

Covers: whole-package validation and atomic failure, content-digest
idempotency, same-timestamp conflicts that stay pending until adjudicated,
preview==apply equivalence, supersede history for samples and events, and
export -> independent recompute preserving provenance, event history and
phase metrics after a refresh (fresh API client / DB session).
"""
from __future__ import annotations

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app import importer  # noqa: E402


# ---------------------------------------------------------------------------
# package builders
# ---------------------------------------------------------------------------

def _summarize(pkg: dict) -> dict:
    """Compute the v1 summary exactly as an offline field logger would.

    Time extent is by extrema, not array position, because a field package may
    serialise samples/events in arrival/record order."""
    samples, events = pkg["samples"], pkg["events"]
    digest = importer.compute_digest(pkg)
    return {
        "n_samples": len(samples),
        "n_events": len(events),
        "t_first_s": min(s["t_s"] for s in samples),
        "t_last_s": max(s["t_s"] for s in samples),
        "n_missing_bean": sum(1 for s in samples if s["bean_temp_c"] is None),
        "n_missing_env": sum(1 for s in samples if s["env_temp_c"] is None),
        "sha256": digest,
    }


def make_package(
    package_id: str,
    samples: list[dict],
    events: list[dict],
    *,
    batch_name: str = "FIELD-OBS-001",
    bean: str = "Panama Geisha",
) -> dict:
    # Deliberately deliver samples/events in a non-sorted order to prove that
    # array arrival order is irrelevant.
    pkg = {
        "format_version": 1,
        "package_id": package_id,
        "generated_at": "2026-09-28T10:15:00",
        "batch": {
            "name": batch_name,
            "roaster": "field-logger-1 (offline)",
            "bean": bean,
            "charge_at": "2026-09-28T09:00:00",
            "charge_temp_c": 180.0,
            "ambient_temp_c": 23.0,
            "target_drop_temp_c": 205.0,
            "note": "现场离线记录；文件本地导入，不上传",
        },
        "samples": samples,
        "events": events,
        "summary": {},
    }
    pkg["summary"] = _summarize(pkg)
    return pkg


def field_samples() -> list[dict]:
    # uneven intervals, one bean dropout, a wide environmental gap
    return [
        {"t_s": 600.0, "bean_temp_c": 206.0, "env_temp_c": 226.0},
        {"t_s": 0.0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
        {"t_s": 4.0, "bean_temp_c": 142.5, "env_temp_c": 191.2},
        {"t_s": 9.5, "bean_temp_c": None, "env_temp_c": 192.0},
        {"t_s": 13.0, "bean_temp_c": 101.0, "env_temp_c": None},
        {"t_s": 62.0, "bean_temp_c": 112.0, "env_temp_c": 199.0},
        {"t_s": 480.0, "bean_temp_c": 195.0, "env_temp_c": 220.0},
        {"t_s": 599.0, "bean_temp_c": 205.2, "env_temp_c": 225.5},
    ]


def field_events() -> list[dict]:
    return [
        {"event_type": "damper_change", "t_s": 300.0, "value_num": 40.0,
         "source": "manual", "created_by": "field-op",
         "label": "风门 -> 40%"},
        {"event_type": "charge", "t_s": 0.0, "source": "manual",
         "created_by": "field-op"},
        {"event_type": "turning_point", "t_s": 58.0, "source": "manual",
         "created_by": "field-op", "label": "现场回温点"},
        {"event_type": "first_crack_start", "t_s": 480.0, "source": "manual"},
        {"event_type": "first_crack_end", "t_s": 540.0, "source": "manual"},
        {"event_type": "drop", "t_s": 600.0, "source": "manual"},
    ]


def deliver(client, pkg: dict):
    return client.post("/api/imports", content=json.dumps(pkg).encode("utf-8"))


def apply(client, import_id: int, resolutions=None):
    if resolutions:
        r = client.put(
            f"/api/imports/{import_id}/resolutions",
            json={"resolutions": resolutions},
        )
        assert r.status_code == 200, r.text
    return client.post(f"/api/imports/{import_id}/apply")


# ---------------------------------------------------------------------------
# ① valid package: preview == apply, provenance visible in curve and export
# ---------------------------------------------------------------------------

def test_import_valid_package_preview_matches_apply(client):
    pkg = make_package("obs-happy-1", field_samples(), field_events())
    r = deliver(client, pkg)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending_review"
    assert body["redelivered"] is False

    preview = body["preview"]
    assert preview["conflicts"]["n_conflicts"] == 0
    assert preview["conflicts"]["sample_additions"] == 8
    # a fresh batch: nothing exists yet
    assert preview["conflicts"]["batch_exists"] is False
    # uneven sampling survives into the projected curve
    pts = preview["projected"]["series"]["raw_points"]
    assert [p["t_s"] for p in pts] == sorted(p["t_s"] for p in pts)
    gaps = {round(pts[i + 1]["t_s"] - pts[i]["t_s"], 3) for i in range(len(pts) - 1)}
    assert len(gaps) > 3
    # missing reading preserved as missing, source labelled on every point
    assert any(p["bean_temp_c"] is None for p in pts)
    assert {p["source"] for p in pts} == {"observation_package"}
    assert {p["import_package_id"] for p in pts} == {"obs-happy-1"}
    # manual events drive the metrics
    assert preview["projected"]["metrics"]["development_s"] == 120.0
    assert preview["projected"]["metrics"]["development_ratio"] == 0.2

    ap = apply(client, body["id"]).json()
    assert ap["status"] == "applied"
    assert ap["applied_result"]["samples_added"] == 8
    assert ap["applied_result"]["events_added"] == 6

    bid = ap["batch_id"]
    stored = client.get(f"/api/batches/{bid}/series").json()
    # Preview and applied state describe the SAME curve and metrics.
    assert [
        (p["t_s"], p["bean_temp_c"], p["env_temp_c"], p["import_package_id"])
        for p in stored["series"]["raw_points"]
    ] == [
        (p["t_s"], p["bean_temp_c"], p["env_temp_c"], p["import_package_id"])
        for p in pts
    ]
    assert stored["metrics"] == preview["projected"]["metrics"]

    evs = client.get(f"/api/batches/{bid}/events").json()
    assert len(evs) == 6
    assert all(e["import_package_id"] == "obs-happy-1" for e in evs)
    dampers = [e for e in evs if e["event_type"] == "damper_change"]
    assert dampers and dampers[0]["value_num"] == 40.0


def test_export_names_source_package_and_recompute_preserves_it(client):
    pkg = make_package("obs-export-1", field_samples(), field_events())
    iid = deliver(client, pkg).json()["id"]
    bid = apply(client, iid).json()["batch_id"]

    ex = client.get(f"/api/batches/{bid}/export?window_s=30&display_smooth_s=12").json()
    assert ex["batch"]["name"] == "FIELD-OBS-001"
    # every exported raw point carries provenance
    origins = {
        (p["t_s"], p["source"], p["import_package_id"])
        for p in ex["series"]["raw_points"]
    }
    assert all(o[1] == "observation_package" for o in origins)
    assert all(o[2] == "obs-export-1" for o in origins)
    # full append-only event history is in the export, all sourced
    assert all("import_package_id" in e for e in ex["events"])
    assert all(e["import_package_id"] == "obs-export-1" for e in ex["events"])

    rc = client.post(
        "/api/recompute",
        json={
            "samples": [
                {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"],
                 "env_temp_c": p["env_temp_c"], "source": p["source"],
                 "import_package_id": p["import_package_id"]}
                for p in ex["series"]["raw_points"]
            ],
            "events": ex["events"],
            "params": ex["params"],
        },
    ).json()
    # ⑤ metrics identical under independent recompute
    for key in ("drying_s", "maillard_s", "development_s",
                "first_crack_window_s", "total_s", "development_ratio"):
        assert rc["metrics"][key] == ex["metrics"][key], key
    # provenance echoed by the independent recompute
    recomputed_pkgs = {o["import_package_id"] for o in rc["sample_origins"]}
    assert recomputed_pkgs == {"obs-export-1"}
    # event history preserved incl. required keys
    assert len(rc["event_history"]) == len(ex["events"])


def test_refresh_then_reexport_and_recompute_is_stable(client):
    """⑤ simulated refresh: re-fetch everything through new requests and
    confirm the same history, sources and metrics come back."""
    pkg = make_package("obs-refresh-1", field_samples(), field_events())
    iid = deliver(client, pkg).json()["id"]
    bid = apply(client, iid).json()["batch_id"]

    ex1 = client.get(f"/api/batches/{bid}/export").json()
    ex2 = client.get(f"/api/batches/{bid}/export").json()  # after "refresh"
    assert ex1["metrics"] == ex2["metrics"]
    assert [
        (e["id"], e["event_type"], e["t_s"], e["superseded"], e["import_package_id"])
        for e in ex1["events"]
    ] == [
        (e["id"], e["event_type"], e["t_s"], e["superseded"], e["import_package_id"])
        for e in ex2["events"]
    ]
    rc = client.post(
        "/api/recompute",
        json={
            "samples": [
                {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"],
                 "env_temp_c": p["env_temp_c"], "source": p["source"],
                 "import_package_id": p["import_package_id"]}
                for p in ex2["series"]["raw_points"]
            ],
            "events": ex2["events"],
            "params": ex2["params"],
        },
    ).json()
    assert rc["metrics"] == ex2["metrics"]
    assert len(rc["event_history"]) == 6


# ---------------------------------------------------------------------------
# ② exact redelivery is idempotent and returns the original result
# ---------------------------------------------------------------------------

def test_identical_package_retry_returns_original_no_new_rows(client):
    pkg = make_package("obs-retry-1", field_samples(), field_events())
    first = deliver(client, pkg).json()
    assert first["status"] == "pending_review"
    iid = first["id"]
    apply(client, iid)

    retry = deliver(client, pkg).json()
    assert retry["redelivered"] is True
    assert retry["id"] == iid
    assert retry["status"] == "applied"
    bid = retry["batch_id"]

    stored = client.get(f"/api/batches/{bid}/series").json()
    assert len(stored["series"]["raw_points"]) == 8
    assert len(client.get(f"/api/batches/{bid}/events").json()) == 6
    ledger = [r for r in client.get("/api/imports").json()
              if r["package_id"] == "obs-retry-1"]
    assert len(ledger) == 1


def test_retry_while_pending_returns_pending_preview(client):
    pkg = make_package("obs-retry-pending", field_samples(), field_events())
    first = deliver(client, pkg).json()
    again = deliver(client, pkg).json()
    assert again["redelivered"] is True
    assert again["id"] == first["id"]
    assert again["status"] == "pending_review"
    assert "preview" in again


def test_same_package_id_different_content_is_refused(client):
    pkg = make_package("obs-clash", field_samples(), field_events())
    assert deliver(client, pkg).status_code == 201
    altered = json.loads(json.dumps(pkg))
    altered["samples"][-1]["bean_temp_c"] = 999.0 if False else 201.0
    altered["summary"] = _summarize(altered)
    r = deliver(client, altered)
    assert r.status_code == 409
    # the refused delivery did not create an applied/pending second row
    rows = [x for x in client.get("/api/imports").json()
            if x["package_id"] == "obs-clash"]
    assert len(rows) == 1 and rows[0]["status"] == "pending_review"


# ---------------------------------------------------------------------------
# ③ same timestamp, disagreeing readings -> stays pending; analysis frozen
# ---------------------------------------------------------------------------

def test_conflicting_late_package_holds_until_adjudicated(client):
    base = make_package(
        "obs-base",
        [
            {"t_s": 0.0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
            {"t_s": 60.0, "bean_temp_c": 110.0, "env_temp_c": 198.0},
            {"t_s": 600.0, "bean_temp_c": 206.0, "env_temp_c": 225.0},
        ],
        [
            {"event_type": "charge", "t_s": 0.0, "source": "manual"},
            {"event_type": "drop", "t_s": 600.0, "source": "manual"},
        ],
    )
    b0 = deliver(client, base).json()
    applied = apply(client, b0["id"]).json()
    bid = applied["batch_id"]

    late = make_package(
        "obs-late",
        [
            # out-of-order arrival: a previously unseen early point ...
            {"t_s": 30.0, "bean_temp_c": 104.0, "env_temp_c": 195.0},
            # ... and a DISAGREEING reading at an existing timestamp
            {"t_s": 60.0, "bean_temp_c": 112.5, "env_temp_c": 198.0},
        ],
        [
            {"event_type": "turning_point", "t_s": 55.0, "source": "manual"},
            {"event_type": "drop", "t_s": 610.0, "source": "manual"},
        ],
        batch_name="FIELD-OBS-001",
    )
    body = deliver(client, late).json()
    assert body["status"] == "pending_review"
    cf = body["preview"]["conflicts"]
    assert {c["ref"] for c in cf["sample_conflicts"]} == {"sample@60.0"}
    assert {c["ref"] for c in cf["event_conflicts"]} == {"drop@610.0"}
    assert body["preview"]["all_conflicts_resolved"] is False
    late_id = body["id"]

    # Current analysis MUST be unchanged before adjudication.
    cur = client.get(f"/api/batches/{bid}/series").json()
    assert len(cur["series"]["raw_points"]) == 3  # no new point at 30s
    at60 = [p for p in cur["series"]["raw_points"] if p["t_s"] == 60.0][0]
    assert at60["bean_temp_c"] == 110.0
    assert at60["import_package_id"] == "obs-base"
    assert cur["metrics"]["total_s"] == 600.0

    # Applying with unresolved conflicts is refused.
    refused = client.post(f"/api/imports/{late_id}/apply")
    assert refused.status_code == 409

    # An unknown resolution ref is rejected too.
    bad_res = client.put(
        f"/api/imports/{late_id}/resolutions",
        json={"resolutions": {"sample@60.0": "nope"}},
    )
    assert bad_res.status_code in (409, 422)

    # Partial adjudication may be SAVED (ledger updated, still not applicable).
    part = client.put(
        f"/api/imports/{late_id}/resolutions",
        json={"resolutions": {"sample@60.0": "supersede"}},
    ).json()
    assert part["preview"]["all_conflicts_resolved"] is False
    stored_res = client.get(f"/api/imports/{late_id}").json()["resolutions"]
    assert stored_res == {"sample@60.0": "supersede"}
    assert client.post(f"/api/imports/{late_id}/apply").status_code == 409

    # Adjudicate: take the new reading, keep the existing drop time.
    resolved = client.put(
        f"/api/imports/{late_id}/resolutions",
        json={
            "resolutions": {
                "sample@60.0": "supersede",
                "drop@610.0": "keep_existing",
            }
        },
    ).json()
    assert resolved["preview"]["all_conflicts_resolved"] is True
    # preview now shows the intended post-apply state
    proj = resolved["preview"]["projected"]
    assert len(proj["samples"]) == 4
    assert [s for s in proj["samples"] if s["t_s"] == 60.0][0]["bean_temp_c"] == 112.5
    assert proj["metrics"]["total_s"] == 600.0

    ap = client.post(f"/api/imports/{late_id}/apply").json()
    assert ap["applied_result"]["samples_superseded"] == 1
    assert ap["applied_result"]["events_superseded"] == 0
    cur2 = client.get(f"/api/batches/{bid}/series").json()
    assert len(cur2["series"]["raw_points"]) == 4
    at60b = [p for p in cur2["series"]["raw_points"] if p["t_s"] == 60.0][0]
    assert at60b["bean_temp_c"] == 112.5 and at60b["import_package_id"] == "obs-late"
    assert cur2["metrics"]["total_s"] == 600.0  # drop kept

    # old reading retained as superseded history; events obey append-only
    history_events = client.get(f"/api/batches/{bid}/events?include_history=true").json()
    drops = [e for e in history_events if e["event_type"] == "drop"]
    assert len(drops) == 1 and drops[0]["t_s"] == 600.0
    # turning point added from the late package
    tps = [e for e in history_events if e["event_type"] == "turning_point"]
    assert len(tps) == 1 and tps[0]["import_package_id"] == "obs-late"


def test_conflict_use_incoming_event_supersedes_old(client):
    base = make_package(
        "obs-base2",
        [{"t_s": 0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
         {"t_s": 600, "bean_temp_c": 206.0, "env_temp_c": 225.0}],
        [{"event_type": "charge", "t_s": 0.0, "source": "manual"},
         {"event_type": "drop", "t_s": 600.0, "source": "manual"}],
    )
    bid = apply(client, deliver(client, base).json()["id"]).json()["batch_id"]

    late = make_package(
        "obs-late2",
        [{"t_s": 600, "bean_temp_c": 206.0, "env_temp_c": 225.0}],
        [{"event_type": "drop", "t_s": 612.0, "source": "manual",
          "label": "现场补录出锅"}],
        batch_name="FIELD-OBS-001",
    )
    body = deliver(client, late).json()
    ref = body["preview"]["conflicts"]["event_conflicts"][0]["ref"]
    ap = apply(client, body["id"], {ref: "use_incoming"}).json()
    assert ap["applied_result"]["events_superseded"] == 1
    hist = client.get(f"/api/batches/{bid}/events?include_history=true").json()
    drops = [e for e in hist if e["event_type"] == "drop"]
    assert len(drops) == 2
    current = [e for e in drops if not e["superseded"]]
    old = [e for e in drops if e["superseded"]]
    assert current[0]["t_s"] == 612.0 and current[0]["import_package_id"] == "obs-late2"
    assert old[0]["t_s"] == 600.0 and old[0]["import_package_id"] == "obs-base2"
    assert old[0]["superseded_by_id"] == current[0]["id"]


def test_incoming_mark_equal_to_superseded_history_still_conflicts(client):
    """A late package whose recorded mark equals an OLD, already-superseded
    value must NOT be silenced as a duplicate — it conflicts with whatever is
    current and stays pending until adjudicated."""
    base = make_package(
        "obs-hist-base",
        [{"t_s": 0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
         {"t_s": 600, "bean_temp_c": 206.0, "env_temp_c": 225.0}],
        [{"event_type": "charge", "t_s": 0.0, "source": "manual"},
         {"event_type": "drop", "t_s": 600.0, "source": "manual"}],
    )
    bid = apply(client, deliver(client, base).json()["id"]).json()["batch_id"]
    # correct the drop to 612
    corr = make_package(
        "obs-hist-corr",
        [{"t_s": 600, "bean_temp_c": 206.0, "env_temp_c": 225.0}],
        [{"event_type": "drop", "t_s": 612.0, "source": "manual"}],
        batch_name="FIELD-OBS-001",
    )
    cb = deliver(client, corr).json()
    ref = cb["preview"]["conflicts"]["event_conflicts"][0]["ref"]
    apply(client, cb["id"], {ref: "use_incoming"})

    # a late field package still carries the OLD 600 value
    late = make_package(
        "obs-hist-late",
        [{"t_s": 300, "bean_temp_c": 160.0, "env_temp_c": 210.0}],
        [{"event_type": "drop", "t_s": 600.0, "source": "manual"}],
        batch_name="FIELD-OBS-001",
    )
    lb = deliver(client, late).json()
    evc = lb["preview"]["conflicts"]["event_conflicts"]
    assert len(evc) == 1
    assert evc[0]["existing"]["t_s"] == 612.0  # adjudicates vs CURRENT, not 600
    # unresolved -> current analysis keeps 612
    cur = client.get(f"/api/batches/{bid}/series").json()
    assert cur["metrics"]["total_s"] == 612.0
    assert client.post(f"/api/imports/{lb['id']}/apply").status_code == 409
    # keep the (newer) current value: the late package adds only its sample
    apply(client, lb["id"], {evc[0]["ref"]: "keep_existing"})
    cur2 = client.get(f"/api/batches/{bid}/series").json()
    assert cur2["metrics"]["total_s"] == 612.0
    assert len(cur2["series"]["raw_points"]) == 3
    hist = client.get(f"/api/batches/{bid}/events?include_history=true").json()
    assert len([e for e in hist if e["event_type"] == "drop"]) == 2  # 600 old, 612 current


def test_intervening_package_changes_conflicts_before_apply(client):
    """Out-of-order/racy arrival: the conflict set is recomputed at apply
    time, so a package that was conflict-free can acquire a conflict and must
    not silently overwrite anything."""
    pkg = make_package(
        "obs-race-1",
        [{"t_s": 0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
         {"t_s": 60, "bean_temp_c": 110.0, "env_temp_c": 198.0}],
        [{"event_type": "charge", "t_s": 0.0, "source": "manual"}],
        batch_name="RACE",
    )
    first = deliver(client, pkg).json()
    assert first["preview"]["conflicts"]["n_conflicts"] == 0

    # another package for the same timestamp lands and gets applied first
    other = make_package(
        "obs-race-2",
        [{"t_s": 60, "bean_temp_c": 109.0, "env_temp_c": 198.0}],
        [],
        batch_name="RACE",
    )
    ob = deliver(client, other).json()
    # obs-race-1 already stored? not yet (pending), so obs-race-2 sees no
    # stored row either -> also pending with no conflict; but now applying
    # obs-race-1 first then obs-race-2 must require adjudication on re-check.
    apply(client, first["id"])
    detail = client.get(f"/api/imports/{ob['id']}").json()
    assert detail["preview"]["conflicts"]["n_conflicts"] == 1
    assert client.post(f"/api/imports/{ob['id']}/apply").status_code == 409


# ---------------------------------------------------------------------------
# ④ malformed packages fail the WHOLE package with no residue
# ---------------------------------------------------------------------------

def test_invalid_packages_are_rejected_wholesale_without_residue(client):
    good = make_package("obs-invalid-base", field_samples()[:3],
                        [{"event_type": "charge", "t_s": 0.0, "source": "manual"}])
    bid = apply(client, deliver(client, good).json()["id"]).json()["batch_id"]

    def reject(pkg_or_bytes, expected_code):
        r = client.post(
            "/api/imports",
            content=(pkg_or_bytes if isinstance(pkg_or_bytes, bytes)
                     else json.dumps(pkg_or_bytes).encode()),
        )
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["status"] == "rejected"
        assert b["error_code"] == expected_code, b
        return b

    # negative / non-numeric / non-finite time
    bad = json.loads(json.dumps(good))
    bad["samples"][0]["t_s"] = -1.0
    bad["summary"] = _summarize(bad)
    reject(bad, "invalid_timestamp")

    bad = json.loads(json.dumps(good))
    bad["samples"][0]["t_s"] = "soon"
    reject(bad, "invalid_timestamp")

    bad = json.loads(json.dumps(good))
    bad["batch"]["charge_at"] = "2026-99-99T00:00:00"
    reject(bad, "invalid_timestamp")

    # duplicate event chain (same anchor twice in package)
    bad = json.loads(json.dumps(good))
    bad["events"] = [
        {"event_type": "charge", "t_s": 0.0},
        {"event_type": "charge", "t_s": 0.0},
    ]
    bad["summary"] = _summarize(bad)
    reject(bad, "duplicate_event_chain")

    # two identical damper marks at the same position
    bad = json.loads(json.dumps(good))
    bad["events"] = [
        {"event_type": "damper_change", "t_s": 100.0, "value_num": 50.0},
        {"event_type": "damper_change", "t_s": 100.0, "value_num": 50.0},
    ]
    bad["summary"] = _summarize(bad)
    reject(bad, "duplicate_event_chain")

    # event types out of roast sequence in time
    bad = json.loads(json.dumps(good))
    bad["events"] = [
        {"event_type": "first_crack_start", "t_s": 600.0},
        {"event_type": "drop", "t_s": 400.0},
    ]
    bad["summary"] = _summarize(bad)
    reject(bad, "invalid_event_order")

    # damper out of range / missing payload
    bad = json.loads(json.dumps(good))
    bad["events"] = [{"event_type": "damper_change", "t_s": 100.0, "value_num": 150.0}]
    bad["summary"] = _summarize(bad)
    reject(bad, "invalid_event")

    # unknown event type
    bad = json.loads(json.dumps(good))
    bad["events"] = [{"event_type": "explosion", "t_s": 100.0}]
    bad["summary"] = _summarize(bad)
    reject(bad, "invalid_event")

    # both temps missing
    bad = json.loads(json.dumps(good))
    bad["samples"][1]["bean_temp_c"] = None
    bad["samples"][1]["env_temp_c"] = None
    bad["summary"] = _summarize(bad)
    reject(bad, "invalid_sample")

    # digest mismatch (tampered content, stale summary)
    bad = json.loads(json.dumps(good))
    bad["summary"]["sha256"] = "0" * 64
    reject(bad, "digest_mismatch")

    # summary counts inconsistent
    bad = json.loads(json.dumps(good))
    bad["summary"]["n_events"] = 42
    reject(bad, "invalid_summary")

    # unsupported version
    bad = json.loads(json.dumps(good))
    bad["format_version"] = 9
    reject(bad, "unsupported_version")

    # empty / non-UTF8 / syntactically broken payloads
    reject(b"", "parse_error")
    reject(b"{not valid json", "parse_error")
    reject(b"\xff\xfe\x00garbage", "parse_error")

    # empty samples list
    bad = json.loads(json.dumps(good))
    bad["samples"] = []
    bad["summary"] = {"n_samples": 0, "n_events": 1, "t_first_s": 0,
                      "t_last_s": 0, "n_missing_bean": 0, "n_missing_env": 0,
                      "sha256": importer.compute_digest(bad)}
    reject(bad, "empty_package")

    # NO residue: the base batch is untouched
    stored = client.get(f"/api/batches/{bid}/series").json()
    assert len(stored["series"]["raw_points"]) == 3
    assert len(client.get(f"/api/batches/{bid}/events").json()) == 1
    # rejected rows auditable in the ledger
    rejected = [r for r in client.get("/api/imports?status=rejected").json()]
    assert len(rejected) >= 12
    assert all(r["error_detail"] for r in rejected)
    assert all(r["batch_id"] is None for r in rejected)


def test_rejected_blob_retry_is_idempotent(client):
    bad = b"{broken"
    a = client.post("/api/imports", content=bad).json()
    b = client.post("/api/imports", content=bad).json()
    assert a["status"] == "rejected"
    assert b["redelivered"] is True and b["id"] == a["id"]
    rejected = client.get("/api/imports?status=rejected").json()
    assert len(rejected) == 1


# ---------------------------------------------------------------------------
# provenance coexistence with synthetic seeds
# ---------------------------------------------------------------------------

def test_imported_points_coexist_with_synthetic_seed_provenance(client):
    r = client.post("/api/seed")
    assert r.status_code == 200
    synth_batches = r.json()
    aid = synth_batches[0]["id"]
    ex = client.get(f"/api/batches/{aid}/export").json()
    assert {p["source"] for p in ex["series"]["raw_points"]} == {"synthetic"}
    assert all(p["import_package_id"] is None for p in ex["series"]["raw_points"])

    # a distinct field batch imported alongside
    pkg = make_package("obs-sidecar", field_samples(), field_events(),
                       batch_name="FIELD-SIDE-1")
    iid = deliver(client, pkg).json()["id"]
    side_bid = apply(client, iid).json()["batch_id"]
    assert side_bid != aid
    side = client.get(f"/api/batches/{side_bid}/series").json()
    assert {p["source"] for p in side["series"]["raw_points"]} == {"observation_package"}
    # synthetic batch untouched
    again = client.get(f"/api/batches/{aid}/series").json()
    assert len(again["series"]["raw_points"]) == len(ex["series"]["raw_points"])


def test_discard_pending_package_leaves_no_data(client):
    pkg = make_package("obs-discard", field_samples(), field_events())
    iid = deliver(client, pkg).json()["id"]
    r = client.post(f"/api/imports/{iid}/discard")
    assert r.status_code == 200 and r.json()["status"] == "discarded"
    # no batch was created for it
    names = {b["name"] for b in client.get("/api/batches").json()}
    assert "FIELD-OBS-001" not in names
    # cannot apply after discard
    assert client.post(f"/api/imports/{iid}/apply").status_code == 409
