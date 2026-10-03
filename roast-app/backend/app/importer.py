"""Offline observation-package import pipeline.

An observation package is a JSON file the operator records *away from the
bench* (a hand logger, a field laptop, a USB stick) and brings back to the
workstation.  Nothing in this module — or anywhere in the app — uploads
anywhere: the browser reads the local file and POSTs its bytes to this same
offline backend.

Lifecycle:

    raw bytes
      -> parse/validate as ONE package      (Rejected -> ledger row, nothing else)
      -> content digest + summary check
      -> conflict analysis against stored current samples/events
      -> pending_review ledger row (idempotent on package_id + digest)
      -> operator previews and adjudicates every conflict
      -> one-shot atomic apply (no partial curves possible)

Package format v1 (see README for the canonical form)::

    {
      "format_version": 1,
      "package_id": "obs-2026-09-28-field-07",
      "generated_at": "2026-09-28T10:15:00",
      "batch": {"name": "...", "roaster": ..., "charge_at": ..., ...},
      "samples": [{"t_s": float >= 0, "bean_temp_c": number|null,
                   "env_temp_c": number|null, "sampled_at": iso8601|null}, ...],
      "events":  [{"event_type": str, "t_s": float >= 0, ...}, ...],
      "summary": {"n_samples": n, "n_events": n, "t_first_s": ...,
                  "t_last_s": n, "n_missing_bean": n, "n_missing_env": n,
                  "sha256": "<hexdigest over canonical content>"}
    }

Design guarantees
-----------------
* Whole-package validation: any malformed element rejects the ENTIRE package.
  Validation runs before any ledger state that affects data is written; the
  apply itself is one transaction, so a half-written curve is impossible.
* Idempotent delivery: an exact retry (same package_id AND same digest)
  returns the original import record.  Same package_id, different digest is a
  refused redelivery (``package_id_conflict``), never silently overwritten.
* Out-of-order arrival is harmless: merging is computed against *current*
  stored rows at both preview and apply time, not against arrival order.
* Stored rows keep source + package_id provenance.  Events continue to obey
  append-only/supersede semantics; same-timestamp conflicting readings keep
  both rows and the loser is marked superseded after adjudication.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .analysis import RoRConfig, build_series, current_events, phase_metrics
from .config import MAX_GAP_FILL_S
from .models import Batch, Event, ObservationImport, Sample

FORMAT_VERSION = 1
EVENT_TYPES = {
    "charge",
    "turning_point",
    "first_crack_start",
    "first_crack_end",
    "drop",
    "damper_change",
    "custom",
}
# Types for which only one CURRENT event may exist per batch.
SINGLE_CURRENT_TYPES = EVENT_TYPES - {"damper_change", "custom"}
# Required chronological order of the roast-anchor events that are present.
EVENT_ORDER = ("charge", "turning_point", "first_crack_start", "first_crack_end", "drop")

SAMPLE_KEYS = ("t_s", "bean_temp_c", "env_temp_c", "sampled_at")
EVENT_KEYS = (
    "event_type",
    "t_s",
    "label",
    "source",
    "created_by",
    "value_num",
    "note",
)

VALID_DECISIONS = {"keep_existing", "use_incoming", "supersede"}


class PackageError(ValueError):
    """Whole-package rejection with an auditable code and human detail."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# canonical form / digest
# ---------------------------------------------------------------------------

def _canonical_value(v: Any) -> Any:
    """Reduce parsed JSON to the canonical form used for digesting.

    Floats round-trip through repr (JSON is already parsed, so this is stable
    for values that originated from JSON); None/null is preserved; unknown
    keys outside the documented schema are dropped by callers.
    """
    if isinstance(v, dict):
        return {k: _canonical_value(v[k]) for k in sorted(v)}
    if isinstance(v, list):
        return [_canonical_value(x) for x in v]
    if isinstance(v, float):
        # Normalise -0.0 -> 0.0 so digesting is unaffected by textual noise.
        return 0.0 if v == 0 else v
    return v


def canonical_content(package: dict[str, Any]) -> dict[str, Any]:
    """Documented content over which the package digest is computed.

    Everything except the digest-bearing ``summary.sha256`` field itself,
    restricted to documented keys so an extra UI annotation cannot change the
    reading set identity.
    """
    samples = [
        {
            k: sample.get(k)
            for k in SAMPLE_KEYS
        }
        for sample in package["samples"]
    ]
    events = [
        {
            k: event.get(k)
            for k in EVENT_KEYS
        }
        for event in package["events"]
    ]
    batch = package.get("batch") or {}
    return _canonical_value({
        "format_version": package["format_version"],
        "package_id": package["package_id"],
        "generated_at": package.get("generated_at"),
        "batch": {k: batch.get(k) for k in sorted(batch)},
        "samples": samples,
        "events": events,
    })


