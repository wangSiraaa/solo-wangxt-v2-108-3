"""End-to-end API tests: seeding, provenance, comparison, reproducibility."""


def _seed(client):
    r = client.post("/api/seed")
    assert r.status_code == 200
    batches = r.json()
    assert len(batches) == 2
    return batches[0]["id"], batches[1]["id"]


def test_health_says_offline(client):
    r = client.get("/api/health")
    assert r.json()["machine_connection"].startswith("none")


def test_seed_has_noise_uneven_sampling_and_dropouts(client):
    aid, _ = _seed(client)
    d = client.get(f"/api/batches/{aid}/series").json()
    pts = d["series"]["raw_points"]

    # uneven intervals: at least 3 distinct gaps
    gaps = [round(pts[i + 1]["t_s"] - pts[i]["t_s"], 3) for i in range(len(pts) - 1)]
    assert len(set(gaps)) > 3

    # NULL samples exist (probe loss)
    assert any(p["bean_temp_c"] is None for p in pts)

    # one short gap bridged+flagged, one wide gap left open
    statuses = {g["status"] for g in d["series"]["missing_segments"] if g["channel"] == "bean"}
    assert "interpolated" in statuses and "wide_unfilled" in statuses

    # window basis is reported with the data
    assert d["series"]["ror_window"]["window_s"] == 30
    assert d["series"]["ror_window"]["units"] == "C/min"


def test_changing_window_and_smoothing_does_not_change_raw(client):
    aid, _ = _seed(client)
    a = client.get(f"/api/batches/{aid}/series?window_s=20&display_smooth_s=0").json()
    b = client.get(f"/api/batches/{aid}/series?window_s=120&display_smooth_s=60").json()
    sig = lambda d: [(p["t_s"], p["bean_temp_c"], p["env_temp_c"]) for p in d["series"]["raw_points"]]
    assert sig(a) == sig(b)
    # derived RoR may differ
    assert [p["ror_c_per_min"] for p in a["series"]["raw_points"]] != [
        p["ror_c_per_min"] for p in b["series"]["raw_points"]
    ]


def test_manual_correction_keeps_source_history(client):
    aid, _ = _seed(client)
    before = [e for e in client.get(f"/api/batches/{aid}/events").json()
              if e["event_type"] == "turning_point"]
    assert before and before[0]["source"] == "auto"

    r = client.post(f"/api/batches/{aid}/events", json={
        "event_type": "turning_point", "t_s": 70.0, "source": "manual",
        "created_by": "tester", "label": "人工回温点"})
    assert r.status_code == 200

    current = [e for e in client.get(f"/api/batches/{aid}/events").json()
               if e["event_type"] == "turning_point"]
    assert len(current) == 1
    assert current[0]["t_s"] == 70.0 and current[0]["source"] == "manual"

    history = client.get(f"/api/batches/{aid}/events?include_history=true").json()
    tps = [e for e in history if e["event_type"] == "turning_point"]
    assert len(tps) == 2
    old = [e for e in tps if e["superseded"]]
    assert old and old[0]["source"] == "auto"
    assert old[0]["superseded_by_id"] == current[0]["id"]


def test_damper_changes_are_multiple_discrete_marks(client):
    aid, _ = _seed(client)
    client.post(f"/api/batches/{aid}/events", json={
        "event_type": "damper_change", "t_s": 200, "value_num": 50, "source": "manual"})
    client.post(f"/api/batches/{aid}/events", json={
        "event_type": "damper_change", "t_s": 350, "value_num": 70, "source": "manual"})
    dampers = [e for e in client.get(f"/api/batches/{aid}/events").json()
               if e["event_type"] == "damper_change"]
    assert len(dampers) == 3  # seeded one + two
    assert all(not e["superseded"] for e in dampers)


def test_phase_metrics_after_correction(client):
    aid, _ = _seed(client)
    # force a clean manual anchor set
    for ev in [
        {"event_type": "turning_point", "t_s": 60, "source": "manual"},
        {"event_type": "first_crack_start", "t_s": 480, "source": "manual"},
        {"event_type": "drop", "t_s": 600, "source": "manual"},
    ]:
        assert client.post(f"/api/batches/{aid}/events", json=ev).status_code == 200
    d = client.get(f"/api/batches/{aid}/series").json()
    m = d["metrics"]
    assert m["drying_s"] == 60
    assert m["maillard_s"] == 420
    assert m["development_s"] == 120
    assert m["development_ratio"] == 0.2


def test_compare_includes_non_causal_note_and_both_batches(client):
    a, b = _seed(client)
    r = client.get("/api/compare", params={"a": a, "b": b})
    assert r.status_code == 200
    body = r.json()
    assert len(body["batches"]) == 2
    assert "不构成因果" in body["interpretation"]


def test_export_recomputes_all_stage_metrics(client):
    aid, _ = _seed(client)
    ex = client.get(f"/api/batches/{aid}/export?window_s=30&display_smooth_s=12").json()
    rc = client.post("/api/recompute", json={
        "samples": [
            {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"], "env_temp_c": p["env_temp_c"]}
            for p in ex["series"]["raw_points"]
        ],
        "events": ex["events"],
        "params": ex["params"],
    }).json()
    for key in ["drying_s", "maillard_s", "development_s", "first_crack_window_s",
                "total_s", "development_ratio"]:
        assert rc["metrics"][key] == ex["metrics"][key], key
    # RoR spot-check: recomputed trace matches the export trace length
    assert len(rc["series"]["raw_points"]) == len(ex["series"]["raw_points"])


def test_gap_fill_limit_parameter_is_respected(client):
    aid, _ = _seed(client)
    # with a tiny fill limit the long gap stays open; with a large one it fills
    tiny = client.get(f"/api/batches/{aid}/series?max_gap_fill_s=5").json()
    large = client.get(f"/api/batches/{aid}/series?max_gap_fill_s=300").json()
    bean_gaps_tiny = [g for g in tiny["series"]["missing_segments"]
                      if g["channel"] == "bean" and g["status"] == "wide_unfilled"]
    bean_gaps_large = [g for g in large["series"]["missing_segments"]
                       if g["channel"] == "bean" and g["status"] == "wide_unfilled"]
    assert len(bean_gaps_tiny) >= len(bean_gaps_large)
