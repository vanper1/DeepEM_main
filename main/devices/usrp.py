from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from deepem.devices.abstract import DeviceCapability, DeviceCommand, DeviceResult


class UsrpApiError(RuntimeError):
    def __init__(self, *, status_code: int | None, message: str, payload: Any | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass(slots=True)
class UsrpClient:
    """HTTP/WebSocket client for the current USRP B210 acquisition service API.

    The current service exposes these public endpoints:
    - POST /api/usrp/scan
    - GET  /api/usrp/devices
    - POST /api/usrp/configure
    - POST /api/usrp/start
    - POST /api/usrp/{dev_id}/stop
    - WS   /api/usrp/{dev_id}/stream
    - GET  /api/usrp/tasks
    - GET  /api/usrp/tasks/{task_id}/files
    - GET  /api/usrp/tasks/{task_id}/files/{filename}
    """

    base_url: str
    timeout: float = 12.0
    ws_base_url: str | None = None

    @classmethod
    def from_env(cls) -> "UsrpClient":
        base_url = (os.getenv("DEEPEM_USRP_BASE_URL") or "http://10.112.210.4:8100").rstrip("/")
        return cls(
            base_url=base_url,
            timeout=float(os.getenv("DEEPEM_USRP_TIMEOUT_SEC") or "12"),
            ws_base_url=(os.getenv("DEEPEM_USRP_WS_BASE_URL") or "").rstrip("/") or None,
        )

    def scan(self) -> dict[str, Any]:
        payload = self._request("POST", "/api/usrp/scan")
        return dict(payload or {})

    def list_devices(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/usrp/devices")
        return list(payload or [])

    def configure(self, config: dict[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", "/api/usrp/configure", json_payload=config) or {})

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", "/api/usrp/start", json_payload=request) or {})

    def stop(self, *, dev_id: str, task_id: str) -> dict[str, Any]:
        dev = str(dev_id or "").strip()
        if not dev:
            raise UsrpApiError(status_code=422, message="dev_id is required")
        return dict(self._request("POST", f"/api/usrp/{urllib.parse.quote(dev, safe='')}/stop", json_payload={"task_id": task_id}) or {})

    def list_tasks(self) -> dict[str, Any]:
        """Return all persisted USRP acquisition tasks and their .npz files."""
        return dict(self._request("GET", "/api/usrp/tasks") or {})

    def list_task_files(self, task_id: str) -> dict[str, Any]:
        """Return metadata for .npz slice files produced by a USRP task."""
        task = urllib.parse.quote(str(task_id), safe="")
        return dict(self._request("GET", f"/api/usrp/tasks/{task}/files") or {})

    def download_task_file(self, task_id: str, filename: str) -> bytes:
        """Download one .npz slice file as bytes from the USRP service."""
        task = urllib.parse.quote(str(task_id), safe="")
        name = urllib.parse.quote(str(filename), safe="")
        return self._request_binary("GET", f"/api/usrp/tasks/{task}/files/{name}")

    def ws_url(self, dev_id: str) -> str:
        """Return the upstream WebSocket URL for real-time FFT frames."""
        dev = str(dev_id or "").strip()
        if not dev:
            raise UsrpApiError(status_code=422, message="dev_id is required")
        base = self.ws_base_url
        if not base:
            parsed = urllib.parse.urlparse(self.base_url)
            scheme = "wss" if parsed.scheme == "https" else "ws"
            base = urllib.parse.urlunparse((scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")).rstrip("/")
        return f"{base}/api/usrp/{urllib.parse.quote(dev, safe='')}/stream"

    def find_task_on_devices(self, task_id: str) -> dict[str, Any]:
        """Compatibility helper for tools that only need running-state status.

        Completed task files are now fetched through /api/usrp/tasks/{task_id}/files
        and recorded in the platform session state after download.
        """
        task_id = str(task_id or "").strip()
        if not task_id:
            raise UsrpApiError(status_code=422, message="task_id is required")
        devices = self.list_devices()
        matches = [dev for dev in devices if str(dev.get("task_id") or "") == task_id]
        if matches:
            dev = dict(matches[0])
            return {
                "task_id": task_id,
                "status": "running" if str(dev.get("status") or "").upper() == "BUSY" else str(dev.get("status") or "UNKNOWN"),
                "device": dev,
                "source": "/api/usrp/devices",
                "message": "新 USRP API 不再提供任务详情端点，当前状态由设备表推断。",
            }
        return {
            "task_id": task_id,
            "status": "not_running_or_completed",
            "devices": devices,
            "source": "/api/usrp/devices",
            "message": "未在当前设备表中找到运行中的 task_id；任务可能已完成、停止或失败，请结合平台主动下载的 .npz 文件判断。",
        }

    def _request(self, method: str, path: str, *, json_payload: dict[str, Any] | None = None) -> Any:
        raw = self._open(method, path, json_payload=json_payload, accept="application/json")
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _request_binary(self, method: str, path: str) -> bytes:
        return self._open(method, path, accept="application/octet-stream")

    def _open(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        accept: str = "application/json",
    ) -> bytes:
        body = None
        headers = {"Accept": accept}
        if json_payload is not None:
            body = json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = None
            message = f"USRP API returned HTTP {exc.code}"
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
            raise UsrpApiError(status_code=exc.code, message=message, payload=payload) from exc
        except urllib.error.URLError as exc:
            raise UsrpApiError(status_code=None, message=f"无法连接 USRP 服务：{exc.reason}") from exc
        except TimeoutError as exc:
            raise UsrpApiError(status_code=None, message="USRP 服务请求超时") from exc


class UsrpDeviceAdapter:
    device_id = "usrp-platform"

    def __init__(self, client: UsrpClient | None = None) -> None:
        self.client = client or UsrpClient.from_env()

    def list_capabilities(self) -> list[DeviceCapability]:
        return [
            DeviceCapability(name="scan_usrp_devices", description="扫描 USRP 硬件并刷新设备能力、状态和 dev_id。"),
            DeviceCapability(name="list_usrp_devices", description="查询 USRP 设备状态、能力范围、current_config 和 task_id。"),
            DeviceCapability(name="configure_usrp_capture", description="调用新 configure API 校验并保存采集参数，但不启动采集。"),
            DeviceCapability(name="query_usrp_task", description="按新 API 从设备表推断运行中任务状态；完成历史由平台主动下载的 .npz 文件记录。"),
        ]

    def execute(self, command: DeviceCommand) -> DeviceResult:
        try:
            if command.capability == "scan_usrp_devices":
                return DeviceResult(status="success", data=self.client.scan())
            if command.capability == "list_usrp_devices":
                return DeviceResult(status="success", data={"devices": self.client.list_devices()})
            if command.capability == "configure_usrp_capture":
                payload = dict(command.args or {})
                return DeviceResult(status="success", data=self.client.configure(payload))
            if command.capability == "query_usrp_task":
                task_id = str(command.args.get("task_id") or "")
                if not task_id:
                    return DeviceResult(status="error", error="task_id is required")
                return DeviceResult(status="success", data={"task": self.client.find_task_on_devices(task_id)})
            return DeviceResult(status="error", error=f"unsupported capability: {command.capability}")
        except UsrpApiError as exc:
            return DeviceResult(status="error", error=str(exc), data={"status_code": exc.status_code, "payload": exc.payload})
