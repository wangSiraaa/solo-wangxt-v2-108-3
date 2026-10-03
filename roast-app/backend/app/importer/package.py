"""Observation-package parsing, canonical digest, and whole-package validation.

A field observation package (format v1) is a JSON document::

    {
      "format_version": 1,
      "package_id": "field-2026-09-21-01",
      "generated_at": "2026-09-21T14:05:00",
      "batch": { name, roaster, bean, charge_at, charge_temp_c, ... },
      "samples": [{ t_s, bean_temp_c|null, env_temp_c|null, sampled_at?, note? }],
      "events":  [{ event_uid, supersedes_uid?, event_type, t_s, ... }],
      "digest":  { "algorithm": "sha256", "sha256": "<hex>", "note"? }
    }

The digest covers a canonical encoding of exactly these fields::

    format_version, package_id, generated_at, batch, samples, events

with ``json.dumps(..., sort_keys=True, separators=(",", ":"),
ensure_ascii=False)`` then ``sha256.hexdigest()``.  The ``digest`` block itself
is deliberately outside the signed portion so a re-delivery can be verified
without self-reference.

Validation is *whole-package*: every structural or semantic finding is
collected.  An invalid package must fail atomically — nothing is applied and
the ledger stores the finding list for audit.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from ..schemas import (
    EVENT_TYPES,
    SUPPORTED_FORMAT_VERSIONS,
    ObservationPackage,
)

# Fields covered by the digest, in a fixed order; everything else (the digest
# block itself, later envelope fields) is excluded.
SIGNED_FIELDS = (
    "format_version",
    "package_id",
    "generated_at",
    "batch",
    "samples",
    "events",
)

# A reading difference smaller than this is treated as the same measurement
# (guards against 208.0999999 vs 208.1 float round-tripping).
VALUE_EPS = 1e-3

# "非法时间顺序" — the roast anchors have to occur in this order.
ANCHOR_ORDER = (
    "charge",
    "turning_point",
    "first_crack_start",
    "first_crack_end",
    "drop",
)


class PackageError(Exception):
    """Aggregated whole-package failure with a stable code and findings."""

    def __init__(self, code: str, findings: list[dict[str, Any]]):
        self.code = code
        self.findings = findings
        super().__init__(f"{code}: {len(findings)} finding(s)")


def canonical_json(raw: dict[str, Any]) -> str:
    """Canonical encoding of the signed portion of a raw package dict."""
    signed = {k: raw.get(k) for k in SIGNED_FIELDS}
    return json.dumps(
        signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def canonical_digest(raw: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()


def _finding(code: str, message: str, **extra: Any) -> dict[str, Any]:
    out = {"code": code, "message": message}
    out.update(extra)
    return out


def validate_package(raw: Any) -> ObservationPackage:
    """Validate an already-decoded JSON value as an observation package.

    Raises :class:`PackageError` with *all* findings whenever the package is
    not fit to enter ``pending_review``.  Nothing here writes anywhere.
    """
    findings: list[dict[str, Any]] = []

    if not isinstance(raw, dict):
        raise PackageError(
            "package_not_object",
            [_finding("package_not_object", "包顶层必须是 JSON 对象")],
        )

    # --- format version (checked first: it selects every later rule) --------
    version = raw.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool):
        findings.append(
            _finding("format_version_invalid", "format_version 必须是整数")
        )
    elif version not in SUPPORTED_FORMAT_VERSIONS:
        findings.append(
            _finding(
                "format_version_unsupported",
                f"不支持的格式版本 {version!r}，仅支持 {SUPPORTED_FORMAT_VERSIONS}",
                format_version=version,
            )
        )

    # --- digest block: present, sha256, and matching the signed bytes --------
    expected_hex: str | None = None
    digest_block = raw.get("digest")
    if not isinstance(digest_block, dict):
        findings.append(_finding("digest_missing", "缺少 digest 摘要块"))
    else:
        algo = digest_block.get("algorithm")
        expected_hex = digest_block.get("sha256")
        if algo != "sha256":
            findings.append(
                _finding("digest_algorithm_unsupported", f"不支持的摘要算法 {algo!r}")
            )
        if not isinstance(expected_hex, str) or not expected_hex:
            findings.append(_finding("digest_missing_sha256", "digest.sha256 缺失"))
        else:
            actual_hex = canonical_digest(raw)
            if actual_hex != expected_hex.lower():
                findings.append(
                    _finding(
                        "digest_mismatch",
                        "内容摘要与包内容不一致：包可能被截断或篡改",
                        expected=expected_hex,
                        actual=actual_hex,
                    )
                )

    # --- schema validation (types, ranges, required fields) -----------------
    try:
        pkg = ObservationPackage.model_validate(raw)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            findings.append(
                _finding(
                    "schema_invalid",
                    f"{loc or '<root>'}: {err['msg']}",
                    location=loc,
                )
            )
        raise PackageError("package_invalid", findings) from None

    # --- semantic validation across rows -------------------------------------
    findings.extend(_semantic_findings(pkg))
    if findings:
        raise PackageError("package_invalid", findings)
    return pkg


def _semantic_findings(pkg: ObservationPackage) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    if not pkg.samples:
        findings.append(_finding("samples_empty", "包必须至少包含一个采样点"))

    # duplicate / out-of-order timestamps inside the package itself ----------
    seen_t: dict[float, int] = {}
    for i, s in enumerate(pkg.samples):
        if s.t_s in seen_t:
            findings.append(
                _finding(
                    "sample_duplicate_t_s",
                    f"包内 t_s={s.t_s} 出现两次（样本索引 {seen_t[s.t_s]} 与 {i}）",
                    t_s=s.t_s,
                    index=i,
                )
            )
        seen_t[s.t_s] = i
        if (s.bean_temp_c is None and s.env_temp_c is None) and not s.note:
            # Both probes missing at a point is legal (long dropout) but the
            # recorder is expected to say so; silence is rejected rather than
            # silently importing an empty row.
            findings.append(
                _finding(
                    "sample_empty_reading",
                    f"样本 t_s={s.t_s} 豆温与环温均为空且没有说明",
                    t_s=s.t_s,
                    index=i,
                )
            )

    # event chain: unique uids, valid supersede links, non-decreasing order ---
    uid_index: dict[str, int] = {}
    for i, e in enumerate(pkg.events):
        if e.event_type not in EVENT_TYPES:
            findings.append(
                _finding(
                    "event_type_unknown",
                    f"事件 {e.event_uid!r} 的类型 {e.event_type!r} 不受支持",
                    event_uid=e.event_uid,
                    index=i,
                )
            )
        if e.event_uid in uid_index:
            findings.append(
                _finding(
                    "event_uid_duplicate",
                    f"事件链中 event_uid={e.event_uid!r} 重复（重复事件链）",
                    event_uid=e.event_uid,
                    index=i,
                )
            )
        else:
            uid_index[e.event_uid] = i
        if e.event_type == "damper_change" and e.value_num is None:
            findings.append(
                _finding(
                    "damper_value_missing",
                    f"风门事件 {e.event_uid!r} 缺少 value_num 开度",
                    event_uid=e.event_uid,
                    index=i,
                )
            )
        if e.value_num is not None and not (0.0 <= e.value_num <= 100.0):
            findings.append(
                _finding(
                    "damper_value_out_of_range",
                    f"事件 {e.event_uid!r} 的风门开度 {e.value_num} 超出 0–100",
                    event_uid=e.event_uid,
                    index=i,
                )
            )

    for i, e in enumerate(pkg.events):
        if e.supersedes_uid is not None:
            j = uid_index.get(e.supersedes_uid)
            if j is None:
                findings.append(
                    _finding(
                        "supersede_target_missing",
                        f"事件 {e.event_uid!r} 声明取代 {e.supersedes_uid!r}，"
                        "但链中不存在该事件",
                        event_uid=e.event_uid,
                        supersedes_uid=e.supersedes_uid,
                    )
                )
            else:
                prev = pkg.events[j]
                if prev.event_type != e.event_type:
                    findings.append(
                        _finding(
                            "supersede_type_mismatch",
                            f"事件 {e.event_uid!r}({e.event_type}) 不能取代不同类型的 "
                            f"{e.supersedes_uid!r}({prev.event_type})",
                            event_uid=e.event_uid,
                            supersedes_uid=e.supersedes_uid,
                        )
                    )
                if e.t_s + 1e-9 < prev.t_s:
                    findings.append(
                        _finding(
                            "supersede_time_before_original",
                            f"事件 {e.event_uid!r}(t={e.t_s}) 早于被取代事件 "
                            f"{prev.event_uid!r}(t={prev.t_s})：无效事件顺序",
                            event_uid=e.event_uid,
                            supersedes_uid=e.supersedes_uid,
                        )
                    )

    # the recorded chain is only "out of order" when a *correction* points
    # backwards in time; independent events (e.g. a late-noted damper change)
    # may legitimately be logged after later anchors and are merged by t_s.
    # (supersede ordering is checked in the loop above.)

    # physical roast order across the *current* anchors present --------------
    first_t: dict[str, float] = {}
    for e in pkg.events:
        first_t.setdefault(e.event_type, e.t_s)
    seq = [(kind, first_t[kind]) for kind in ANCHOR_ORDER if kind in first_t]
    for (ka, ta), (kb, tb) in zip(seq, seq[1:]):
        if tb + 1e-9 < ta:
            findings.append(
                _finding(
                    "event_phase_order_invalid",
                    f"非法时间顺序：{kb}(t={tb}) 早于 {ka}(t={ta})",
                    earlier=kb,
                    later=ka,
                )
            )

    return findings
