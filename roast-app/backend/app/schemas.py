"""Pydantic request/response schemas."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# existing API
# ---------------------------------------------------------------------------


class SampleIn(BaseModel):
    t_s: float
    bean_temp_c: float | None = None
    env_temp_c: float | None = None
    sampled_at: datetime | None = None


class EventIn(BaseModel):
    event_type: str
    t_s: float = Field(ge=0)
    label: str = ""
    source: str = "manual"
    created_by: str = "operator"
    value_num: float | None = None
    note: str = ""


class EventOut(EventIn):
    id: int
    batch_id: int
    superseded: bool
    superseded_by_id: int | None = None
    created_at: datetime
    event_uid: str | None = None
    source_package_id: str | None = None

    class Config:
        from_attributes = True


class BatchMeta(BaseModel):
    id: int
    name: str
    roaster: str
    bean: str
    charge_at: datetime
    charge_temp_c: float
    ambient_temp_c: float
    target_drop_temp_c: float | None = None
    note: str

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# offline observation packages (v1)
# ---------------------------------------------------------------------------

SUPPORTED_FORMAT_VERSIONS = (1,)
EVENT_TYPES = (
    "charge",
    "turning_point",
    "first_crack_start",
    "first_crack_end",
    "drop",
    "damper_change",
    "custom",
)
EVENT_SOURCES = ("manual", "auto", "imported")


def _finite_or_none(v: float | None) -> float | None:
    if v is None:
        return None
    if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise ValueError("温度必须是有限数值")
    return float(v)


class PkgDigest(BaseModel):
    """Content digest covering the signed portion of the package."""

    algorithm: Literal["sha256"]
    # sha256 over the canonical JSON of
    # {"format_version", "package_id", "generated_at", "batch",
    #  "samples", "events"} — sort_keys=True, separators=(",", ":"),
    # ensure_ascii=False.  See app.imports.package.canonical_digest.
    sha256: str = Field(min_length=1)
    note: str = ""


class PkgBatch(BaseModel):
    """Batch context carried by the recorder; used when creating a new batch."""

    name: str = Field(min_length=1, max=120)
    roaster: str = "offline-field-recorder"
    bean: str = ""
    charge_at: datetime
    charge_temp_c: float
    ambient_temp_c: float = 22.0
    target_drop_temp_c: float | None = None
    note: str = ""

    @field_validator("charge_temp_c", "ambient_temp_c", "target_drop_temp_c")
    @classmethod
    def _temps_finite(cls, v: float | None) -> float | None:
        return _finite_or_none(v)


class PkgSample(BaseModel):
    """One measured reading.  NULL temperatures preserve a probe dropout;
    timestamps are seconds since charge and must be finite and >= 0."""

    t_s: float = Field(ge=0)
    bean_temp_c: float | None = None
    env_temp_c: float | None = None
    sampled_at: datetime | None = None
    note: str = ""

    @field_validator("t_s")
    @classmethod
    def _t_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("t_s 必须是有限数值")
        return float(v)

    @field_validator("bean_temp_c", "env_temp_c")
    @classmethod
    def _temps_finite(cls, v: float | None) -> float | None:
        return _finite_or_none(v)


class PkgEvent(BaseModel):
    """One recorded event.  ``event_uid`` is stable inside the package and
    ``supersedes_uid`` links a correction to the event it replaces."""

    event_uid: str = Field(min_length=1, max=120)
    supersedes_uid: str | None = None
    event_type: str
    t_s: float = Field(ge=0)
    label: str = ""
    source: Literal["manual", "auto"] = "manual"
    created_by: str = "field-operator"
    value_num: float | None = None
    note: str = ""

    @field_validator("t_s")
    @classmethod
    def _t_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("t_s 必须是有限数值")
        return float(v)

    @field_validator("value_num")
    @classmethod
    def _value_finite(cls, v: float | None) -> float | None:
        return _finite_or_none(v)


class ObservationPackage(BaseModel):
    """The parsed offline observation package (format v1)."""

    format_version: int
    package_id: str = Field(min_length=1, max=64)
    generated_at: datetime
    batch: PkgBatch
    samples: list[PkgSample] = Field(default_factory=list)
    events: list[PkgEvent] = Field(default_factory=list)
    digest: PkgDigest


# ---------------------------------------------------------------------------
# import API bodies / results
# ---------------------------------------------------------------------------


class ImportRequest(BaseModel):
    """The package is delivered as already-parsed JSON text.  The browser
    reads it from a locally chosen file; nothing is ever uploaded anywhere
    beyond this same-origin local API."""

    package: dict[str, Any]
    target_batch_id: int | None = None
    created_by: str = "operator"


class ConflictResolutionIn(BaseModel):
    # conflict id -> decision
    resolutions: dict[int, Literal["keep_existing", "use_incoming"]]
    resolved_by: str = "operator"