def compute_digest(package: dict[str, Any]) -> str:
    blob = json.dumps(
        canonical_content(package),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# parsing & whole-package validation
# ---------------------------------------------------------------------------

@dataclass
class ValidPackage:
    package_id: str
    generated_at: datetime | None
    batch: dict[str, Any]
    samples: list[dict[str, Any]]
    events: list[dict[str, Any]]
    summary: dict[str, Any]
    digest: str


def _parse_datetime(value: Any, *, where: str, required: bool = False) -> datetime | None:
    if value is None:
        if required:
            raise PackageError("invalid_timestamp", f"{where}: 时间戳缺失")
        return None
    if not isinstance(value, str):
        raise PackageError("invalid_timestamp", f"{where}: 时间戳必须是 ISO 8601 字符串")
    text = value.strip()
    # datetime.fromisoformat accepts a trailing Z in 3.11; normalise anyway.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise PackageError(
            "invalid_timestamp", f"{where}: 非法时间 {value!r}（需 ISO 8601）"
        )


def _finite_temp(value: Any, *, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PackageError("invalid_sample", f"{where}: 温度必须是数字或 null")
    fv = float(value)
    if not math.isfinite(fv):
        raise PackageError("invalid_sample", f"{where}: 温度必须有限，得到 {value!r}")
    if fv < -273.15 or fv > 1000.0:
        raise PackageError("invalid_sample", f"{where}: 温度超出物理范围：{fv}")
    return fv


def _finite_nonneg(value: Any, *, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(value, str):
        raise PackageError("invalid_timestamp", f"{where}: 时间必须是非负数字，得到 {value!r}")
    fv = float(value)
    if not math.isfinite(fv) or fv < 0:
        raise PackageError(
            "invalid_timestamp", f"{where}: 时间必须是有限非负数，得到 {value!r}"
        )
    return fv


def parse_package(raw: Any) -> ValidPackage:
    """Validate raw decoded JSON as a whole.  Raises PackageError on the first
    problem — callers turn that into a REJECTED ledger row and touch nothing
    else."""
    if not isinstance(raw, dict):
        raise PackageError("parse_error", "观察包必须是 JSON 对象")

    version = raw.get("format_version")
    if version != FORMAT_VERSION:
        if version is None:
            raise PackageError("invalid_format", "缺少 format_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise PackageError("invalid_format", f"format_version 必须是整数，得到 {version!r}")
        raise PackageError(
            "unsupported_version",
            f"不支持的观察包格式版本 {version}，当前仅支持 {FORMAT_VERSION}",
        )

    pid = raw.get("package_id")
    if not isinstance(pid, str) or not pid.strip():
        raise PackageError("invalid_package_id", "package_id 必须是非空字符串")
    if len(pid) > 120:
        raise PackageError("invalid_package_id", "package_id 过长（>120 字符）")
    pid = pid.strip()

    generated_at = _parse_datetime(raw.get("generated_at"), where="generated_at")

    # ---- batch descriptor -------------------------------------------------
    batch_in = raw.get("batch")
    if not isinstance(batch_in, dict):
        raise PackageError("invalid_batch", "batch 必须是对象（批次描述）")
    name = batch_in.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PackageError("invalid_batch", "batch.name 必须是非空字符串")
    charge_at = _parse_datetime(
        batch_in.get("charge_at"), where="batch.charge_at", required=True
    )
    for key in ("charge_temp_c", "ambient_temp_c"):
        if key not in batch_in or not isinstance(batch_in[key], (int, float)) \
                or isinstance(batch_in[key], bool) or not math.isfinite(batch_in[key]):
            raise PackageError("invalid_batch", f"batch.{key} 必须是有限数字")
    target = batch_in.get("target_drop_temp_c")
    if target is not None:
        if isinstance(target, bool) or not isinstance(target, (int, float)) \
                or not math.isfinite(target):
            raise PackageError("invalid_batch", "batch.target_drop_temp_c 必须是数字或 null")

    # ---- samples ----------------------------------------------------------
    samples_in = raw.get("samples")
    if not isinstance(samples_in, list):
        raise PackageError("invalid_sample", "samples 必须是数组")
    if not samples_in:
        raise PackageError("empty_package", "观察包至少要包含一个原始采样点")

    samples: list[dict[str, Any]] = []
    seen_t: set[float] = set()
    for i, sp in enumerate(samples_in):
        where = f"samples[{i}]"
        if not isinstance(sp, dict):
            raise PackageError("invalid_sample", f"{where}: 必须是对象")
        if "t_s" not in sp:
            raise PackageError("invalid_sample", f"{where}: 缺少 t_s")
        t_s = _finite_nonneg(sp["t_s"], where=f"{where}.t_s")
        bean = _finite_temp(sp.get("bean_temp_c"), where=f"{where}.bean_temp_c")
        env = _finite_temp(sp.get("env_temp_c"), where=f"{where}.env_temp_c")
        sampled_at = _parse_datetime(sp.get("sampled_at"), where=f"{where}.sampled_at")
        if bean is None and env is None:
            raise PackageError("invalid_sample", f"{where}: 豆温与环温不能同时缺测")
        # Same timestamp twice inside one package is an ambiguity the sender
        # must resolve — reject rather than pick one silently.
        tkey = round(t_s, 9)
        if tkey in seen_t:
            raise PackageError(
                "duplicate_sample_time", f"{where}: 包内 t_s={t_s} 重复"
            )
        seen_t.add(tkey)
        samples.append(
            {
                "t_s": t_s,
                "bean_temp_c": bean,
                "env_temp_c": env,
                "sampled_at": sampled_at,
            }
        )
    samples.sort(key=lambda s: s["t_s"])

    # ---- events -----------------------------------------------------------
    events_in = raw.get("events")
    if not isinstance(events_in, list):
        raise PackageError("invalid_event", "events 必须是数组")
    events: list[dict[str, Any]] = []
    single_seen: dict[str, float] = {}
    seen_chain: set[tuple] = set()
    damper_times: dict[float, float] = {}
    for i, ev in enumerate(events_in):
        where = f"events[{i}]"
        if not isinstance(ev, dict):
            raise PackageError("invalid_event", f"{where}: 必须是对象")
        etype = ev.get("event_type")
        if etype not in EVENT_TYPES:
            raise PackageError(
                "invalid_event",
                f"{where}: 未知事件类型 {etype!r}，允许：{sorted(EVENT_TYPES)}",
            )
        if "t_s" not in ev:
            raise PackageError("invalid_event", f"{where}: 缺少 t_s")
        t_s = _finite_nonneg(ev["t_s"], where=f"{where}.t_s")
        if etype == "charge" and t_s != 0.0:
            raise PackageError(
                "invalid_event", f"{where}: charge（下豆）事件的 t_s 必须为 0"
            )
        source = ev.get("source", "manual")
        if source not in ("manual", "auto"):
            raise PackageError(
                "invalid_event", f"{where}: source 只能是 manual 或 auto"
            )
        value_num = ev.get("value_num")
        if value_num is not None:
            if isinstance(value_num, bool) or not isinstance(value_num, (int, float)) \
                    or not math.isfinite(value_num):
                raise PackageError(
                    "invalid_event", f"{where}: value_num 必须是有限数字或 null"
                )
        if etype == "damper_change":
            if value_num is None:
                raise PackageError(
                    "invalid_event", f"{where}: damper_change 必须带 value_num（风门开度 %）"
                )
            if not 0 <= float(value_num) <= 100:
                raise PackageError(
                    "invalid_event", f"{where}: 风门开度必须在 0–100% 之间"
                )
        for key in ("label", "note", "created_by"):
            v = ev.get(key, "")
            if not isinstance(v, str):
                raise PackageError("invalid_event", f"{where}.{key} 必须是字符串")

        row = {
            "event_type": etype,
            "t_s": t_s,
            "label": ev.get("label", ""),
            "source": source,
            "created_by": ev.get("created_by", "operator"),
            "value_num": float(value_num) if value_num is not None else None,
            "note": ev.get("note", ""),
        }
        # A repeated event chain inside one package: identical (type, t, value)
        # duplicates are redundant copies -> invalid, must not double-insert.
        chain_key = (etype, round(t_s, 9), row["value_num"])
        if chain_key in seen_chain:
            raise PackageError(
                "duplicate_event_chain",
                f"{where}: 包内重复事件链 {etype}@{t_s}（value={row['value_num']}）",
            )
        seen_chain.add(chain_key)
        if etype == "damper_change":
            tkey = round(t_s, 9)
            prev_value = damper_times.get(tkey)
            if prev_value is not None:
                # Same timestamp carries an identical mark -> duplicate_event_chain
                # above; a DIFFERENT opening is an intra-package ambiguity that
                # must be resolved by the sender, never guessed at.
                raise PackageError(
                    "duplicate_event_chain",
                    f"{where}: 同一时刻 t_s={t_s} 存在两条不同开度的风门事件"
                    f"（{prev_value}% 与 {row['value_num']}%）",
                )
            damper_times[tkey] = row["value_num"]
        if etype in SINGLE_CURRENT_TYPES:
            if etype in single_seen:
                raise PackageError(
                    "duplicate_event_chain",
                    f"{where}: 同一锚点事件 {etype} 在包内出现多次"
                    f"（{single_seen[etype]} 与 {t_s}）；锚点事件每包至多一条",
                )
            single_seen[etype] = t_s
        events.append(row)

    # Chronological order of the anchor events that are present must hold:
    # when ordered by time, their types must follow the roast sequence.
    present = [(etype, t) for etype, t in single_seen.items()]
    order_index = {etype: i for i, etype in enumerate(EVENT_ORDER)}
    by_time = sorted(present, key=lambda x: x[1])
    for (e1, t1), (e2, t2) in zip(by_time, by_time[1:]):
        if order_index[e2] <= order_index[e1]:
            raise PackageError(
                "invalid_event_order",
                f"事件顺序非法：{e1}@{t1}s 之后出现更早的锚点 {e2}@{t2}s"
                "（烘焙锚点须按 charge→回温点→一爆→出锅 推进）",
            )
    # Every non-charge event should be at/after charge when charge is given.
    if "charge" in single_seen:
        for etype, t in present:
            if etype != "charge" and t < single_seen["charge"]:
                raise PackageError(
                    "invalid_event_order",
                    f"{etype}@{t}s 早于 charge@0s",
                )

    # ---- summary ----------------------------------------------------------
    summary = raw.get("summary")
    if not isinstance(summary, dict):
        raise PackageError("invalid_summary", "summary 必须是对象（内容摘要）")

    # Digest first (digest is defined over content, summary is audited next).
    package_for_digest = {
        "format_version": version,
        "package_id": pid,
        "generated_at": raw.get("generated_at"),
        "batch": batch_in,
        "samples": samples_in,
        "events": events_in,
    }
    digest = compute_digest(package_for_digest)

    n_bean_missing = sum(1 for s in samples if s["bean_temp_c"] is None)
    n_env_missing = sum(1 for s in samples if s["env_temp_c"] is None)
    expected = {
        "n_samples": len(samples),
        "n_events": len(events),
        # Time extent, independent of array/arrival order (samples are sorted
        # by t_s just above).
        "t_first_s": samples[0]["t_s"],
        "t_last_s": samples[-1]["t_s"],
        "n_missing_bean": n_bean_missing,
        "n_missing_env": n_env_missing,
        "sha256": digest,
    }
    for key, want in expected.items():
        if key not in summary:
            raise PackageError("invalid_summary", f"summary 缺少字段 {key}")
        got = summary[key]
        if key == "sha256":
            if not isinstance(got, str) or got.strip().lower() != digest:
                raise PackageError(
                    "digest_mismatch",
                    "summary.sha256 与规范化内容不一致——包可能被改动或摘要损坏",
                )
        elif isinstance(want, float):
            if not isinstance(got, (int, float)) or isinstance(got, bool) \
                    or abs(float(got) - want) > 1e-6:
                raise PackageError(
                    "invalid_summary",
                    f"summary.{key}={got!r} 与实际内容 {want} 不一致",
                )
        else:
            if got != want:
                raise PackageError(
                    "invalid_summary",
                    f"summary.{key}={got!r} 与实际内容 {want} 不一致",
                )

    return ValidPackage(
        package_id=pid,
        generated_at=generated_at,
        batch={
            "name": name.strip(),
            "roaster": batch_in.get("roaster", "offline-field-log"),
            "bean": batch_in.get("bean", ""),
            "charge_at": charge_at,
            "charge_temp_c": float(batch_in["charge_temp_c"]),
            "ambient_temp_c": float(batch_in["ambient_temp_c"]),
            "target_drop_temp_c": float(target) if target is not None else None,
            "note": batch_in.get("note", ""),
        },
        samples=samples,
        events=events,
        summary={k: summary[k] for k in expected},
        digest=digest,
    )


def decode_package(raw_bytes: bytes) -> ValidPackage:
    """Entry point for delivered bytes: JSON-decode then validate.

    Parse failures are whole-package rejections with ``parse_error`` — nothing
    is decoded "best effort"."""
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise PackageError("parse_error", "投递内容必须是文件字节")
    if not raw_bytes.strip():
        raise PackageError("parse_error", "观察包为空文件")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise PackageError("parse_error", "观察包不是 UTF-8 文本（需要 JSON 文件）")
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PackageError(
            "parse_error", f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
        )
    return parse_package(decoded)


# ---------------------------------------------------------------------------
# conflict analysis
# ---------------------------------------------------------------------------

def _temps_equal(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, abs_tol=1e-9)


def _event_equal(a: dict, b: dict) -> bool:
    return (
        a["event_type"] == b["event_type"]
        and math.isclose(a["t_s"], b["t_s"], abs_tol=1e-9)
        and (
            (a["value_num"] is None and b["value_num"] is None)
            or (
                a["value_num"] is not None
                and b["value_num"] is not None
                and math.isclose(a["value_num"], b["value_num"], abs_tol=1e-9)
            )
        )
    )


def _event_sig(ev: dict) -> tuple:
    vn = None if ev["value_num"] is None else round(ev["value_num"], 6)
    return (ev["event_type"], round(ev["t_s"], 9), vn)


@dataclass
class ConflictReport:
    sample_conflicts: list[dict] = field(default_factory=list)
    event_conflicts: list[dict] = field(default_factory=list)
    # Delivery/merge bookkeeping (informational, not requiring adjudication).
    sample_additions: int = 0
    event_additions: int = 0
    duplicate_samples: int = 0
    duplicate_events: int = 0
    superseded_events: int = 0
    batch_exists: bool = False
    batch_id: int | None = None
    batch_name: str = ""

    def to_dict(self) -> dict:
        return {
            "sample_conflicts": self.sample_conflicts,
            "event_conflicts": self.event_conflicts,
            "sample_additions": self.sample_additions,
            "event_additions": self.event_additions,
            "duplicate_samples": self.duplicate_samples,
            "duplicate_events": self.duplicate_events,
            "superseded_events": self.superseded_events,
            "batch_exists": self.batch_exists,
            "batch_id": self.batch_id,
            "batch_name": self.batch_name,
            "n_conflicts": len(self.sample_conflicts) + len(self.event_conflicts),
        }


def analyze_conflicts(session: Session, pkg: ValidPackage) -> ConflictReport:
    """Compare package content against the CURRENT stored rows.

    Recomputed for every preview/apply call, so late/earlier packages arriving
    out of order are always evaluated against what is actually current.
    """
    report = ConflictReport(batch_name=pkg.batch["name"])
    batch = session.scalar(select(Batch).where(Batch.name == pkg.batch["name"]))
    if batch is not None:
        report.batch_exists = True
        report.batch_id = batch.id

    stored_samples: dict[float, Sample] = {}
    stored_sigs: set[tuple] = set()
    if batch is not None:
        for s in session.scalars(
            select(Sample).where(
                Sample.batch_id == batch.id, Sample.superseded.is_(False)
            )
        ):
            stored_samples[round(s.t_s, 9)] = s
        # Own-history replay (the signature carries this package_id).  An
        # identical superseded row from a DIFFERENT package does not match and
        # falls through to adjudicated comparison against the current row.
        for s in session.scalars(
            select(Sample).where(Sample.batch_id == batch.id)
        ):
            stored_sigs.add(
                (
                    round(s.t_s, 9),
                    None if s.bean_temp_c is None else round(s.bean_temp_c, 6),
                    None if s.env_temp_c is None else round(s.env_temp_c, 6),
                    s.import_package_id,
                )
            )

    for sp in pkg.samples:
        tkey = round(sp["t_s"], 9)
        sig = (
            tkey,
            None if sp["bean_temp_c"] is None else round(sp["bean_temp_c"], 6),
            None if sp["env_temp_c"] is None else round(sp["env_temp_c"], 6),
            pkg.package_id,
        )
        if sig in stored_sigs:
            report.duplicate_samples += 1
            continue
        existing = stored_samples.get(tkey)
        if existing is not None:
            if (
                _temps_equal(sp["bean_temp_c"], existing.bean_temp_c)
                and _temps_equal(sp["env_temp_c"], existing.env_temp_c)
            ):
                # Same time, same reading, different source: idempotent merge.
                report.duplicate_samples += 1
                continue
            report.sample_conflicts.append(
                {
                    "ref": f"sample@{sp['t_s']}",
                    "kind": "sample_value_conflict",
                    "t_s": sp["t_s"],
                    "incoming": {
                        "bean_temp_c": sp["bean_temp_c"],
                        "env_temp_c": sp["env_temp_c"],
                    },
                    "existing": {
                        "sample_id": existing.id,
                        "bean_temp_c": existing.bean_temp_c,
                        "env_temp_c": existing.env_temp_c,
                        "source": existing.source,
                        "import_package_id": existing.import_package_id,
                    },
                    # Pending until the operator picks; analysis stays as-is.
                    "requires_decision": True,
                }
            )
        else:
            report.sample_additions += 1

    stored_events: list[dict] = []
    if batch is not None:
        for e in session.scalars(
            select(Event).where(Event.batch_id == batch.id)
        ):
            stored_events.append(
                {
                    "id": e.id,
                    "event_type": e.event_type,
                    "t_s": e.t_s,
                    "value_num": e.value_num,
                    "superseded": e.superseded,
                    "source": e.source,
                    "import_package_id": e.import_package_id,
                }
            )
    current_rows = [e for e in stored_events if not e["superseded"]]
    current_single = {
        e["event_type"]: e
        for e in current_rows
        if e["event_type"] in SINGLE_CURRENT_TYPES
    }
    current_dampers = [e for e in current_rows if e["event_type"] == "damper_change"]

    for ev in pkg.events:
        sig = _event_sig(ev)
        # An identical CURRENT mark is a duplicate observation regardless of
        # which package delivered it (idempotent merge / retries).  A match
        # only against superseded history is NOT a duplicate: the current
        # value differs, so this incoming mark must go through adjudication
        # against the current one.  An exact replay of THIS package's own
        # historical row is still a duplicate (ledger-level retry).
        identical_current = any(_event_sig(se) == sig for se in current_rows)
        identical_own_history = any(
            _event_sig(se) == sig and se["import_package_id"] == pkg.package_id
            for se in stored_events
        )
        if identical_current or identical_own_history:
            report.duplicate_events += 1
            continue

        if ev["event_type"] == "damper_change":
            same_mark = next(
                (d for d in current_dampers if math.isclose(d["t_s"], ev["t_s"], abs_tol=1e-9)),
                None,
            )
            if same_mark is not None:
                if same_mark["value_num"] is not None and ev["value_num"] is not None and math.isclose(
                    same_mark["value_num"], ev["value_num"], abs_tol=1e-9
                ):
                    report.duplicate_events += 1
                else:
                    report.event_conflicts.append(
                        _event_conflict_dict(ev, same_mark, kind="damper_value_conflict")
                    )
            else:
                report.event_additions += 1
            continue

        cur = current_single.get(ev["event_type"])
        if cur is None:
            report.event_additions += 1
            continue
        if math.isclose(cur["t_s"], ev["t_s"], abs_tol=1e-9) and (
            (cur["value_num"] is None and ev["value_num"] is None)
            or (
                cur["value_num"] is not None
                and ev["value_num"] is not None
                and math.isclose(cur["value_num"], ev["value_num"], abs_tol=1e-9)
            )
        ):
            # Same mark, same time — duplicate even if wording differs.
            report.duplicate_events += 1
        else:
            report.event_conflicts.append(
                _event_conflict_dict(ev, cur, kind="anchor_event_conflict")
            )

    report.superseded_events = len(report.event_conflicts)
    return report


def _event_conflict_dict(ev: dict, cur: dict, *, kind: str) -> dict:
    return {
        "ref": f"{ev['event_type']}@{ev['t_s']}",
        "kind": kind,
        "event_type": ev["event_type"],
        "incoming": {"t_s": ev["t_s"], "value_num": ev["value_num"], "source": ev["source"]},
        "existing": {
            "event_id": cur["id"],
            "t_s": cur["t_s"],
            "value_num": cur["value_num"],
            "source": cur["source"],
            "import_package_id": cur["import_package_id"],
        },
        "requires_decision": True,
    }


def _conflict_refs(report: ConflictReport) -> set[str]:
    refs = {c["ref"] for c in report.sample_conflicts}
    refs |= {c["ref"] for c in report.event_conflicts}
    return refs


def validate_resolution_values(
    report: ConflictReport, resolutions: dict[str, str]
) -> None:
    """Every decision must address a real conflict with a legal value.

    Partial adjudication is allowed here — it is saved on the ledger but does
    not make the package applicable (that check is ``require_all_resolved``)."""
    refs = _conflict_refs(report)
    unknown = set(resolutions) - refs
    if unknown:
        raise PackageError(
            "invalid_resolution", f"裁决引用了不存在的冲突：{sorted(unknown)}"
        )
    bad = {r: d for r, d in resolutions.items() if d not in VALID_DECISIONS}
    if bad:
        raise PackageError(
            "invalid_resolution",
            f"裁决取值非法 {bad}，允许 {sorted(VALID_DECISIONS)}",
        )


def require_all_resolved(
    report: ConflictReport, resolutions: dict[str, str]
) -> None:
    """Apply-time gate: not one conflict may remain undecided."""
    missing = _conflict_refs(report) - set(resolutions)
    if missing:
        raise PackageError(
            "unresolved_conflict",
            f"仍有 {len(missing)} 项冲突未裁决：{sorted(missing)}；未裁决不会应用",
        )


def validate_resolutions(report: ConflictReport, resolutions: dict[str, str]) -> None:
    """Both checks together (kept for callers that need the full gate)."""
    validate_resolution_values(report, resolutions)
    require_all_resolved(report, resolutions)


# ---------------------------------------------------------------------------
# preview — pure projection of "what the batch would look like if applied"
# ---------------------------------------------------------------------------

def projected_rows(
    pkg: ValidPackage,
    session: Session,
    batch: Batch | None,
    resolutions: dict[str, str],
) -> tuple[list[dict], list[dict], ConflictReport]:
    """Build the post-apply sample/event row sets WITHOUT writing.

    Used by both preview and apply, guaranteeing they describe exactly the
    same outcome."""
    report = analyze_conflicts(session, pkg)

    # ---- samples ----------------------------------------------------------
    projected: dict[float, dict] = {}
    if batch is not None:
        for s in batch.samples:
            if not s.superseded:
                projected[round(s.t_s, 9)] = {
                    "t_s": s.t_s,
                    "bean_temp_c": s.bean_temp_c,
                    "env_temp_c": s.env_temp_c,
                    "source": s.source,
                    "import_package_id": s.import_package_id,
                    "_state": "existing",
                }
    sample_by_ref = {c["ref"]: c for c in report.sample_conflicts}
    for sp in pkg.samples:
        tkey = round(sp["t_s"], 9)
        ref = f"sample@{sp['t_s']}"
        if tkey in projected and ref not in sample_by_ref:
            continue  # duplicate / identical reading
        incoming_row = {
            "t_s": sp["t_s"],
            "bean_temp_c": sp["bean_temp_c"],
            "env_temp_c": sp["env_temp_c"],
            "source": "observation_package",
            "import_package_id": pkg.package_id,
            "_state": "incoming",
        }
        if ref in sample_by_ref:
            decision = resolutions.get(ref, "keep_existing")
            if decision in ("use_incoming", "supersede"):
                old = projected[tkey]
                old["_state"] = "superseded"
                projected[tkey] = incoming_row
            # keep_existing: leave stored row untouched
        else:
            projected[tkey] = incoming_row
    final_samples = sorted(
        (r for r in projected.values() if r["_state"] != "superseded"),
        key=lambda r: r["t_s"],
    )

    # ---- events -----------------------------------------------------------
    proj_events: list[dict] = []
    if batch is not None:
        proj_events = [
            {
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
                "import_package_id": e.import_package_id,
                "_state": "existing",
                "_temp_id": None,
            }
            for e in sorted(batch.events, key=lambda e: e.id)
        ]

    temp_seq = -1
    event_by_ref = {c["ref"]: c for c in report.event_conflicts}
    for ev in pkg.events:
        ref = f"{ev['event_type']}@{ev['t_s']}"
        # duplicate if an identical CURRENT mark exists, or this package's own
        # historical row is being replayed (a superseded row from ANOTHER
        # package does not silence an incoming mark — it adjudicates against
        # the current value instead).
        own_replay = any(
            r["import_package_id"] == pkg.package_id
            and r["event_type"] == ev["event_type"]
            and math.isclose(r["t_s"], ev["t_s"], abs_tol=1e-9)
            and (r["value_num"] is None) == (ev["value_num"] is None)
            and (
                r["value_num"] is None
                or math.isclose(r["value_num"], ev["value_num"], abs_tol=1e-9)
            )
            for r in proj_events
        )
        exists_as_dup = (
            _event_already_projected(proj_events, ev, current_only=True)
            or own_replay
        )
        if exists_as_dup and ref not in event_by_ref:
            continue
        new_row = {
            "id": None,
            "_temp_id": temp_seq,
            "event_type": ev["event_type"],
            "t_s": ev["t_s"],
            "label": ev["label"],
            "source": ev["source"],
            "created_by": ev["created_by"],
            "value_num": ev["value_num"],
            "note": ev["note"],
            "superseded": False,
            "superseded_by_id": None,
            "import_package_id": pkg.package_id,
            "_state": "incoming",
        }
        temp_seq -= 1
        if ev["event_type"] in SINGLE_CURRENT_TYPES:
            if ref in event_by_ref:
                decision = resolutions.get(ref, "keep_existing")
                if decision in ("use_incoming", "supersede"):
                    for pe in proj_events:
                        if (
                            pe["event_type"] == ev["event_type"]
                            and not pe["superseded"]
                        ):
                            pe["superseded"] = True
                            pe["superseded_by_id"] = new_row["_temp_id"]
                    proj_events.append(new_row)
                # keep_existing -> nothing
            else:
                proj_events.append(new_row)
        else:
            # damper_change / custom: only add when not an exact duplicate.
            if not exists_as_dup:
                proj_events.append(new_row)

    final_events = sorted(proj_events, key=lambda e: (e["t_s"], e["_temp_id"] or e["id"] or 0))
    return final_samples, final_events, report


def _event_already_projected(rows: list[dict], ev: dict, *, current_only: bool = False) -> bool:
    for r in rows:
        if current_only and r.get("superseded"):
            continue
        if r["event_type"] != ev["event_type"] or not math.isclose(
            r["t_s"], ev["t_s"], abs_tol=1e-9
        ):
            continue
        if ev["event_type"] == "damper_change":
            if r["value_num"] is not None and ev["value_num"] is not None and math.isclose(
                r["value_num"], ev["value_num"], abs_tol=1e-9
            ):
                return True
            continue
        if (r["value_num"] is None) == (ev["value_num"] is None):
            return True
    return False


def build_preview(
    pkg: ValidPackage,
    session: Session,
    *,
    resolutions: dict[str, str] | None = None,
    window_s: float = 30.0,
    display_smooth_s: float = 12.0,
    max_gap_fill_s: float = MAX_GAP_FILL_S,
) -> dict:
    """The auditable projection: projected samples/events, the curve those
    samples produce and the metrics those events produce — all without
    writing anything."""
    resolutions = resolutions or {}
    batch = session.scalar(select(Batch).where(Batch.name == pkg.batch["name"]))
    samples, events, report = projected_rows(pkg, session, batch, resolutions)

    series = build_series(
        samples,
        ror_cfg=RoRConfig(window_s=window_s, display_smooth_s=display_smooth_s),
        max_gap_fill_s=max_gap_fill_s,
    )
    # Events for analysis need stable pseudo ids and the keys phase_metrics
    # reads; state/provenance is surfaced alongside.
    analysis_events = []
    for e in events:
        analysis_events.append(
            {
                "id": e["id"] if e["id"] is not None else e["_temp_id"],
                "event_type": e["event_type"],
                "t_s": e["t_s"],
                "value_num": e["value_num"],
                "source": e["source"],
                "superseded": e["superseded"],
                "import_package_id": e["import_package_id"],
            }
        )
    metrics = phase_metrics(analysis_events)
    current = current_events(analysis_events)

    n_conflicts = report.to_dict()["n_conflicts"]
    required_refs = {c["ref"] for c in report.sample_conflicts}
    required_refs |= {c["ref"] for c in report.event_conflicts}
    return {
        "conflicts": report.to_dict(),
        "resolutions": resolutions,
        "all_conflicts_resolved": len(required_refs) == n_conflicts
        and required_refs.issubset(resolutions.keys()),
        "projected": {
            "samples": samples,
            "events": events,
            "series": series,
            "metrics": metrics,
            "current_events": list(current),
        },
    }


# ---------------------------------------------------------------------------
# apply — the only writing path, one transaction at the call site
# ---------------------------------------------------------------------------

def apply_import(session: Session, imp: ObservationImport, pkg: ValidPackage) -> dict:
    """Write the package content exactly as previewed.

    Must be called inside a transaction the caller commits/rolls back.  The
    final conflict analysis + resolution validation is repeated here against
    CURRENT rows, so an intervening package can never be silently clobbered.
    """
    import json as _json

    resolutions = _json.loads(imp.resolutions_json or "{}")
    batch = session.scalar(select(Batch).where(Batch.name == pkg.batch["name"]))
    if batch is None:
        batch = Batch(
            name=pkg.batch["name"],
            roaster=pkg.batch["roaster"],
            bean=pkg.batch["bean"],
            charge_at=pkg.batch["charge_at"],
            charge_temp_c=pkg.batch["charge_temp_c"],
            ambient_temp_c=pkg.batch["ambient_temp_c"],
            target_drop_temp_c=pkg.batch["target_drop_temp_c"],
            note=pkg.batch["note"],
        )
        session.add(batch)
        session.flush()
        # Descriptor can only matter on creation; an existing batch's identity
        # (charge moment etc.) is never silently rewritten by a late package.

    final_samples, final_events, report = projected_rows(pkg, session, batch, resolutions)
    validate_resolutions(report, resolutions)

    n_samples_added = 0
    n_samples_superseded = 0
    n_events_added = 0
    n_events_superseded = 0
    n_duplicates_skipped = report.duplicate_samples + report.duplicate_events

    existing_current_samples = {
        round(s.t_s, 9): s
        for s in session.scalars(
            select(Sample).where(
                Sample.batch_id == batch.id, Sample.superseded.is_(False)
            )
        )
    }
    for sp in pkg.samples:
        tkey = round(sp["t_s"], 9)
        ref = f"sample@{sp['t_s']}"
        existing = existing_current_samples.get(tkey)
        identical = existing is not None and (
            _temps_equal(sp["bean_temp_c"], existing.bean_temp_c)
            and _temps_equal(sp["env_temp_c"], existing.env_temp_c)
        )
        already_from_package = (
            existing is not None and existing.import_package_id == pkg.package_id
            and identical
        )
        if identical or already_from_package:
            continue
        if existing is not None:
            decision = resolutions.get(ref)
            if decision not in ("use_incoming", "supersede"):
                # Should be unreachable after validate_resolutions, but the
                # storage rule must never depend on that.
                raise PackageError(
                    "unresolved_conflict", f"冲突 {ref} 未裁决，拒绝写入"
                )
            # Mark the old reading superseded BEFORE inserting the new one —
            # the partial unique index allows at most one current row per
            # (batch, t_s), so both may never be current in the same flush.
            existing.superseded = True
            n_samples_superseded += 1
        row = Sample(
            batch_id=batch.id,
            t_s=sp["t_s"],
            sampled_at=sp["sampled_at"],
            bean_temp_c=sp["bean_temp_c"],
            env_temp_c=sp["env_temp_c"],
            source="observation_package",
            import_id=imp.id,
            import_package_id=pkg.package_id,
            superseded=False,
        )
        session.add(row)
        session.flush()
        if existing is not None:
            existing.superseded_by_sample_id = row.id
        n_samples_added += 1

    # Events: append-only.  Resolved anchor conflicts supersede; damper/custom
    # marks are only appended when not exact duplicates of stored marks.
    stored_all = list(
        session.scalars(select(Event).where(Event.batch_id == batch.id))
    )
    pending_rows: list[tuple[Event, str | None]] = []
    current_all = [e for e in stored_all if not e.superseded]
    for ev in pkg.events:
        sig = _event_sig(ev)
        # Skip only an identical CURRENT mark or this package's own historical
        # replay — an identical superseded mark from another package still has
        # to be adjudicated against the current value.
        if any(_event_sig(_event_dict(e)) == sig for e in current_all) or any(
            _event_sig(_event_dict(e)) == sig and e.import_package_id == pkg.package_id
            for e in stored_all
        ):
            continue
        row = Event(
            batch_id=batch.id,
            event_type=ev["event_type"],
            t_s=ev["t_s"],
            label=ev["label"],
            source=ev["source"],
            created_by=ev["created_by"],
            value_num=ev["value_num"],
            note=ev["note"],
            import_id=imp.id,
            import_package_id=pkg.package_id,
            superseded=False,
        )
        ref = f"{ev['event_type']}@{ev['t_s']}"
        action = "add"
        if ev["event_type"] in SINGLE_CURRENT_TYPES:
            cur = next(
                (e for e in stored_all if not e.superseded and e.event_type == ev["event_type"]),
                None,
            )
            if cur is not None:
                action = resolutions.get(ref, "keep_existing")
        elif ev["event_type"] == "damper_change":
            same_t = next(
                (
                    e
                    for e in stored_all
                    if not e.superseded
                    and e.event_type == "damper_change"
                    and math.isclose(e.t_s, ev["t_s"], abs_tol=1e-9)
                ),
                None,
            )
            if same_t is not None:
                action = resolutions.get(ref, "keep_existing")
        pending_rows.append((row, action))

    # Supersede links need new row ids: flush additions first, then set links.
    added_rows: list[tuple[Event, str | None, str | None]] = []
    for row, action in pending_rows:
        if action == "keep_existing":
            n_duplicates_skipped += 1
            continue
        ref = f"{row.event_type}@{row.t_s}"
        session.add(row)
        session.flush()
        added_rows.append((row, action, ref))

    for row, action, ref in added_rows:
        n_events_added += 1
        if action in ("use_incoming", "supersede") and row.event_type in SINGLE_CURRENT_TYPES:
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
                n_events_superseded += 1
        elif action in ("use_incoming", "supersede") and row.event_type == "damper_change":
            prev = session.scalars(
                select(Event).where(
                    Event.batch_id == batch.id,
                    Event.event_type == "damper_change",
                    Event.superseded.is_(False),
                    Event.id != row.id,
                    Event.t_s == row.t_s,
                )
            ).all()
            for p in prev:
                p.superseded = True
                p.superseded_by_id = row.id
                n_events_superseded += 1

    imp.batch_id = batch.id
    imp.batch_name = batch.name
    imp.status = "applied"
    imp.applied_at = datetime.utcnow()
    imp.conflict_report_json = _json.dumps(report.to_dict(), ensure_ascii=False)

    session.flush()
    return {
        "batch_id": batch.id,
        "samples_added": n_samples_added,
        "samples_superseded": n_samples_superseded,
        "events_added": n_events_added,
        "events_superseded": n_events_superseded,
        "duplicates_skipped": n_duplicates_skipped,
    }


def _event_dict(e: Event) -> dict:
    return {"event_type": e.event_type, "t_s": e.t_s, "value_num": e.value_num}
