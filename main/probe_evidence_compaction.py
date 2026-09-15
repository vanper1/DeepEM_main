from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

KIND_LABELS = {
    "wifi_ap": "WiFi 热点",
    "wifi_client": "WiFi 客户端",
    "bluetooth": "蓝牙设备",
}

DEFAULT_PROBE_LLM_MAX_CHARS = 18000


def resolve_probe_llm_max_chars(value: int | None = None) -> int:
    """Resolve a conservative input budget for models with about a 10k-token context."""
    raw = value if value is not None else os.getenv("DEEPEM_PROBE_LLM_MAX_CHARS", str(DEFAULT_PROBE_LLM_MAX_CHARS))
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = DEFAULT_PROBE_LLM_MAX_CHARS
    return max(8000, min(28000, parsed))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round_number(value: Any, digits: int = 2) -> float | int | None:
    number = _number(value)
    if number is None:
        return None
    rounded = round(number, digits)
    return int(rounded) if rounded.is_integer() else rounded


def _iso_timestamp(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return text


def _counter(rows: Iterable[dict[str, Any]], key: str, *, limit: int = 12) -> list[dict[str, Any]]:
    counts = Counter(str(row.get(key) or "").strip() for row in rows)
    counts.pop("", None)
    return [{"value": value, "count": count} for value, count in counts.most_common(limit)]


def _numeric_stats(rows: Iterable[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [number for row in rows if (number := _number(row.get(key))) is not None]
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None}
    return {
        "count": len(values),
        "min": _round_number(min(values)),
        "median": _round_number(statistics.median(values)),
        "max": _round_number(max(values)),
    }


def _is_locally_administered_mac(value: Any) -> bool:
    try:
        first_octet = int(str(value or "").split(":", 1)[0], 16)
    except (TypeError, ValueError):
        return False
    return bool(first_octet & 0x02)


def _compact_record(row: dict[str, Any], *, source: str) -> dict[str, Any]:
    mapping = {
        "probe_id": "p",
        "kind": "k",
        "mac": "m",
        "name": "n",
        "first_seen": "fs",
        "last_seen": "ls",
        "rssi_latest": "r",
        "rssi_min": "rn",
        "rssi_max": "rx",
        "distance": "d",
        "channel": "ch",
        "vendor": "v",
        "device_type": "dt",
        "ssid": "s",
        "bssid": "b",
        "connected_ssid": "cs",
        "connected_bssid": "cb",
        "encryption": "e",
        "hidden": "h",
        "observation_count": "oc",
        "total_observation_count": "toc",
        "session_count": "sc",
        "device_duration_seconds": "du",
        "platform_duration_seconds": "pu",
    }
    compact: dict[str, Any] = {"src": "o" if source == "observation" else "t"}
    for source_key, target_key in mapping.items():
        value = row.get(source_key)
        if value in (None, "", [], {}):
            continue
        if source_key in {"rssi_latest", "rssi_min", "rssi_max", "distance", "device_duration_seconds", "platform_duration_seconds"}:
            value = _round_number(value)
        elif source_key in {"first_seen", "last_seen"}:
            value = _iso_timestamp(value)
        compact[target_key] = value
    return compact


def _priority(row: dict[str, Any], *, observation: bool) -> tuple[Any, ...]:
    rssi = _number(row.get("rssi_latest"))
    distance = _number(row.get("distance"))
    last_seen = str(row.get("last_seen") or "")
    flags = sum(
        [
            bool(row.get("hidden")),
            str(row.get("encryption") or "").upper() == "OPEN",
            _is_locally_administered_mac(row.get("mac")),
            bool(row.get("connected_ssid") or row.get("connected_bssid")),
        ]
    )
    return (
        0 if observation else 1,
        -flags,
        -(rssi if rssi is not None else -999),
        distance if distance is not None else 999999,
        "".join(chr(0x10FFFF - ord(char)) for char in last_seen[:32]),
        str(row.get("mac") or ""),
    )


def _kind_statistics(targets: list[dict[str, Any]], observations: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for kind in KIND_LABELS:
        kind_targets = [row for row in targets if row.get("kind") == kind]
        kind_observations = [row for row in observations if row.get("kind") == kind]
        result[kind] = {
            "label": KIND_LABELS[kind],
            "targets": len(kind_targets),
            "current_observations": len(kind_observations),
            "named_targets": sum(1 for row in kind_targets if str(row.get("name") or row.get("ssid") or "").strip() not in {"", "--"}),
            "hidden_targets": sum(1 for row in kind_targets if bool(row.get("hidden"))),
            "open_networks": sum(1 for row in kind_targets if str(row.get("encryption") or "").upper() == "OPEN"),
            "locally_administered_macs": sum(1 for row in kind_targets if _is_locally_administered_mac(row.get("mac"))),
            "rssi_latest": _numeric_stats(kind_observations or kind_targets, "rssi_latest"),
            "distance_m": _numeric_stats(kind_observations or kind_targets, "distance"),
            "top_channels": _counter(kind_observations or kind_targets, "channel"),
            "top_vendors": _counter(kind_targets, "vendor"),
            "top_encryptions": _counter(kind_targets, "encryption"),
            "top_device_types": _counter(kind_targets, "device_type"),
            "top_ssids": _counter(kind_targets, "ssid"),
            "top_connected_ssids": _counter(kind_observations, "connected_ssid"),
            "latest_seen": max((str(row.get("last_seen") or "") for row in kind_targets + kind_observations), default="") or None,
        }
    return result


def _chunk_manifest(rows: list[dict[str, Any]], *, section: str, chunk_size: int = 100) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for index in range(0, len(rows), chunk_size):
        chunk = rows[index:index + chunk_size]
        chunks.append(
            {
                "id": f"{section}-{index // chunk_size + 1:03d}",
                "start": index,
                "end": index + len(chunk) - 1,
                "rows": len(chunk),
                "sha256": _sha256(chunk),
                "first_key": str((chunk[0] if chunk else {}).get("id") or (chunk[0] if chunk else {}).get("mac") or ""),
                "last_key": str((chunk[-1] if chunk else {}).get("id") or (chunk[-1] if chunk else {}).get("mac") or ""),
            }
        )
    return chunks


def build_probe_evidence_pack(result: dict[str, Any], *, max_chars: int | None = None) -> dict[str, Any]:
    """Build a bounded LLM dossier while preserving the full raw evidence and integrity hashes.

    The returned ``llm_evidence`` is deliberately bounded. Every raw row remains in the
    caller-owned result and is covered by section/chunk hashes, so the dossier is auditable
    without sending an unbounded JSON document to the model.
    """
    max_chars = resolve_probe_llm_max_chars(max_chars)
    devices = list(result.get("devices") or [])
    targets = list(result.get("targets") or [])
    observations = list(result.get("observations") or [])
    counts = dict(result.get("counts") or {})
    statistics_by_kind = _kind_statistics(targets, observations)
    manifest = {
        "algorithm": "sha256-canonical-json-v1",
        "full_result_sha256": _sha256(result),
        "sections": {
            "devices": {"rows": len(devices), "sha256": _sha256(devices)},
            "targets": {"rows": len(targets), "sha256": _sha256(targets), "chunks": _chunk_manifest(targets, section="targets")},
            "observations": {"rows": len(observations), "sha256": _sha256(observations), "chunks": _chunk_manifest(observations, section="observations")},
            "errors": {"rows": len(result.get("errors") or []), "sha256": _sha256(result.get("errors") or [])},
        },
    }

    ranked_observations = sorted(observations, key=lambda row: _priority(row, observation=True))
    ranked_targets = sorted(targets, key=lambda row: _priority(row, observation=False))
    strongest = sorted(
        observations,
        key=lambda row: -(_number(row.get("rssi_latest")) if _number(row.get("rssi_latest")) is not None else -999),
    )[:15]
    nearest = sorted(
        [row for row in observations if _number(row.get("distance")) is not None],
        key=lambda row: _number(row.get("distance")) or 0,
    )[:15]
    recent = sorted(observations, key=lambda row: str(row.get("last_seen") or ""), reverse=True)[:15]

    llm_evidence: dict[str, Any] = {
        "format": "deepem-probe-evidence-pack-v1",
        "instructions": [
            "counts/statistics are computed deterministically from every raw row",
            "compact record keys: src=o observation/t target; p probe; k kind; m mac; n name; fs/ls first/last seen; r/rn/rx RSSI; d distance; ch channel; v vendor; dt device type; s/b SSID/BSSID; cs/cb connected SSID/BSSID; e encryption; h hidden; oc/toc observation counts; sc sessions",
            "raw evidence is retained unchanged and covered by SHA-256 manifests; do not infer identity, exact location, or causality",
        ],
        "source": result.get("source"),
        "probe_ids": list(result.get("probe_ids") or []),
        "active_minutes": result.get("active_minutes"),
        "counts": counts,
        "partial_failure": bool(result.get("partial_failure")),
        "errors": list(result.get("errors") or []),
        "integrity": {
            "full_result_sha256": manifest["full_result_sha256"],
            "section_hashes": {key: value["sha256"] for key, value in manifest["sections"].items()},
            "chunk_counts": {
                "targets": len(manifest["sections"]["targets"]["chunks"]),
                "observations": len(manifest["sections"]["observations"]["chunks"]),
            },
        },
        "statistics_by_kind": statistics_by_kind,
        "devices": devices,
        "priority_records": [],
        "records": [],
        "coverage": {
            "observations_total": len(observations),
            "observations_included": 0,
            "targets_total": len(targets),
            "targets_included": 0,
            "omitted_records_are_preserved_in_raw_artifact": True,
        },
    }


    priority_records: list[dict[str, Any]] = []
    seen_priority: set[tuple[str, str, str]] = set()
    for reason, rows in (("strongest", strongest[:8]), ("nearest", nearest[:8]), ("recent", recent[:8])):
        for row in rows:
            key = (str(row.get("probe_id") or ""), str(row.get("kind") or ""), str(row.get("mac") or ""))
            if key in seen_priority:
                continue
            seen_priority.add(key)
            compact = _compact_record(row, source="observation")
            compact["why"] = reason
            priority_records.append(compact)
            if len(priority_records) >= 15:
                break
        if len(priority_records) >= 15:
            break
    llm_evidence["priority_records"] = priority_records

    # Reserve space for JSON closings and coverage counters. Observations are included first
    # because they represent the current active window; remaining budget is filled with targets.
    base_chars = len(_canonical_json(llm_evidence))
    budget_remaining = max(0, max_chars - base_chars - 512)
    source_budgets = {
        "observation": int(budget_remaining * 0.64),
        "target": budget_remaining - int(budget_remaining * 0.64),
    }
    included_observations = 0
    included_targets = 0
    compact_records: list[dict[str, Any]] = []
    for source, rows in (("observation", ranked_observations), ("target", ranked_targets)):
        for row in rows:
            record = _compact_record(row, source=source)
            record_chars = len(_canonical_json(record)) + 1
            if record_chars > source_budgets[source]:
                continue
            compact_records.append(record)
            source_budgets[source] -= record_chars
            if source == "observation":
                included_observations += 1
            else:
                included_targets += 1
    llm_evidence["records"] = compact_records
    llm_evidence["coverage"].update(
        {
            "observations_included": included_observations,
            "targets_included": included_targets,
            "observations_omitted": len(observations) - included_observations,
            "targets_omitted": len(targets) - included_targets,
        }
    )
    serialized = _canonical_json(llm_evidence)
    llm_evidence["budget"] = {
        "max_chars": max_chars,
        "actual_chars": len(serialized),
        "estimated_input_tokens": max(1, math.ceil(len(serialized) / 3.2)),
        "bounded": len(serialized) <= max_chars + 512,
    }

    overview = {
        "source": result.get("source"),
        "probe_ids": list(result.get("probe_ids") or []),
        "active_minutes": result.get("active_minutes"),
        "counts": counts,
        "partial_failure": bool(result.get("partial_failure")),
        "errors": list(result.get("errors") or []),
        "statistics_by_kind": statistics_by_kind,
        "integrity": manifest,
        "llm_budget": llm_evidence["budget"],
        "coverage": llm_evidence["coverage"],
    }
    return {"llm_evidence": llm_evidence, "manifest": manifest, "overview": overview}
