from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .probe_evidence_store import ProbeEvidenceStore, get_probe_evidence_store


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", _clean(value).lower())


def _usable_name(value: Any) -> bool:
    text = _clean(value)
    return bool(text and text.lower() not in {"unknown", "unnamed", "none", "null", "--", "未命名设备", "n/a"})


def _display_name(row: dict[str, Any]) -> str:
    for key in ("name", "device_name", "ssid", "connected_ssid", "alias"):
        if _usable_name(row.get(key)):
            return _clean(row.get(key))
    for key in ("mac", "bssid", "connected_bssid"):
        if _clean(row.get(key)):
            return _clean(row.get(key))
    return "未命名设备"


def _identity_tokens(row: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in ("mac", "bssid", "connected_bssid"):
        value = _clean(row.get(key)).lower()
        if value:
            tokens.add(f"mac:{value}")
    for key in ("name", "device_name", "ssid", "connected_ssid", "alias"):
        value = row.get(key)
        if _usable_name(value):
            normalized = _norm(value)
            if len(normalized) >= 2:
                tokens.add(f"name:{normalized}")
    return tokens


def _modality(kind: str) -> str:
    return {
        "wifi_ap": "WiFi热点",
        "wifi_client": "WiFi客户端",
        "bluetooth": "蓝牙",
    }.get(kind, kind or "探针")


def _artifact_public(task_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
    artifact_id = str(artifact.get("id") or "")
    result = {
        "artifact_id": artifact_id,
        "file_name": artifact.get("file_name"),
        "kind": artifact.get("kind"),
        "size_bytes": artifact.get("size_bytes"),
        "created_at": artifact.get("created_at"),
    }
    if artifact_id:
        result["download_url"] = f"/api/capture-agent/tasks/{task_id}/artifacts/{artifact_id}"
        if str(artifact.get("file_name") or "").lower().endswith(".npz"):
            result["spectrum_url"] = f"/api/capture-agent/tasks/{task_id}/artifacts/{artifact_id}/spectrum"
    return result


def build_device_evidence_chain(
    task: dict[str, Any],
    *,
    store: ProbeEvidenceStore | None = None,
) -> dict[str, Any]:
    """Build cautious logical device -> evidence chains across probe and task artifacts.

    Probe records are joined when they share a strong identifier (MAC/BSSID) or a
    meaningful device name/SSID. IQ / IR artifacts are attached as task-level
    contextual evidence only; co-occurrence is explicitly not treated as identity proof.
    """
    store = store or get_probe_evidence_store()
    task_id = _clean(task.get("id"))
    evidence_set_ids: list[str] = []
    for item in task.get("probe_evidence_sets") or []:
        value = _clean((item or {}).get("evidence_set_id"))
        if value and value not in evidence_set_ids:
            evidence_set_ids.append(value)
    for node in task.get("nodes") or []:
        output = dict((node or {}).get("outputs") or {})
        for candidate in (output.get("evidence_set_id"), (output.get("result") or {}).get("evidence_set_id") if isinstance(output.get("result"), dict) else None):
            value = _clean(candidate)
            if value and value not in evidence_set_ids:
                evidence_set_ids.append(value)
    if not evidence_set_ids and task_id:
        latest = store.latest_for_task(task_id)
        if latest:
            evidence_set_ids.append(_clean(latest.get("evidence_set_id")))

    records: list[dict[str, Any]] = []
    load_errors: list[str] = []
    for evidence_set_id in evidence_set_ids:
        try:
            raw = store.load_raw_result(evidence_set_id)
        except Exception as exc:
            load_errors.append(f"{evidence_set_id}: {exc}")
            continue
        for section in ("targets", "observations"):
            for raw_row in raw.get(section) or []:
                row = dict(raw_row or {})
                row["_section"] = section
                row["_evidence_set_id"] = evidence_set_id
                records.append(row)

    # Union-find across strong identity tokens.
    parent = list(range(len(records)))
    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    token_owner: dict[str, int] = {}
    for i, row in enumerate(records):
        for token in _identity_tokens(row):
            if token in token_owner:
                union(i, token_owner[token])
            else:
                token_owner[token] = i

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for i, row in enumerate(records):
        grouped[find(i)].append(row)

    task_artifacts = [_artifact_public(task_id, dict(item or {})) for item in task.get("artifacts") or []]
    contextual_artifacts = []
    for artifact in task_artifacts:
        name = _clean(artifact.get("file_name")).lower()
        kind = _clean(artifact.get("kind")).lower()
        if name.endswith(".npz") or "iq" in name or "spectrum" in kind or any(token in name + " " + kind for token in ("infrared", "thermal", "红外", "ir_")):
            contextual_artifacts.append(artifact)

    devices: list[dict[str, Any]] = []
    for index, rows in enumerate(grouped.values(), start=1):
        names = []
        identifiers = []
        modalities = []
        evidence = []
        relations = []
        strongest_rssi = None
        last_seen = ""
        for row in rows:
            name = _display_name(row)
            if _usable_name(name) and name not in names:
                names.append(name)
            for key in ("mac", "bssid", "connected_bssid"):
                value = _clean(row.get(key))
                if value and value not in identifiers:
                    identifiers.append(value)
            kind = _clean(row.get("kind"))
            label = _modality(kind)
            if label not in modalities:
                modalities.append(label)
            try:
                rssi = float(row.get("rssi_latest")) if row.get("rssi_latest") not in (None, "") else None
            except (TypeError, ValueError):
                rssi = None
            if rssi is not None and (strongest_rssi is None or rssi > strongest_rssi):
                strongest_rssi = rssi
            seen = _clean(row.get("last_seen"))
            if seen > last_seen:
                last_seen = seen
            evidence.append({
                "evidence_type": "probe_record",
                "modality": label,
                "kind": kind,
                "section": row.get("_section"),
                "evidence_set_id": row.get("_evidence_set_id"),
                "name": name,
                "mac": row.get("mac"),
                "ssid": row.get("ssid"),
                "bssid": row.get("bssid"),
                "connected_ssid": row.get("connected_ssid"),
                "connected_bssid": row.get("connected_bssid"),
                "vendor": row.get("vendor"),
                "channel": row.get("channel"),
                "rssi": rssi,
                "distance": row.get("distance"),
                "last_seen": row.get("last_seen"),
                "probe_id": row.get("probe_id"),
                "association": "identifier_or_name_match",
            })
            if row.get("connected_ssid") or row.get("connected_bssid"):
                relations.append({
                    "type": "connected_to_wifi",
                    "ssid": row.get("connected_ssid"),
                    "bssid": row.get("connected_bssid"),
                    "source": "probe_record",
                })

        for artifact in contextual_artifacts:
            evidence.append({
                "evidence_type": "task_artifact",
                "modality": "IQ/频谱" if str(artifact.get("file_name") or "").lower().endswith(".npz") else "其他模态",
                "artifact": artifact,
                "association": "same_capture_task_context",
                "confidence": "contextual_only",
                "note": "与该设备在线索层面属于同一采集任务共现，不代表已通过射频指纹或身份字段完成一一确认。",
            })
            modality = "IQ/频谱" if str(artifact.get("file_name") or "").lower().endswith(".npz") else "其他模态"
            if modality not in modalities:
                modalities.append(modality)

        display = names[0] if names else (identifiers[0] if identifiers else f"设备 {index}")
        iq_spectrum_evidence_count = sum(
            1
            for item in evidence
            if _clean(item.get("modality")) == "IQ/频谱"
            or (
                item.get("evidence_type") == "task_artifact"
                and (
                    _clean((item.get("artifact") or {}).get("file_name")).lower().endswith(".npz")
                    or "spectrum" in _clean((item.get("artifact") or {}).get("kind")).lower()
                )
            )
        )
        devices.append({
            "device_id": f"logical_device_{index}",
            "display_name": display,
            "aliases": names[1:12],
            "identifiers": identifiers[:20],
            "modalities": modalities,
            "relations": relations[:30],
            "strongest_rssi": strongest_rssi,
            "last_seen": last_seen or None,
            "iq_spectrum_evidence_count": iq_spectrum_evidence_count,
            "evidence_count": len(evidence),
            "evidence": evidence[:300],
            "identity_confidence": "strong" if identifiers and len(rows) > 1 else ("medium" if identifiers or len(names) > 0 else "weak"),
        })

    devices.sort(
        key=lambda item: (
            -(item.get("iq_spectrum_evidence_count") or 0),
            -(item.get("evidence_count") or 0),
            item.get("display_name") or "",
        )
    )
    return {
        "task_id": task_id,
        "generated_from_evidence_sets": evidence_set_ids,
        "device_count": len(devices),
        "devices": devices,
        "task_context_artifacts": contextual_artifacts,
        "load_errors": load_errors,
        "association_policy": {
            "strong_join": "共享 MAC/BSSID/connected_bssid，或共享有意义的设备名称/SSID",
            "cross_modal": "IQ/频谱/红外等任务产物按任务级共现关联，除非后续证据提供更强身份映射",
            "warning": "设备—证据链用于汇聚线索，不把任务级共现自动解释为同一物理设备的身份确认。",
        },
    }
