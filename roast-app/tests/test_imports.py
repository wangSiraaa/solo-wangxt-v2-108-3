"""Offline observation-package import tests.

Covers the five acceptance behaviours:

1. legal package (uneven sampling, missing readings, manual events) ->
   preview and apply agree; curve and export show source package;
2. identical re-delivery adds nothing and returns the original result;
3. late package disagreeing at the same second stays pending; current
   analysis does not move before adjudication;
4. illegal time / duplicate event chain / parse failure -> whole package
   fails, no residual changes to batches/samples/events;
5. after refresh, re-export + independent recompute reproduce the event
   history, sample provenance and phase metrics.
"""
from __future__ import annotations

import copy
import json

from app.importer.package import canonical_digest
from app.models import Batch, Event, ImportBatch, Sample


# ---------------------------------------------------------------------------
# package builders
# ---------------------------------------------------------------------------


def _sign(raw: dict, *, digest: str | None = "__auto__") -> dict:
    raw = copy.deepcopy(raw)
    if digest is None:
        return raw
    raw["digest"] = {
        "algorithm": "sha256",
        "sha256": canonical_digest(raw) if digest == "__auto__" else digest,
    }
    return raw


def pkg(
    package_id: str = "field-001",
    *,
    samples=None,
    events=None,
    batch_name: str = "FIELD-2026-0921-01",
    **overrides,
) -> dict:
    """A valid uneven-sampling package with gaps and manual events."""
    if samples is None:
        samples = [
            {"t_s": 0.0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
            {"t_s": 2.3, "bean_temp_c": 165.1, "env_temp_c": 190.8},
            {"t_s": 5.9, "bean_temp_c": 154.7, "env_temp_c": 191.6},
            {"t_s": 9.1, "bean_temp_c": None, "env_temp_c": 192.4, "note": "豆温探针短暂失联"},
            {"t_s": 14.6, "bean_temp_c": 146.2, "env_temp_c": 193.1},
            {"t_s": 21.7, "bean_temp_c": 142.0, "env_temp_c": 194.0},
            {"t_s": 30.5, "bean_temp_c": 143.8, "env_temp_c": 195.2},
            {"t_s": 60.0, "bean_temp_c": 150.5, "env_temp_c": 197.0},
            {"t_s": 120.0, "bean_temp_c": 168.0, "env_temp_c": 201.0},
            {"t_s": 240.0, "bean_temp_c": 185.0, "env_temp_c": 208.0},
            {"t_s": 301.4, "bean_temp_c": 188.4, "env_temp_c": 210.0},
            {"t_s": 420.0, "bean_temp_c": 198.0, "env_temp_c": 213.0},
            {"t_s": 540.0, "bean_temp_c": 205.0, "env_temp_c": 216.0},
        ]
    if events is None:
        events = [
            {"event_uid": "f-charge", "event_type": "charge", "t_s": 0.0,
             "source": "manual", "created_by": "lin"},
            {"event_uid": "f-tp", "event_type": "turning_point", "t_s": 21.7,
             "source": "manual", "created_by": "lin", "label": "现场回温点"},
            {"event_uid": "f-damp", "event_type": "damper_change", "t_s": 300.0,
             "source": "manual", "created_by": "lin", "value_num": 35.0,
             "label": "风门 70->35"},
            {"event_uid": "f-fc", "event_type": "first_crack_start", "t_s": 420.0,
             "source": "manual", "created_by": "lin"},
            {"event_uid": "f-drop", "event_type": "drop", "t_s": 540.0,
             "source": "manual", "created_by": "lin"},
        ]
    raw = {
        "format_version": 1,
        "package_id": package_id,
        "generated_at": "2026-09-21T14:05:00",
        "batch": {
            "name": batch_name,
            "roaster": "field-tr-1 (offline recorder)",
            "bean": "Ethiopia Yirgacheffe",
            "charge_at": "2026-09-21T13:00:00",
            "charge_temp_c": 180.0,
            "ambient_temp_c": 22.5,
            "target_drop_temp_c": 205.0,
            "note": "现场离线记录包",
        },
        "samples": samples,
        "events": events,
    }
    raw.update(overrides)
    return _sign(raw)


def _codes(resp) -> list[str]:
    return [f["code"] for f in resp.json()["detail"]["findings"]]


def _receive(client, body_package: dict, target=None, expect: int = 200):
    r = client.post(
        "/api/imports",
        json={"package": body_package, "target_batch_id": target},
    )
    assert r.status_code == expect, r.text
    return r.json()


# ---------------------------------------------------------------------------
# ① legal package: preview == apply, curve/export show source
# ---------------------------------------------------------------------------


def test_legal_package_preview_then_apply_consistent(client):
    body = pkg()
    rec = _receive(client, body)
    assert rec["status"] == "pending_review"
    assert rec["n_samples"] == 13 and rec["n_events"] == 5
    assert rec["conflict_count"] == 0

    pv = client.get(f"/api/imports/{rec['id']}/preview").json()
    proj = pv["projection"]
    # uneven sampling preserved; missing reading preserved (not filled in raw)
    pts = proj["series"]["raw_points"]
    assert [p["t_s"] for p in pts] == sorted(p["t_s"] for p in pts)
    missing = [p for p in pts if p["bean_temp_c"] is None]
    assert missing and missing[0]["t_s"] == 9.1
    # projected provenance already labels the source package
    assert all(p["source_package_id"] == "field-001" for p in pts)
    assert proj["metrics"]["drying_s"] == round(21.7 - 0.0, 3)
    assert proj["metrics"]["development_s"] == 120.0
    assert proj["event_counts"]["added"] == 5

    res = client.post(f"/api/imports/{rec['id']}/apply")
    assert res.status_code == 200, res.text
    applied = res.json()
    bid = applied["batch_id"]
    assert applied["samples"]["added"] == 13
    assert applied["events"]["added"] == 5

    # applied curve equals the preview point-for-point
    live = client.get(f"/api/batches/{bid}/series").json()
    sig = lambda arr: [(p["t_s"], p["bean_temp_c"], p["env_temp_c"]) for p in arr]
    assert sig(live["series"]["raw_points"]) == sig(proj["series"]["raw_points"])
    assert live["metrics"] == proj["metrics"]
    # event history (damper coexists; all sourced)
    assert len(live["events"]) == 5

    # curve shows source package per point; export too
    assert live["provenance"]["imported_sample_count"] == 13
    by_pkg = {x["source_package_id"]: x["count"] for x in live["provenance"]["samples_by_source"]}
    assert by_pkg == {"field-001": 13}
    ex = client.get(f"/api/batches/{bid}/export").json()
    assert all(p["source"] == "imported" and p["source_package_id"] == "field-001"
               for p in ex["series"]["raw_points"])
    ev_pkgs = {e["source_package_id"] for e in ex["events"]}
    assert ev_pkgs == {"field-001"}
    assert ex["provenance"]["imported_sample_count"] == 13


def test_export_recompute_reproduces_imported_batch(client):
    rec = _receive(client, pkg())
    bid = client.post(f"/api/imports/{rec['id']}/apply").json()["batch_id"]
    ex = client.get(f"/api/batches/{bid}/export?window_s=30&display_smooth_s=12").json()
    # independent recompute from exported raw samples + full event history
    rc = client.post("/api/recompute", json={
        "samples": [
            {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"], "env_temp_c": p["env_temp_c"],
             "source": p["source"], "source_package_id": p["source_package_id"]}
            for p in ex["series"]["raw_points"]
        ],
        "events": ex["events"],
        "params": ex["params"],
    }).json()
    for key in ["drying_s", "maillard_s", "development_s", "first_crack_window_s",
                "total_s", "development_ratio"]:
        assert rc["metrics"][key] == ex["metrics"][key], key
    assert len(rc["series"]["raw_points"]) == len(ex["series"]["raw_points"])
    # provenance survives the independent recompute
    assert rc["series"]["raw_points"][0]["source_package_id"] == "field-001"
    # event history (incl. superseded rows) and current-event selection reproduce
    assert len(rc["current_events"]) == len([
        e for e in ex["events"] if not e["superseded"]
    ])
    hist_sig = sorted(
        (e["event_type"], e["t_s"], e["superseded"], e["event_uid"],
         e["source_package_id"])
        for e in ex["events"]
    )
    assert all(item[3] for item in hist_sig)  # every event keeps its uid
    assert ex["provenance"]["event_history_by_source_package"][0]["total"] == 5
    # re-exporting again (simulating refresh) yields identical content
    ex2 = client.get(f"/api/batches/{bid}/export?window_s=30&display_smooth_s=12").json()
    assert json.dumps(ex2["events"], sort_keys=True) == json.dumps(ex["events"], sort_keys=True)
    assert {
        (p["t_s"], p["bean_temp_c"], p["source_package_id"])
        for p in ex2["series"]["raw_points"]
    } == {
        (p["t_s"], p["bean_temp_c"], p["source_package_id"])
        for p in ex["series"]["raw_points"]
    }


# ---------------------------------------------------------------------------
# ② identical re-delivery: nothing new, same result
# ---------------------------------------------------------------------------


def test_identical_package_retry_is_idempotent(client):
    body = pkg()
    first = _receive(client, body)
    second = _receive(client, body)
    assert second["id"] == first["id"]

    applied1 = client.post(f"/api/imports/{first['id']}/apply").json()
    applied2 = client.post(f"/api/imports/{first['id']}/apply").json()
    assert applied1 == applied2

    bid = applied1["batch_id"]
    series = client.get(f"/api/batches/{bid}/series?include_history=true").json()
    assert len(series["series"]["raw_points"]) == 13
    assert len(series["events"]) == 5
    # a third identical delivery after apply still returns the same row/result
    third = _receive(client, body)
    assert third["id"] == first["id"] and third["status"] == "applied"


def test_same_package_id_different_content_is_rejected(client):
    _receive(client, pkg())
    tampered = pkg()
    tampered["samples"][0]["bean_temp_c"] = 9.0
    tampered["digest"]["sha256"] = canonical_digest(tampered)
    r = client.post("/api/imports", json={"package": tampered})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# ③ late package, same second, different reading -> pending adjudication
# ---------------------------------------------------------------------------


def test_late_conflicting_reading_holds_current_analysis(client):
    bid = client.post(f"/api/imports/{_receive(client, pkg())['id']}/apply").json()["batch_id"]

    late = pkg(
        "field-001-late",
        samples=[
            # disagreeing measurement at an existing second
            {"t_s": 240.0, "bean_temp_c": 999.0, "env_temp_c": 208.0},
            # backfill offer for nothing-stored (both channels present elsewhere)
            {"t_s": 260.0, "bean_temp_c": 187.0, "env_temp_c": 209.0},
        ],
        events=[
            {"event_uid": "l-drop", "event_type": "drop", "t_s": 560.0,
             "source": "manual", "created_by": "lin"},
        ],
        batch_name="FIELD-2026-0921-01",  # irrelevant: target set explicitly
    )
    rec = _receive(client, late, target=bid)
    assert rec["status"] == "pending_review"
    assert rec["conflict_count"] == 1

    # current analysis unchanged before adjudication
    before = client.get(f"/api/batches/{bid}/series").json()
    assert [p["bean_temp_c"] for p in before["series"]["raw_points"] if p["t_s"] == 240.0] == [185.0]
    assert before["metrics"]["total_s"] == 540.0

    pv = client.get(f"/api/imports/{rec['id']}/preview").json()
    kinds = {c["kind"] for c in pv["conflicts"]}
    assert kinds == {"value_mismatch"}
    # projection keeps existing reading while pending
    val = [p["bean_temp_c"] for p in pv["projection"]["series"]["raw_points"] if p["t_s"] == 240.0]
    assert val == [185.0]
    # new, non-conflicting points are already part of the projected curve
    assert any(p["t_s"] == 260.0 for p in pv["projection"]["series"]["raw_points"])
    # but the event supersede cannot happen until apply: current anchors stand
    assert before["metrics"]["total_s"] == 540.0

    # apply blocked while unresolved
    assert client.post(f"/api/imports/{rec['id']}/apply").status_code == 409

    # keep the existing measurement
    cid = pv["conflicts"][0]["id"]
    rr = client.post(f"/api/imports/{rec['id']}/resolve",
                     json={"resolutions": {cid: "keep_existing"}, "resolved_by": "lead"})
    assert rr.status_code == 200
    # preview AFTER adjudication must be exactly what apply then writes
    pv2 = client.get(f"/api/imports/{rec['id']}/preview").json()
    applied = client.post(f"/api/imports/{rec['id']}/apply").json()
    assert applied["samples"]["added"] == 1       # t=260 new
    assert applied["samples"]["existing_kept"] == 1  # t=240 bean kept
    assert applied["samples"]["identical_skipped"] == 1  # t=240 env equal
    after = client.get(f"/api/batches/{bid}/series?include_history=true").json()
    sig = lambda arr: [(p["t_s"], p["bean_temp_c"], p["env_temp_c"], p["source_package_id"])
                       for p in arr]
    assert sig(after["series"]["raw_points"]) == sig(pv2["projection"]["series"]["raw_points"])
    assert after["metrics"] == pv2["projection"]["metrics"]
    assert [p["bean_temp_c"] for p in after["series"]["raw_points"] if p["t_s"] == 240.0] == [185.0]
    assert len(after["series"]["raw_points"]) == 14
    # late drop event superseded the earlier drop with history retained
    drops = [e for e in after["events"] if e["event_type"] == "drop"]
    assert len(drops) == 2
    old = [e for e in drops if e["superseded"]][0]
    new = [e for e in drops if not e["superseded"]][0]
    assert old["t_s"] == 540.0 and new["t_s"] == 560.0
    assert old["superseded_by_id"] == new["id"]
    assert new["source_package_id"] == "field-001-late"


def test_late_conflict_use_incoming_updates_with_audit(client):
    bid = client.post(f"/api/imports/{_receive(client, pkg())['id']}/apply").json()["batch_id"]
    late = pkg(
        "field-001-fix",
        samples=[{"t_s": 240.0, "bean_temp_c": 186.2, "env_temp_c": 208.0}],
        events=[],
    )
    rec = _receive(client, late, target=bid)
    cid = client.get(f"/api/imports/{rec['id']}/preview").json()["conflicts"][0]["id"]
    client.post(f"/api/imports/{rec['id']}/resolve",
                json={"resolutions": {cid: "use_incoming"}, "resolved_by": "lead"})
    out = client.post(f"/api/imports/{rec['id']}/apply").json()
    assert out["samples"]["updated"] == 1
    live = client.get(f"/api/batches/{bid}/series").json()
    p = [p for p in live["series"]["raw_points"] if p["t_s"] == 240.0][0]
    assert p["bean_temp_c"] == 186.2 and p["source_package_id"] == "field-001-fix"


def test_missing_fill_conflict_for_backfill(client):
    bid = client.post(f"/api/imports/{_receive(client, pkg())['id']}/apply").json()["batch_id"]
    # recorder later recovers the reading it lost at t=9.1
    fill = pkg(
        "field-001-fill",
        samples=[{"t_s": 9.1, "bean_temp_c": 149.5, "env_temp_c": 192.4}],
        events=[],
    )
    rec = _receive(client, fill, target=bid)
    assert rec["conflict_count"] == 1
    pv = client.get(f"/api/imports/{rec['id']}/preview").json()
    assert pv["conflicts"][0]["kind"] == "missing_fill"
    # pending: NULL stays NULL (no invented measurement, current analysis fixed)
    val = [p["bean_temp_c"] for p in pv["projection"]["series"]["raw_points"] if p["t_s"] == 9.1]
    assert val == [None]
    cid = pv["conflicts"][0]["id"]
    client.post(f"/api/imports/{rec['id']}/resolve",
                json={"resolutions": {cid: "use_incoming"}})
    client.post(f"/api/imports/{rec['id']}/apply")
    live = client.get(f"/api/batches/{bid}/series").json()
    p = [p for p in live["series"]["raw_points"] if p["t_s"] == 9.1][0]
    assert p["bean_temp_c"] == 149.5 and p["source_package_id"] == "field-001-fill"


# ---------------------------------------------------------------------------
# ④ invalid packages: whole-package failure, no residue
# ---------------------------------------------------------------------------


def test_illegal_phase_order_fails_whole_package(client):
    # The chain is non-decreasing in time, but the physical roast anchors are
    # inverted: charge is recorded after the turning point.
    bad = pkg("bad-order", events=[
        {"event_uid": "a", "event_type": "turning_point", "t_s": 50, "source": "manual"},
        {"event_uid": "b", "event_type": "charge", "t_s": 100, "source": "manual"},
    ])
    r = client.post("/api/imports", json={"package": bad})
    assert r.status_code == 422
    assert "event_phase_order_invalid" in _codes(r)
    assert client.get("/api/batches").json() == []
    led = client.get("/api/imports?status=failed").json()
    assert any(x["package_id"] == "bad-order" and x["error_code"] == "package_invalid" for x in led)


def test_supersede_pointing_backwards_and_type_mismatch_fail(client):
    # the correction claims to supersede an event that happens *later* — an
    # impossible event order; plus the two events are different types.
    bad = pkg("bad-chain", events=[
        {"event_uid": "a", "event_type": "turning_point", "t_s": 10, "source": "manual"},
        {"event_uid": "b", "event_type": "charge", "t_s": 0, "source": "manual",
         "supersedes_uid": "a"},
    ])
    r = client.post("/api/imports", json={"package": bad})
    assert r.status_code == 422
    codes = _codes(r)
    assert "supersede_time_before_original" in codes
    assert "supersede_type_mismatch" in codes
    assert client.get("/api/batches").json() == []


def test_events_logged_out_of_timeline_order_are_accepted(client):
    # A damper change is notebooked after the drop (common in field logs);
    # independent events merge by t_s and are not an invalid order.
    late_log = pkg("late-note", events=[
        {"event_uid": "f-charge", "event_type": "charge", "t_s": 0.0, "source": "manual"},
        {"event_uid": "f-tp", "event_type": "turning_point", "t_s": 21.7, "source": "manual"},
        {"event_uid": "f-fc", "event_type": "first_crack_start", "t_s": 420.0, "source": "manual"},
        {"event_uid": "f-drop", "event_type": "drop", "t_s": 540.0, "source": "manual"},
        {"event_uid": "f-damp", "event_type": "damper_change", "t_s": 300.0,
         "source": "manual", "value_num": 35.0},
    ])
    rec = _receive(client, late_log)
    assert rec["status"] == "pending_review"


def test_duplicate_event_uid_fails(client):
    bad = pkg("dup-uid", events=[
        {"event_uid": "x", "event_type": "charge", "t_s": 0, "source": "manual"},
        {"event_uid": "x", "event_type": "drop", "t_s": 100, "source": "manual"},
    ])
    r = client.post("/api/imports", json={"package": bad})
    assert r.status_code == 422
    assert "event_uid_duplicate" in _codes(r)


def test_duplicate_sample_timestamp_and_illegal_time_fail(client):
    bad = pkg("dup-t", samples=[
        {"t_s": 0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
        {"t_s": 0, "bean_temp_c": 181.0, "env_temp_c": 190.0},
    ])
    r = client.post("/api/imports", json={"package": bad})
    assert r.status_code == 422
    codes = _codes(r)
    assert "sample_duplicate_t_s" in codes

    neg = pkg("neg-t", samples=[
        {"t_s": -1.0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
    ])
    r = client.post("/api/imports", json={"package": neg})
    assert r.status_code == 422
    assert client.get("/api/batches").json() == []


def test_parse_error_and_digest_and_version_failures(client):
    # malformed JSON text
    r = client.post("/api/imports", content="{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422  # request itself unparseable

    # structurally broken object
    r = client.post("/api/imports", json={"package": {"hello": "world"}})
    assert r.status_code == 422

    # wrong digest
    wrong = pkg("wrong-sig")
    wrong["digest"]["sha256"] = "0" * 64
    r = client.post("/api/imports", json={"package": wrong})
    assert r.status_code == 422 and "digest_mismatch" in _codes(r)

    # unsupported version
    future = pkg("future-v")
    future["format_version"] = 9
    future["digest"]["sha256"] = canonical_digest(future)
    r = client.post("/api/imports", json={"package": future})
    assert r.status_code == 422 and "format_version_unsupported" in _codes(r)

    # no residue anywhere except auditable failed ledger rows
    assert client.get("/api/batches").json() == []
    failed = client.get("/api/imports?status=failed").json()
    assert {x["package_id"] for x in failed} >= {"wrong-sig", "future-v"}


def test_failed_package_unchanged_retry_returns_same_ledger_row(client):
    bad = pkg("bad-retry", events=[
        {"event_uid": "a", "event_type": "drop", "t_s": 100, "source": "manual"},
        {"event_uid": "b", "event_type": "first_crack_start", "t_s": 200, "source": "manual"},
    ])
    r1 = client.post("/api/imports", json={"package": bad})
    r2 = client.post("/api/imports", json={"package": bad})
    assert r1.status_code == r2.status_code == 422
    i1 = r1.json()["detail"]["import"]["id"]
    i2 = r2.json()["detail"]["import"]["id"]
    assert i1 == i2


# ---------------------------------------------------------------------------
# out-of-order *arrival* of valid packages (fine; merge by t) + ledger
# ---------------------------------------------------------------------------


def test_out_of_order_arrival_merges(client):
    # later part of the roast arrives first...
    tail = pkg(
        "oo-tail",
        samples=[
            {"t_s": 420.0, "bean_temp_c": 198.0, "env_temp_c": 213.0},
            {"t_s": 540.0, "bean_temp_c": 205.0, "env_temp_c": 216.0},
        ],
        events=[
            {"event_uid": "t-fc", "event_type": "first_crack_start", "t_s": 420.0,
             "source": "manual"},
            {"event_uid": "t-drop", "event_type": "drop", "t_s": 540.0,
             "source": "manual"},
        ],
    )
    bid = client.post(f"/api/imports/{_receive(client, tail)['id']}/apply").json()["batch_id"]
    # ...then the head of the roast shows up later
    head = pkg(
        "oo-head",
        samples=[
            {"t_s": 0.0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
            {"t_s": 60.0, "bean_temp_c": 150.0, "env_temp_c": 197.0},
        ],
        events=[
            {"event_uid": "h-charge", "event_type": "charge", "t_s": 0.0,
             "source": "manual"},
        ],
    )
    rec = _receive(client, head, target=bid)
    assert rec["conflict_count"] == 0
    client.post(f"/api/imports/{rec['id']}/apply")
    live = client.get(f"/api/batches/{bid}/series").json()
    ts = [p["t_s"] for p in live["series"]["raw_points"]]
    assert ts == sorted(ts) and len(ts) == 4
    # provenance per point follows the delivering package
    src = {p["t_s"]: p["source_package_id"] for p in live["series"]["raw_points"]}
    assert src[0.0] == "oo-head" and src[540.0] == "oo-tail"
    # metrics now span both deliveries
    assert live["metrics"]["total_s"] == 540.0


def test_abort_leaves_analysis_untouched(client):
    bid = client.post(f"/api/imports/{_receive(client, pkg())['id']}/apply").json()["batch_id"]
    before = client.get(f"/api/batches/{bid}/series").json()
    late = pkg("abort-me",
               samples=[{"t_s": 700.0, "bean_temp_c": 220.0, "env_temp_c": 230.0}],
               events=[])
    rec = _receive(client, late, target=bid)
    assert client.post(f"/api/imports/{rec['id']}/abort").status_code == 200
    after = client.get(f"/api/batches/{bid}/series").json()
    assert len(after["series"]["raw_points"]) == len(before["series"]["raw_points"])
    assert client.post(f"/api/imports/{rec['id']}/apply").status_code == 409


def test_intra_package_supersede_chain_is_kept(client):
    events = [
        {"event_uid": "tp1", "event_type": "turning_point", "t_s": 20.0, "source": "manual"},
        {"event_uid": "tp2", "event_type": "turning_point", "t_s": 22.0, "source": "manual",
         "supersedes_uid": "tp1", "label": "现场复核回温点"},
    ]
    rec = _receive(client, pkg("chain-pkg", events=events))
    bid = client.post(f"/api/imports/{rec['id']}/apply").json()["batch_id"]
    hist = client.get(f"/api/batches/{bid}/events?include_history=true").json()
    tps = sorted((e for e in hist if e["event_type"] == "turning_point"), key=lambda e: e["id"])
    assert len(tps) == 2
    old, new = tps
    assert old["superseded"] is True and old["superseded_by_id"] == new["id"]
    assert new["event_uid"] == "tp2" and new["source_package_id"] == "chain-pkg"


def test_recorder_event_uids_may_repeat_across_batches(client):
    # Two different roasts both use the same recorder-local uids; that must
    # not collide (uniqueness is batch_id + event_uid).
    r1 = _receive(client, pkg("roast-a", batch_name="ROAST-A"))
    b1 = client.post(f"/api/imports/{r1['id']}/apply").json()["batch_id"]
    r2 = _receive(client, pkg("roast-b", batch_name="ROAST-B"))
    b2 = client.post(f"/api/imports/{r2['id']}/apply").json()["batch_id"]
    assert b1 != b2
    for bid in (b1, b2):
        hist = client.get(f"/api/batches/{bid}/events").json()
        assert {e["event_uid"] for e in hist} >= {"f-charge", "f-drop"}
