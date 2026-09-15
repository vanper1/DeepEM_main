from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from deepem.devices.abstract import DeviceCapability, DeviceCommand, DeviceResult


class ProbeApiError(RuntimeError):
    def __init__(self, *, status_code: int | None, message: str, payload: Any | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


ProbeProgressHandler = Callable[[str, dict[str, Any]], None]


@dataclass(slots=True)
class ProbeClient:
    """Client for the device-platform WiFi/Bluetooth probe APIs."""

    base_url: str
    timeout: float = 12.0

    @classmethod
    def from_env(cls) -> "ProbeClient":
        base_url = (
            os.getenv("DEEPEM_PROBE_BASE_URL")
            or os.getenv("DEEPEM_DEVICE_PLATFORM_BASE_URL")
            or "http://10.168.1.145:3000"
        ).rstrip("/")
        return cls(base_url=base_url, timeout=float(os.getenv("DEEPEM_PROBE_TIMEOUT_SEC") or "12"))

    def list_devices(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/probe/devices")
        return list(payload or [])

    def list_targets(self, **params: Any) -> dict[str, Any]:
        payload = self._request("GET", "/api/probe/targets", query=params)
        return dict(payload or {})

    def list_observations(self, **params: Any) -> dict[str, Any]:
        payload = self._request("GET", "/api/probe/observations", query=params)
        return dict(payload or {})

    def collect_evidence(
        self,
        *,
        probe_ids: list[str] | None = None,
        kinds: list[str] | None = None,
        active_minutes: int = 30,
        page_size: int = 200,
        progress: ProbeProgressHandler | None = None,
    ) -> dict[str, Any]:
        selected_ids = {str(item).strip() for item in (probe_ids or []) if str(item).strip()}
        requested_kinds = [item for item in (kinds or ["wifi_ap", "wifi_client", "bluetooth"]) if item in {"wifi_ap", "wifi_client", "bluetooth"}]
        if not requested_kinds:
            requested_kinds = ["wifi_ap", "wifi_client", "bluetooth"]
        page_size = max(1, min(200, int(page_size or 200)))
        active_minutes = max(1, min(1440, int(active_minutes or 30)))

        errors: list[dict[str, Any]] = []
        if progress:
            progress("devices_started", {"message": "正在查询探针列表和在线状态"})
        try:
            all_devices = self.list_devices()
            devices = [item for item in all_devices if not selected_ids or str(item.get("probe_id") or "") in selected_ids]
            if selected_ids:
                missing = sorted(selected_ids - {str(item.get("probe_id") or "") for item in devices})
                for probe_id in missing:
                    errors.append({"stage": "devices", "probe_id": probe_id, "message": "探针不存在或本次未返回"})
            if progress:
                progress("devices_completed", {"message": f"已读取 {len(devices)} 台探针", "count": len(devices), "devices": devices})
        except Exception as exc:
            devices = []
            errors.append({"stage": "devices", "message": str(exc)})
            if progress:
                progress("devices_failed", {"message": str(exc)})

        targets: list[dict[str, Any]] = []
        if progress:
            progress("targets_started", {"message": "正在查询 WiFi/蓝牙目标汇总"})
        for device in devices:
            probe_id = str(device.get("probe_id") or "")
            for kind in requested_kinds:
                try:
                    payload = self.list_targets(
                        probe_id=probe_id,
                        kind=kind,
                        status="all",
                        page=1,
                        page_size=page_size,
                    )
                    items = list(payload.get("items") or [])
                    targets.extend(items)
                    if progress:
                        progress(
                            "targets_batch",
                            {
                                "message": f"探针 {probe_id} 已获取 {kind} 目标 {len(items)} 条",
                                "probe_id": probe_id,
                                "kind": kind,
                                "count": len(items),
                                "total": payload.get("total"),
                            },
                        )
                except Exception as exc:
                    errors.append({"stage": "targets", "probe_id": probe_id, "kind": kind, "message": str(exc)})
                    if progress:
                        progress("targets_batch_failed", {"message": str(exc), "probe_id": probe_id, "kind": kind})
        if progress:
            progress("targets_completed", {"message": f"目标汇总查询完成，共 {len(targets)} 条", "count": len(targets)})

        observations: list[dict[str, Any]] = []
        if progress:
            progress("observations_started", {"message": "正在查询目标观测时段明细"})
        for device in devices:
            probe_id = str(device.get("probe_id") or "")
            for kind in requested_kinds:
                try:
                    payload = self.list_observations(
                        probe_id=probe_id,
                        kind=kind,
                        view="current",
                        active_minutes=active_minutes,
                        page=1,
                        page_size=page_size,
                    )
                    items = list(payload.get("items") or [])
                    observations.extend(items)
                    if progress:
                        progress(
                            "observations_batch",
                            {
                                "message": f"探针 {probe_id} 已获取 {kind} 观测 {len(items)} 条",
                                "probe_id": probe_id,
                                "kind": kind,
                                "count": len(items),
                                "total": payload.get("total"),
                            },
                        )
                except Exception as exc:
                    errors.append({"stage": "observations", "probe_id": probe_id, "kind": kind, "message": str(exc)})
                    if progress:
                        progress("observations_batch_failed", {"message": str(exc), "probe_id": probe_id, "kind": kind})
        if progress:
            progress("observations_completed", {"message": f"观测明细查询完成，共 {len(observations)} 条", "count": len(observations)})

        counts_by_kind = {
            kind: {
                "targets": sum(1 for item in targets if item.get("kind") == kind),
                "observations": sum(1 for item in observations if item.get("kind") == kind),
            }
            for kind in requested_kinds
        }
        online_count = sum(1 for item in devices if str(item.get("status") or "").upper() == "ONLINE")
        return {
            "source": self.base_url,
            "probe_ids": [str(item.get("probe_id") or "") for item in devices],
            "devices": devices,
            "targets": targets,
            "observations": observations,
            "counts": {
                "devices": len(devices),
                "online_devices": online_count,
                "targets": len(targets),
                "observations": len(observations),
                "by_kind": counts_by_kind,
            },
            "errors": errors,
            "partial_failure": bool(errors),
            "active_minutes": active_minutes,
        }

    def _request(self, method: str, path: str, *, query: dict[str, Any] | None = None) -> Any:
        params = {
            key: value
            for key, value in (query or {}).items()
            if value is not None and value != ""
        }
        suffix = f"?{urllib.parse.urlencode(params, doseq=True)}" if params else ""
        request = urllib.request.Request(
            f"{self.base_url}{path}{suffix}",
            headers={"Accept": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = None
            message = f"探针 API 返回 HTTP {exc.code}"
            if raw:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    detail = payload.get("detail") if isinstance(payload, dict) else None
                    if isinstance(detail, list):
                        message = "; ".join(str(item.get("msg") or item) for item in detail if isinstance(item, dict)) or str(detail)
                    elif detail:
                        message = str(detail)
                except Exception:
                    message = raw.decode("utf-8", errors="replace")
            raise ProbeApiError(status_code=exc.code, message=message, payload=payload) from exc
        except urllib.error.URLError as exc:
            raise ProbeApiError(status_code=None, message=f"无法连接 WiFi/蓝牙探针服务：{exc.reason}") from exc
        except TimeoutError as exc:
            raise ProbeApiError(status_code=None, message="WiFi/蓝牙探针服务请求超时") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ProbeApiError(status_code=None, message="WiFi/蓝牙探针服务返回了无法解析的 JSON") from exc


class ProbeDeviceAdapter:
    device_id = "wifi-bluetooth-probe-platform"

    def __init__(self, client: ProbeClient | None = None) -> None:
        self.client = client or ProbeClient.from_env()

    def list_capabilities(self) -> list[DeviceCapability]:
        return [
            DeviceCapability(name="list_wifi_bluetooth_probes", description="查询 WiFi/蓝牙探针列表及在线状态。"),
            DeviceCapability(name="collect_wifi_bluetooth_probe_evidence", description="依次调用探针设备、目标汇总和观测明细接口，形成辅助研判证据。"),
        ]

    def execute(self, command: DeviceCommand) -> DeviceResult:
        try:
            if command.capability == "list_wifi_bluetooth_probes":
                return DeviceResult(status="success", data={"devices": self.client.list_devices(), "source": self.client.base_url})
            if command.capability == "collect_wifi_bluetooth_probe_evidence":
                return DeviceResult(status="success", data=self.client.collect_evidence(**dict(command.args or {})))
            return DeviceResult(status="error", error=f"unsupported capability: {command.capability}")
        except ProbeApiError as exc:
            return DeviceResult(status="error", error=str(exc), data={"status_code": exc.status_code, "payload": exc.payload})
