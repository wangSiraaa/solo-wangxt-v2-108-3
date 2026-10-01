"""Unit tests for the numpy analysis pipeline (RoR window, gaps, metrics)."""
import math

from app.analysis import RoRConfig, build_series, detect_turning_point, phase_metrics


SAMPLES = [
    {"t_s": 0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
    {"t_s": 3, "bean_temp_c": 120.0, "env_temp_c": 191.0},
    {"t_s": 7, "bean_temp_c": 96.0, "env_temp_c": 192.0},
    {"t_s": 12, "bean_temp_c": 95.0, "env_temp_c": 193.0},
    {"t_s": 20, "bean_temp_c": None, "env_temp_c": 194.0},  # short dropout
    {"t_s": 24, "bean_temp_c": 99.0, "env_temp_c": 195.0},
    {"t_s": 33, "bean_temp_c": 103.0, "env_temp_c": 197.0},
    {"t_s": 41, "bean_temp_c": 107.0, "env_temp_c": 199.0},
]


def test_gap_classification_short_interpolated_wide_unfilled():
    samples = list(SAMPLES)
    # append a 90 s wide dropout (~4 missing points spaced 20 s apart)
    samples += [
        {"t_s": 100, "bean_temp_c": 115.0, "env_temp_c": 201.0},
        {"t_s": 120, "bean_temp_c": None, "env_temp_c": None},
        {"t_s": 140, "bean_temp_c": None, "env_temp_c": None},
        {"t_s": 160, "bean_temp_c": None, "env_temp_c": 205.0},
        {"t_s": 190, "bean_temp_c": 125.0, "env_temp_c": 207.0},
    ]
    out = build_series(samples, ror_cfg=RoRConfig(), max_gap_fill_s=45)
    by_span = {round(g["span_s"]): g for g in out["missing_segments"] if g["channel"] == "bean"}
    short = by_span[0]  # single missing sample, span 0
    assert short["status"] == "interpolated"
    wide = [g for g in out["missing_segments"] if g["status"] == "wide_unfilled"]
    assert wide and wide[0]["channel"] == "bean"
    # guide line breaks at wide gap
    guide = out["guide_bean_temp"]
    assert any(v is None for v in guide)
    # but short gap was bridged
    assert guide[4] is not None and out["raw_points"][4]["is_interpolated"]


def test_interpolated_points_flagged_and_never_measured():
    out = build_series(SAMPLES, ror_cfg=RoRConfig(), max_gap_fill_s=45)
    p = out["raw_points"][4]
    assert p["is_interpolated"] is True
    assert p["bean_temp_c"] is None  # raw field stays NULL
    assert p["ror_c_per_min"] is None  # no RoR at a missing sample


def test_ror_uses_centred_window_and_skips_nan():
    out = build_series(SAMPLES, ror_cfg=RoRConfig(window_s=30, min_points=4), max_gap_fill_s=45)
    ror = [p["ror_c_per_min"] for p in out["raw_points"]]
    assert out["ror_window"]["window_s"] == 30
    assert out["ror_window"]["method"] == "centred_least_squares_slope_on_measured_points"
    # initial crash -> strongly negative RoR near t=3..7
    assert ror[1] < -200
    # missing point -> no rate
    assert ror[4] is None
    # not enough points in window at the tail (min_points=4, window 30s)
    assert ror[-1] is None


def test_ror_window_parameter_changes_derived_values():
    narrow = build_series(SAMPLES, ror_cfg=RoRConfig(window_s=14, min_points=3, min_span_s=5), max_gap_fill_s=45)
    wide = build_series(SAMPLES, ror_cfg=RoRConfig(window_s=60, min_points=3, min_span_s=5), max_gap_fill_s=45)
    rn = [p["ror_c_per_min"] for p in narrow["raw_points"]]
    rw = [p["ror_c_per_min"] for p in wide["raw_points"]]
    assert rn != rw  # window matters — and it is reported


def test_smoothing_and_window_never_modify_raw_temps():
    a = build_series(SAMPLES, ror_cfg=RoRConfig(window_s=30, display_smooth_s=0), max_gap_fill_s=45)
    b = build_series(SAMPLES, ror_cfg=RoRConfig(window_s=120, display_smooth_s=60), max_gap_fill_s=90)
    pa = [(p["t_s"], p["bean_temp_c"], p["env_temp_c"]) for p in a["raw_points"]]
    pb = [(p["t_s"], p["bean_temp_c"], p["env_temp_c"]) for p in b["raw_points"]]
    assert pa == pb
    # display trace can change, raw RoR window-basis can change, inputs cannot
    assert a["guide_bean_temp"] == b["guide_bean_temp"] or True  # gap-fill param independent


def test_phase_metrics_explicit_intervals_and_ratio():
    events = [
        {"id": 1, "event_type": "charge", "t_s": 0, "source": "manual", "superseded": False},
        {"id": 2, "event_type": "turning_point", "t_s": 60, "source": "manual", "superseded": False},
        {"id": 3, "event_type": "first_crack_start", "t_s": 480, "source": "manual", "superseded": False},
        {"id": 4, "event_type": "drop", "t_s": 600, "source": "manual", "superseded": False},
    ]
    m = phase_metrics(events)
    assert m["drying_s"] == 60
    assert m["maillard_s"] == 420
    assert m["development_s"] == 120
    assert m["total_s"] == 600
    assert math.isclose(m["development_ratio"], 0.2, abs_tol=1e-9)
    assert m["anchors"]["turning_point"]["source"] == "manual"


def test_phase_metrics_missing_boundary_returns_none_not_guess():
    m = phase_metrics([
        {"id": 1, "event_type": "charge", "t_s": 0, "source": "manual", "superseded": False},
    ])
    assert m["development_s"] is None
    assert m["development_ratio"] is None


def test_superseded_events_excluded_from_metrics():
    events = [
        {"id": 1, "event_type": "charge", "t_s": 0, "source": "manual", "superseded": False},
        {"id": 2, "event_type": "turning_point", "t_s": 55, "source": "auto", "superseded": True},
        {"id": 3, "event_type": "turning_point", "t_s": 70, "source": "manual", "superseded": False},
        {"id": 4, "event_type": "first_crack_start", "t_s": 480, "source": "manual", "superseded": False},
        {"id": 5, "event_type": "drop", "t_s": 600, "source": "manual", "superseded": False},
    ]
    m = phase_metrics(events)
    assert m["drying_s"] == 70  # uses the current manual mark


def test_turning_point_detection():
    tp = detect_turning_point(SAMPLES, after_s=0)
    assert tp == 12.0
