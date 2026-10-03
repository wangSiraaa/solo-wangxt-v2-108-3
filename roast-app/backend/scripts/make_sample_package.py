"""Build a sample offline observation package (format v1) on stdout.

The package exercises uneven sampling, NULL probe readings, a manual event
chain (including an in-package supersede), and carries a valid digest.

Usage:
    python scripts/make_sample_package.py > /tmp/field-pkg.json
    # then choose that file in the "离线观察包导入" panel (nothing uploaded).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.importer.package import canonical_digest  # noqa: E402


def build(package_id: str = "field-2026-0921-01") -> dict:
    raw = {
        "format_version": 1,
        "package_id": package_id,
        "generated_at": "2026-09-21T14:05:00",
        "batch": {
            "name": "FIELD-2026-0921-01",
            "roaster": "field-tr-1kg (offline recorder)",
            "bean": "Ethiopia Yirgacheffe",
            "charge_at": "2026-09-21T13:00:00",
            "charge_temp_c": 180.0,
            "ambient_temp_c": 22.5,
            "target_drop_temp_c": 205.0,
            "note": "现场离线记录：不均采样 + 两处探针缺测 + 人工事件",
        },
        "samples": [
            {"t_s": 0.0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
            {"t_s": 2.4, "bean_temp_c": 166.3, "env_temp_c": 190.9},
            {"t_s": 6.1, "bean_temp_c": 155.0, "env_temp_c": 191.7},
            {"t_s": 9.0, "bean_temp_c": None, "env_temp_c": 192.3, "note": "豆温探针短暂失联"},
            {"t_s": 14.8, "bean_temp_c": 146.0, "env_temp_c": 193.2},
            {"t_s": 21.6, "bean_temp_c": 141.8, "env_temp_c": 194.0},
            {"t_s": 30.2, "bean_temp_c": 144.0, "env_temp_c": 195.1},
            {"t_s": 60.0, "bean_temp_c": 150.6, "env_temp_c": 197.0},
            {"t_s": 120.0, "bean_temp_c": 168.1, "env_temp_c": 201.0},
            {"t_s": 240.0, "bean_temp_c": 185.0, "env_temp_c": None, "note": "环温探针失联"},
            {"t_s": 300.0, "bean_temp_c": 188.3, "env_temp_c": 210.0},
            {"t_s": 420.0, "bean_temp_c": 198.1, "env_temp_c": 213.0},
            {"t_s": 540.0, "bean_temp_c": 205.2, "env_temp_c": 216.0},
        ],
        "events": [
            {"event_uid": "ch-1", "event_type": "charge", "t_s": 0.0,
             "source": "manual", "created_by": "lin", "label": "下豆"},
            {"event_uid": "tp-1", "event_type": "turning_point", "t_s": 20.0,
             "source": "manual", "created_by": "lin", "label": "初判回温点"},
            {"event_uid": "tp-2", "event_type": "turning_point", "t_s": 21.6,
             "source": "manual", "created_by": "lin", "supersedes_uid": "tp-1",
             "label": "复核回温点（取代 tp-1）"},
            {"event_uid": "dp-1", "event_type": "damper_change", "t_s": 300.0,
             "source": "manual", "created_by": "lin", "value_num": 35.0,
             "label": "风门 70 -> 35"},
            {"event_uid": "fc-1", "event_type": "first_crack_start", "t_s": 420.0,
             "source": "manual", "created_by": "lin", "label": "一爆开始"},
            {"event_uid": "dr-1", "event_type": "drop", "t_s": 540.0,
             "source": "manual", "created_by": "lin", "label": "出锅"},
        ],
    }
    raw["digest"] = {
        "algorithm": "sha256",
        "sha256": canonical_digest(raw),
        "note": "sha256 over canonical JSON of the signed fields",
    }
    return raw


if __name__ == "__main__":
    pid = sys.argv[1] if len(sys.argv) > 1 else "field-2026-0921-01"
    json.dump(build(pid), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
