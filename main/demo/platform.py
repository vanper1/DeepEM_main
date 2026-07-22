from __future__ import annotations

import json
import os
import math
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from deepem.app import build_app_from_env
from deepem.devices.usrp import UsrpApiError, UsrpClient
from deepem.protocol import ChatRole, EvidenceRef
from deepem.realtime.evidence_store import SignalEvidenceStore
from deepem.realtime.real_capture import RealSignalAdapter, SessionAnalyzer, build_spectrogram_payload, parse_npz

from .platform_support import build_demo_snapshot, utc_now_iso


class DemoPlatform:
    def __init__(self) -> None:
        self._root = Path(__file__).resolve().parent.parent
        self._db_path = self._root / "deepem_demo.sqlite3"
        # .npz slices are now fetched by the main platform from the USRP service
        # and grouped locally as main/data/npz_data/{session_id}/{usrp_task_id}/.
        self._uploads_root = self._root / "data" / "npz_data"
        self._evidence_root = self._root / "data" / "evidence"
        self._lock = threading.RLock()
        self._detection_stop = threading.Event()
        self._chat_stop = threading.Event()
        self._detection_thread: threading.Thread | None = None
        self._chat_thread: threading.Thread | None = None
        self._active_detection_job: dict[str, Any] | None = None
        self._active_detection_cancel: threading.Event | None = None
        self._usrp_threads: dict[str, threading.Thread] = {}
        self._detection_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._chat_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._logs: list[str] = []
        self._usrp_client = UsrpClient.from_env()
        self._evidence_store = SignalEvidenceStore(root=self._evidence_root)
        self.app = None
        self.task = None
        self._initialize(clear_existing=False)

    def reset(self) -> None:
        self._initialize(clear_existing=True)

    def scan_devices(self) -> dict[str, Any]:
        """Call POST /api/usrp/scan and persist the returned device table."""
        try:
            scan_payload = self._usrp_client.scan()
        except UsrpApiError as exc:
            return {"devices": [], "found_count": 0, "scan_error": str(exc)}
        devices = list(scan_payload.get("devices") or [])
        found = int(scan_payload.get("found_count") or len([d for d in devices if d.get("status") != "OFFLINE"]))
        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            collector["devices"] = devices
            collector["last_scan"] = {
                "found_count": found,
                "scan_error": scan_payload.get("scan_error"),
                "updated_at": utc_now_iso(),
            }
            self._save_collector_state(state, collector)
        return self._device_payload_mhz({"devices": devices, "found_count": found, "scan_error": scan_payload.get("scan_error")})

    def list_devices(self) -> dict[str, Any]:
        """Call GET /api/usrp/devices without touching hardware."""
        try:
            devices = self._usrp_client.list_devices()
        except UsrpApiError as exc:
            return {"devices": [], "error": str(exc)}
        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            collector["devices"] = devices
            self._save_collector_state(state, collector)
        return self._device_payload_mhz({"devices": devices, "error": None})

    def get_usrp_stream_info(self, dev_id: str | None = None) -> dict[str, Any]:
        """Return the upstream WebSocket endpoint used by the browser for FFT frames."""
        device_id = str(dev_id or "").strip()
        if not device_id:
            with self._lock:
                state = self.app.runtime.state_repo.get(self.task.id)
                collector = self._collector_meta(state)
                active = collector.get("active_session") or {}
                device_id = str(active.get("device_id") or "")
                if not device_id:
                    params = dict(collector.get("device_params") or {})
                    device_id = str(params.get("dev_id") or "")
                if not device_id:
                    devices = list(collector.get("devices") or [])
                    candidate = next((item for item in devices if item.get("dev_id")), None)
                    device_id = str((candidate or {}).get("dev_id") or "")
        if not device_id:
            device_id = (os.getenv("DEEPEM_USRP_DEVICE_ID") or "usrp-30B1FDE").strip()
        return {"dev_id": device_id, "ws_url": self._usrp_client.ws_url(device_id), "fft_size": 1024}

    @staticmethod
    def _mhz_value(value: Any) -> float:
        number = float(value)
        return number / 1e6 if abs(number) > 100000 else number

    @classmethod
    def _range_mhz(cls, value: Any) -> Any:
        if isinstance(value, dict):
            converted = dict(value)
            for key in ("min", "max", "default"):
                if key in converted and isinstance(converted.get(key), (int, float)):
                    converted[key] = cls._mhz_value(converted[key])
            return converted
        return value

    @classmethod
    def _device_payload_mhz(cls, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload or {})
        devices = []
        for device in result.get("devices") or []:
            item = dict(device)
            cfg = dict(item.get("dev_config") or {})
            for key in ("freq_range", "sample_rate_range", "bandwidth_range"):
                if key in cfg:
                    cfg[key] = cls._range_mhz(cfg[key])
            item["dev_config"] = cfg
            current = dict(item.get("current_config") or {})
            for key in ("freq", "sample_rate", "bandwidth"):
                if key in current and isinstance(current.get(key), (int, float)):
                    current[key] = cls._mhz_value(current[key])
            item["current_config"] = current
            devices.append(item)
        result["devices"] = devices
        return result

    @staticmethod
    def _to_usrp_unit(value: Any) -> float:
        number = float(value)
        return number if abs(number) > 100000 else number * 1e6

    def get_device_params(self) -> dict[str, Any]:
        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            params = dict(collector.get("device_params") or {})
        defaults = {
            "dev_id": (os.getenv("DEEPEM_USRP_DEVICE_ID") or "").strip() or None,
            "sample_rate": self._mhz_value(os.getenv("DEEPEM_USRP_SAMPLE_RATE") or "1000000"),
            "bandwidth": self._mhz_value(os.getenv("DEEPEM_USRP_BANDWIDTH") or "1000000"),
            "gain": float(os.getenv("DEEPEM_USRP_GAIN") or "40"),
            "slice_duration": float(os.getenv("DEEPEM_USRP_SLICE_DURATION") or "1"),
            "duration": float(os.getenv("DEEPEM_USRP_STEP_DURATION") or "3"),
            "freq": self._mhz_value(os.getenv("DEEPEM_USRP_FREQ") or "2400000000"),
            # channel is a DeepEM/WiFi analysis label only; it is not sent to the new USRP API.
            "channel": int(os.getenv("DEEPEM_USRP_CHANNEL") or "0"),
            "antenna": (os.getenv("DEEPEM_USRP_ANTENNA") or "RX2").strip() or None,
        }
        merged = {**defaults, **params}
        if not merged.get("dev_id"):
            try:
                state = self.app.runtime.state_repo.get(self.task.id)
                collector = self._collector_meta(state)
                devices = list(collector.get("devices") or [])
                candidate = next((item for item in devices if item.get("dev_id") and str(item.get("status")) == "IDLE"), None)
                candidate = candidate or next((item for item in devices if item.get("dev_id")), None)
                if candidate:
                    merged["dev_id"] = candidate.get("dev_id")
            except Exception:
                pass
        return merged

    def set_device_params(self, params: dict[str, Any]) -> dict[str, Any]:
        cleaned = self._normalize_device_params(params)
        configure_result: dict[str, Any] | None = None
        configure_error: str | None = None
        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            existing = dict(collector.get("device_params") or {})
            merged = {**existing, **cleaned}
            collector["device_params"] = merged
            self._save_collector_state(state, collector)

        # Saving parameters now exercises the new configure API when enough fields are present.
        # Start still sends the complete parameter set again, per the USRP service contract.
        try:
            config_payload = self._build_usrp_config_payload(self.get_device_params(), require_dev_id=False)
            if not config_payload.get("dev_id"):
                selected = self._select_usrp_device()
                config_payload["dev_id"] = selected["dev_id"]
            configure_result = self._usrp_client.configure(config_payload)
            with self._lock:
                state = self.app.runtime.state_repo.get(self.task.id)
                collector = self._collector_meta(state)
                collector["last_configure_result"] = configure_result
                self._record_collector_activity(
                    collector,
                    summary="USRP configure 参数校验通过，配置已保存到采集服务。",
                    level="normal",
                    details=[{"kind_label": "configure", "content": str(configure_result)}],
                )
                self._save_collector_state(state, collector)
        except Exception as exc:
            configure_error = str(exc)
            with self._lock:
                state = self.app.runtime.state_repo.get(self.task.id)
                collector = self._collector_meta(state)
                collector["last_configure_error"] = configure_error
                self._record_collector_activity(
                    collector,
                    summary=f"USRP configure 参数校验未通过或服务不可达：{configure_error}",
                    level="warning",
                    details=[{"kind_label": "参数", "content": str(cleaned)}],
                )
                self._save_collector_state(state, collector)
        result = self.get_device_params()
        if configure_result is not None:
            result["configure_result"] = configure_result
        if configure_error:
            result["configure_error"] = configure_error
        return result

    @staticmethod
    def _normalize_device_params(params: dict[str, Any]) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key in ("sample_rate", "bandwidth", "gain", "slice_duration", "duration", "freq", "channel", "antenna", "dev_id"):
            if key not in params:
                continue
            value = params.get(key)
            if key == "duration" and value in {"", "null", "None"}:
                cleaned[key] = None
                continue
            if value is None:
                if key == "duration":
                    cleaned[key] = None
                continue
            if key == "channel":
                cleaned[key] = int(value)
            elif key in {"antenna", "dev_id"}:
                val = str(value).strip()
                if val:
                    cleaned[key] = val
            else:
                cleaned[key] = float(value)
        return cleaned

    def _build_usrp_config_payload(self, params: dict[str, Any], *, require_dev_id: bool = True) -> dict[str, Any]:
        required = ["freq", "sample_rate", "bandwidth", "gain", "slice_duration"]
        missing = [key for key in required if params.get(key) is None]
        if missing:
            raise ValueError("缺少 USRP 参数：" + ", ".join(missing))
        if require_dev_id and not params.get("dev_id"):
            raise ValueError("缺少 dev_id，请先扫描设备或设置 DEEPEM_USRP_DEVICE_ID")
        bandwidth = self._to_usrp_unit(params["bandwidth"])
        sample_rate = self._to_usrp_unit(params["sample_rate"])
        if bandwidth > sample_rate:
            raise ValueError("bandwidth 不能大于 sample_rate")
        payload: dict[str, Any] = {
            "dev_id": str(params.get("dev_id") or ""),
            "freq": self._to_usrp_unit(params["freq"]),
            "sample_rate": sample_rate,
            "bandwidth": bandwidth,
            "gain": float(params["gain"]),
            "slice_duration": float(params["slice_duration"]),
            "duration": None if params.get("duration") is None else float(params["duration"]),
            "antenna": params.get("antenna"),
        }
        return {key: value for key, value in payload.items() if value is not None or key == "duration"}

    def start_stream(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            active = self._active_session()
            if active and active.get("status") in {"pending", "starting", "running"}:
                self._log(f"已有采集任务在执行：{active['session_id']}")
                return active
            if overrides:
                state = self.app.runtime.state_repo.get(self.task.id)
                collector = self._collector_meta(state)
                merged_params = {**dict(collector.get("device_params") or {}), **self._normalize_device_params(dict(overrides))}
                collector["device_params"] = merged_params
                self._save_collector_state(state, collector)
            # If the operator entered a dev_id in the panel, prefer it for this run.
            device = self._select_usrp_device()
            plan = self._load_usrp_plan(overrides=overrides)
            session = self._create_capture_session(
                device=device,
                acquisition_plan=plan,
                source_mode="real",
            )
            session_id = session["session_id"]
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            self._record_collector_activity(
                collector,
                summary=f"已创建 USRP 采集计划 {session_id}，后台将按 {len(plan)} 个频点启动设备。",
                level="info",
                details=[
                    {"kind_label": "设备", "content": str(device.get("dev_id", "-"))},
                    {"kind_label": "计划", "content": " -> ".join(str(item.get("label")) for item in plan)},
                ],
            )
            self._save_collector_state(state, collector)
            self._log(f"已创建真实 USRP 采集计划：{session_id}，共 {len(plan)} 个频点。")
        self._launch_usrp_step_thread(session_id)
        return session

    def stop_stream(self) -> dict[str, Any]:
        stop_info: dict[str, str] | None = None
        with self._lock:
            review_cancelled = self._cancel_detection_reviews_locked()
            active = self._active_session()
            if not active or active.get("status") not in {"pending", "starting", "running"}:
                if review_cancelled["queued"] or review_cancelled["running"]:
                    return {
                        "status": "cancelled",
                        "session_id": active.get("session_id") if active else None,
                        "cancelled_reviews": review_cancelled,
                    }
                return {"status": "idle", "session_id": active.get("session_id") if active else None}
            stopped = self._cancel_active_session_locked(active, reason="manual_stop")
            stopped["cancelled_reviews"] = review_cancelled
            stop_info = self._usrp_stop_info(stopped)
        if stop_info:
            stopped["usrp_stop"] = self._stop_usrp_task_safe(stop_info)
        return stopped

    def send_chat(self, content: str) -> dict[str, Any]:
        message = self.app.append_chat(task_id=self.task.id, content=content, role=ChatRole.OPERATOR)
        self._chat_queue.put({"task_id": self.task.id, "message_id": message.id})
        self._log(f"聊天消息已入队：{content}")
        return {"message_id": message.id, "status": "queued"}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return build_demo_snapshot(self)

    def collector_pending(self, collector_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            pending_queue = list(collector.get("pending_queue", []))
            while pending_queue:
                session_id = pending_queue.pop(0)
                session = collector["sessions"].get(session_id)
                if not session or session.get("status") != "pending":
                    continue
                session["status"] = "running"
                session["started_at"] = utc_now_iso()
                session["collector_id"] = collector_id
                collector["pending_queue"] = pending_queue
                self._save_collector_state(state, collector)
                self._log(f"collector_agent 已领取任务：{session_id} / {collector_id}")
                return {
                    "command": "start_capture",
                    "session_id": session_id,
                    "duration_sec": session["duration_sec"],
                    "channels_to_scan": session["channels_to_scan"],
                }
            collector["pending_queue"] = []
            self._save_collector_state(state, collector)
            return {"command": "idle"}

    def collector_upload(self, *, session_id: str, collector_id: str, filename: str, content: bytes) -> dict[str, Any]:
        """Compatibility endpoint for older collector agents.

        The main runtime no longer depends on collector callbacks for .npz files;
        new captures are downloaded from the USRP service by the main platform.
        Keeping this method preserves backwards compatibility without changing the
        analysis and evidence ingestion pipeline.
        """
        reported_session_id = session_id
        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            session_id = self._resolve_parent_session_id(collector, reported_session_id)
            session = collector["sessions"].get(session_id)
            if not session:
                raise KeyError(reported_session_id)
            if session.get("status") == "cancelled":
                return {"ok": False, "status": "cancelled", "session_id": session_id}

            session_dir = Path(session["session_dir"])
            session_dir.mkdir(parents=True, exist_ok=True)
            source_name = Path(filename or "capture.npz").name
            stored_name = source_name
            if reported_session_id != session_id:
                stored_name = f"{reported_session_id}_{source_name}"
            filepath = session_dir / stored_name
            filepath.write_bytes(content)

            return self._ingest_npz_file_locked(
                state=state,
                collector=collector,
                session=session,
                session_id=session_id,
                reported_session_id=reported_session_id,
                collector_id=collector_id,
                source_name=source_name,
                stored_name=stored_name,
                filepath=filepath,
            )

    def _ingest_npz_file_locked(
        self,
        *,
        state,
        collector: dict[str, Any],
        session: dict[str, Any],
        session_id: str,
        reported_session_id: str,
        collector_id: str,
        source_name: str,
        stored_name: str,
        filepath: Path,
    ) -> dict[str, Any]:
        iq, meta = parse_npz(filepath)
        current_step = self._current_plan_step(session)
        meta["session_id"] = session_id
        meta["usrp_task_id"] = reported_session_id
        meta["collector_id"] = collector_id
        meta.setdefault("timestamp", utc_now_iso())
        if current_step:
            meta.setdefault("plan_step_id", current_step.get("step_id"))
            meta.setdefault("plan_label", current_step.get("label"))
            meta.setdefault("channel", current_step.get("channel"))
            meta.setdefault("freq", current_step.get("freq"))
            meta.setdefault("center_freq", current_step.get("freq"))
        meta.setdefault("device_id", session.get("device_id"))
        sample_rate = float(meta.get("sample_rate") or 1.0)

        analyzer = SessionAnalyzer.from_payload(session.get("analyzer_state"), channels=session.get("channels_to_scan", [1, 6, 11]))
        analysis = analyzer.process(iq, meta)
        session["analyzer_state"] = analyzer.to_payload()
        analysis["file_name"] = stored_name
        analysis["session_id"] = session_id
        analysis["collector_id"] = collector_id
        analysis["usrp_task_id"] = reported_session_id

        adapted = RealSignalAdapter.adapt(analysis=analysis, meta=meta, filepath=filepath, iq=iq)
        evidence = EvidenceRef(
            kind="npz",
            uri=str(filepath),
            label=stored_name,
            metadata={
                "session_id": session_id,
                "usrp_task_id": reported_session_id,
                "collector_id": collector_id,
                "device_id": session.get("device_id"),
                "channel": analysis["channel"],
                "scan_index": analysis["scan_index"],
                "source_file_name": source_name,
            },
        )
        event = self.app.ingest_structured_signal(
            task_id=self.task.id,
            signal_id=adapted.signal_id,
            batch_id=adapted.batch_id,
            fingerprint=adapted.fingerprint,
            classification=adapted.classification,
            carries_information=adapted.carries_information,
            suspected_device_type=adapted.suspected_device_type,
            source="realtime.usrp_download",
            evidence_refs=[evidence],
            features=adapted.features,
            raw_sample_count=len(iq),
        )

        spectrogram = build_spectrogram_payload(iq, sample_rate=sample_rate)
        spectrogram.update(
            {
                "signal_id": adapted.signal_id,
                "file_name": stored_name,
                "channel": analysis["channel"],
                "freq_mhz": self._mhz_value(meta.get("freq") or meta.get("center_freq") or 0.0),
                "timestamp": str(meta.get("timestamp", "")),
                "usrp_task_id": reported_session_id,
                "device_id": session.get("device_id"),
            }
        )

        self._record_observation(
            state=state,
            collector=collector,
            session=session,
            event_id=event.id,
            observation=adapted.observation_record,
            analysis=analysis,
            spectrogram=spectrogram,
            meta=meta,
            filepath=filepath,
            raw_sample_count=len(iq),
        )
        session.setdefault("downloaded_npz_files", [])
        downloaded_key = str(Path(stored_name))
        if downloaded_key not in session["downloaded_npz_files"]:
            session["downloaded_npz_files"].append(downloaded_key)
            session["downloaded_npz_files"] = session["downloaded_npz_files"][-1000:]
        self._maybe_complete_session(session=session, collector=collector)
        self._save_collector_state(state, collector)

        if adapted.classification == "suspicious":
            self._detection_queue.put({"task_id": self.task.id, "event_id": event.id})
            self._log(f"USRP 采集命中告警，已升级为事件并进入智能体队列：{adapted.signal_id}")
        else:
            self._log(f"USRP 采集信号已入库：{adapted.signal_id}")
        return {"ok": True, "analysis": analysis, "signal": adapted.observation_record}

    def collector_session_complete(self, payload: dict[str, Any], *, launch_next: bool = True) -> dict[str, Any]:
        reported_session_id = str(payload.get("session_id", ""))
        start_next = False
        parent_session_id = reported_session_id
        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            parent_session_id = self._resolve_parent_session_id(collector, reported_session_id)
            session_id = parent_session_id
            session = collector["sessions"].get(session_id)
            if not session:
                raise KeyError(reported_session_id)
            report_status = str(payload.get("status") or "completed").lower()
            session.setdefault("usrp_completed_tasks", []).append(
                {
                    "usrp_task_id": reported_session_id,
                    "device_id": payload.get("device_id"),
                    "status": payload.get("status", "completed"),
                    "message": payload.get("message", ""),
                    "completed_at": str(payload.get("completed_at") or utc_now_iso()),
                }
            )
            collector["pending_queue"] = [item for item in collector.get("pending_queue", []) if item != session_id]
            if report_status in {"failed", "error"}:
                session["status"] = "error"
                session["error"] = str(payload.get("message") or "USRP task failed")
                session["completed_at"] = str(payload.get("completed_at") or utc_now_iso())
                session["usrp_task_id"] = None
                self._record_collector_activity(
                    collector,
                    summary=f"USRP 子任务 {reported_session_id} 上报失败：{session['error']}",
                    level="warning",
                    details=[{"kind_label": "任务状态", "content": str(payload)}],
                )
                self._log(f"USRP 子任务失败：{reported_session_id} / {session['error']}")
            elif report_status == "stopped" or session.get("status") in {"cancelled", "stopped"}:
                session["status"] = "stopped"
                session["completed_at"] = str(payload.get("completed_at") or utc_now_iso())
                session["usrp_task_id"] = None
                self._record_collector_activity(
                    collector,
                    summary=f"USRP 子任务 {reported_session_id} 已停止，不再继续后续频点。",
                    level="warning",
                    details=[{"kind_label": "任务状态", "content": str(payload)}],
                )
                self._log(f"USRP 子任务停止：{reported_session_id}")
            elif self._has_next_plan_step(session):
                session["status"] = "pending"
                session["usrp_task_id"] = None
                start_next = True
                self._record_collector_activity(
                    collector,
                    summary=f"USRP 子任务 {reported_session_id} 已完成，已下载本任务 .npz，正在准备下一频点。",
                    level="normal",
                    details=[{"kind_label": "下载结果", "content": str(payload)}],
                )
                self._log(f"USRP 子任务完成：{reported_session_id}，准备启动下一频点。")
            else:
                session["status"] = "completed"
                session["completed_at"] = str(payload.get("completed_at") or utc_now_iso())
                session["usrp_task_id"] = None
                self._record_collector_activity(
                    collector,
                    summary=f"USRP 采集计划 {session_id} 已完成，已下载并解析 {session.get('files_received', 0)} 个分片。",
                    level="normal",
                    details=[{"kind_label": "下载结果", "content": str(payload)}],
                )
                self._log(f"USRP 采集计划完成：{session_id}")
            collector["active_session"] = session
            self._save_collector_state(state, collector)
        if start_next and launch_next:
            self._launch_usrp_step_thread(parent_session_id)
        return {"ok": True, "session_id": parent_session_id, "usrp_task_id": reported_session_id, "started_next": start_next}

    def _initialize(self, *, clear_existing: bool) -> None:
        self._stop_workers()
        if self.app is not None:
            self.app.close()
            self.app = None
        if clear_existing:
            for candidate in (self._db_path, Path(f"{self._db_path}-wal"), Path(f"{self._db_path}-shm")):
                if candidate.exists():
                    candidate.unlink()
            if self._uploads_root.exists():
                shutil.rmtree(self._uploads_root)
        self._uploads_root.mkdir(parents=True, exist_ok=True)

        self.app = build_app_from_env(
            place_baselines={"venue-001": ["wifi-ch1-scan", "wifi-ch6-scan", "wifi-ch11-scan"]},
            signal_knowledge={
                "wifi-ch1-scan": {"type": "wifi", "risk": "low", "note": "真实采集中的常规 2.4G WiFi 扫描记录"},
                "wifi-ch6-scan": {"type": "wifi", "risk": "low", "note": "真实采集中的常规 2.4G WiFi 扫描记录"},
                "wifi-ch11-scan": {"type": "wifi", "risk": "low", "note": "真实采集中的常规 2.4G WiFi 扫描记录"},
                "wifi-ch1-alert": {"type": "wifi", "risk": "high", "note": "真实采集中命中的疑似异常信号"},
                "wifi-ch6-alert": {"type": "wifi", "risk": "high", "note": "真实采集中命中的疑似异常信号"},
                "wifi-ch11-alert": {"type": "wifi", "risk": "high", "note": "真实采集中命中的疑似异常信号"},
            },
            db_path=self._db_path,
        )

        recent_tasks = self.app.runtime.task_repo.list_recent(limit=1)
        if recent_tasks:
            self.task = recent_tasks[0]
            self._load_platform_state()
            self._start_workers()
            self._log("已从 SQLite 恢复真实采集平台状态。")
            return

        self._logs = []
        self.task = self.app.create_task(place_id="venue-001", created_by="demo-operator")
        self._seed_chat()
        state = self.app.runtime.state_repo.get(self.task.id)
        state.metadata.setdefault(
            "collector",
            {
                "mode": "real_collector",
                "uploads_root": str(self._uploads_root),
                "evidence_root": str(self._evidence_root),
                "pending_queue": [],
                "task_session_map": {},
                "sessions": {},
                "session_order": [],
                "current_session_id": None,
                "active_session": None,
                "latest_spectrogram": None,
                "stream_info": None,
                "last_scan": None,
                "last_configure_result": None,
                "last_configure_error": None,
                "devices": [],
                "activity": [],
            },
        )
        state.metadata.setdefault("observation_history", [])
        state.metadata.setdefault("recent_observations", [])
        state.metadata.setdefault("latest_batch", {})
        state.metadata.setdefault("platform_logs", [])
        self.app.runtime.state_repo.save(state)
        self._start_workers()
        self._persist_platform_state()
        self._log("平台初始化完成。")

    def _initial_usrp_device_hint(self) -> dict[str, Any]:
        preferred_id = (os.getenv("DEEPEM_USRP_DEVICE_ID") or "").strip()
        if preferred_id:
            return {"dev_id": preferred_id, "status": "UNKNOWN", "source": "env", "usrp_stream_url": self._usrp_client.ws_url(preferred_id)}
        try:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            params = dict(collector.get("device_params") or {})
            if params.get("dev_id"):
                dev_id = str(params["dev_id"])
                return {"dev_id": dev_id, "status": "UNKNOWN", "source": "saved_params", "usrp_stream_url": self._usrp_client.ws_url(dev_id)}
            devices = list(collector.get("devices") or [])
            candidate = next((item for item in devices if item.get("dev_id") and str(item.get("status")) == "IDLE"), None)
            candidate = candidate or next((item for item in devices if item.get("dev_id")), None)
            if candidate:
                result = dict(candidate)
                result["usrp_stream_url"] = self._usrp_client.ws_url(str(result.get("dev_id")))
                return result
        except Exception:
            pass
        dev_id = "usrp-30B1FDE"
        return {"dev_id": dev_id, "status": "UNKNOWN", "source": "doc_default", "usrp_stream_url": self._usrp_client.ws_url(dev_id)}

    def _select_usrp_device(self) -> dict[str, Any]:
        preferred_id = (os.getenv("DEEPEM_USRP_DEVICE_ID") or "").strip()
        try:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            saved_params = dict(collector.get("device_params") or {})
            preferred_id = preferred_id or str(saved_params.get("dev_id") or "").strip()
        except Exception:
            saved_params = {}
        scan_payload = self._usrp_client.scan()
        scan_error = scan_payload.get("scan_error")
        devices = list(scan_payload.get("devices") or [])
        if not devices:
            devices = self._usrp_client.list_devices()
        if preferred_id:
            selected = next((item for item in devices if str(item.get("dev_id")) == preferred_id), None)
            if selected is None:
                raise UsrpApiError(status_code=404, message=f"未找到指定 USRP 设备：{preferred_id}")
        else:
            selected = next((item for item in devices if str(item.get("status")) == "IDLE"), None)
            if selected is None:
                busy = next((item for item in devices if str(item.get("status")) == "BUSY"), None)
                if busy:
                    raise UsrpApiError(status_code=409, message=f"USRP 设备 {busy.get('dev_id')} 正在采集，不能启动新任务")
            selected = selected or next((item for item in devices if str(item.get("status")) not in {"OFFLINE", "ERROR"}), None)
            if selected is None:
                extra = f"；scan_error={scan_error}" if scan_error else ""
                raise UsrpApiError(status_code=404, message="未发现 IDLE 的可用 USRP 设备" + extra)
        if str(selected.get("status") or "").upper() != "IDLE":
            raise UsrpApiError(status_code=409, message=f"USRP 设备 {selected.get('dev_id')} 当前状态为 {selected.get('status')}，只有 IDLE 才能配置/启动")
        selected = dict(selected)
        selected["usrp_stream_url"] = self._usrp_client.ws_url(str(selected.get("dev_id")))
        state = self.app.runtime.state_repo.get(self.task.id)
        collector = self._collector_meta(state)
        collector["devices"] = devices
        collector["last_scan"] = {"found_count": scan_payload.get("found_count"), "scan_error": scan_error, "updated_at": utc_now_iso()}
        if saved_params:
            saved_params["dev_id"] = selected.get("dev_id")
            collector["device_params"] = saved_params
        self._save_collector_state(state, collector)
        return selected

    def _launch_usrp_step_thread(self, session_id: str) -> None:
        def runner() -> None:
            try:
                self._run_usrp_session_steps(session_id)
            except Exception as exc:
                self._log(f"USRP 后台启动/下载线程异常：{session_id} / {exc}")
            finally:
                with self._lock:
                    current = self._usrp_threads.get(session_id)
                    if current is threading.current_thread():
                        self._usrp_threads.pop(session_id, None)

        with self._lock:
            current = self._usrp_threads.get(session_id)
            if current and current.is_alive():
                return
            thread = threading.Thread(target=runner, name=f"deepem-usrp-{session_id}", daemon=True)
            self._usrp_threads[session_id] = thread
            thread.start()

    def _run_usrp_session_steps(self, session_id: str) -> None:
        """Run configured USRP plan steps and fetch .npz files without callbacks."""
        while True:
            started = self._start_next_usrp_step(session_id)
            usrp_task_id = str(started.get("usrp_task_id") or "")
            if not usrp_task_id or started.get("status") not in {"starting", "running"}:
                return
            step = self._current_plan_step(started) or {}
            try:
                download_result = self._wait_download_and_ingest_usrp_task(
                    session_id=session_id,
                    usrp_task_id=usrp_task_id,
                    step=step,
                )
            except Exception as exc:
                self.collector_session_complete(
                    {
                        "session_id": usrp_task_id,
                        "device_id": started.get("device_id"),
                        "status": "failed",
                        "message": f"主平台下载 .npz 失败：{exc}",
                        "completed_at": utc_now_iso(),
                    },
                    launch_next=False,
                )
                raise

            completion = self.collector_session_complete(
                {
                    "session_id": usrp_task_id,
                    "device_id": started.get("device_id"),
                    "status": download_result.get("status", "completed"),
                    "message": download_result.get("message", "主平台已下载 .npz 文件"),
                    "completed_at": utc_now_iso(),
                    "file_count": download_result.get("file_count", 0),
                    "downloaded_files": download_result.get("downloaded_files", []),
                    "source": "main_platform_download",
                },
                launch_next=False,
            )
            if not completion.get("started_next"):
                return

    @staticmethod
    def _expected_npz_file_count(step: dict[str, Any]) -> int | None:
        duration = step.get("duration")
        slice_duration = step.get("slice_duration")
        if duration is None or slice_duration in {None, 0, 0.0}:
            return None
        try:
            duration_value = float(duration)
            slice_value = float(slice_duration)
        except (TypeError, ValueError):
            return None
        if duration_value <= 0 or slice_value <= 0:
            return None
        return max(1, int(math.ceil(duration_value / slice_value)))

    def _downloaded_count_for_task(self, session_id: str, usrp_task_id: str) -> int:
        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            session = collector.get("sessions", {}).get(session_id) or {}
            prefix = f"{usrp_task_id}/"
            return len([item for item in session.get("downloaded_npz_files", []) if str(item).startswith(prefix)])

    def _session_is_cancelled_or_stopped(self, session_id: str) -> bool:
        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            session = collector.get("sessions", {}).get(session_id) or {}
            return session.get("status") in {"cancelled", "stopped"}

    def _is_usrp_task_running(self, usrp_task_id: str) -> bool:
        try:
            devices = self._usrp_client.list_devices()
        except Exception as exc:
            self._log(f"查询 USRP 任务状态失败，将继续按文件下载结果判断：{usrp_task_id} / {exc}")
            return False
        for device in devices:
            if str(device.get("task_id") or "") == usrp_task_id and str(device.get("status") or "").upper() == "BUSY":
                return True
        return False

    def _download_and_ingest_usrp_task_files(self, *, session_id: str, usrp_task_id: str) -> list[str]:
        try:
            listing = self._usrp_client.list_task_files(usrp_task_id)
        except UsrpApiError as exc:
            if exc.status_code == 404:
                return []
            raise
        files = list(listing.get("files") or [])
        downloaded: list[str] = []
        for item in files:
            if isinstance(item, dict):
                raw_name = str(item.get("filename") or "")
            else:
                raw_name = str(item or "")
            source_name = Path(raw_name).name
            if not source_name.endswith(".npz"):
                continue
            stored_name = f"{usrp_task_id}/{source_name}"
            with self._lock:
                state = self.app.runtime.state_repo.get(self.task.id)
                collector = self._collector_meta(state)
                session = collector.get("sessions", {}).get(session_id)
                if not session:
                    raise KeyError(session_id)
                if session.get("status") in {"cancelled", "stopped"}:
                    return downloaded
                if stored_name in session.get("downloaded_npz_files", []):
                    continue

            content = self._usrp_client.download_task_file(usrp_task_id, source_name)

            with self._lock:
                state = self.app.runtime.state_repo.get(self.task.id)
                collector = self._collector_meta(state)
                session = collector.get("sessions", {}).get(session_id)
                if not session:
                    raise KeyError(session_id)
                if session.get("status") in {"cancelled", "stopped"}:
                    return downloaded
                if stored_name in session.get("downloaded_npz_files", []):
                    continue
                task_dir = Path(session["session_dir"]) / usrp_task_id
                task_dir.mkdir(parents=True, exist_ok=True)
                filepath = task_dir / source_name
                filepath.write_bytes(content)
                self._ingest_npz_file_locked(
                    state=state,
                    collector=collector,
                    session=session,
                    session_id=session_id,
                    reported_session_id=usrp_task_id,
                    collector_id="main-platform",
                    source_name=source_name,
                    stored_name=stored_name,
                    filepath=filepath,
                )
                session["last_npz_download_at"] = utc_now_iso()
                session["last_npz_download_source"] = self._usrp_client.base_url
                collector["active_session"] = session
                self._save_collector_state(state, collector)
                downloaded.append(stored_name)
        return downloaded

    def _wait_download_and_ingest_usrp_task(self, *, session_id: str, usrp_task_id: str, step: dict[str, Any]) -> dict[str, Any]:
        expected_count = self._expected_npz_file_count(step)
        poll_interval = max(0.2, float(os.getenv("DEEPEM_USRP_NPZ_POLL_INTERVAL_SEC") or "1.0"))
        settle_seconds = max(0.5, float(os.getenv("DEEPEM_USRP_NPZ_SETTLE_SEC") or "2.0"))
        if os.getenv("DEEPEM_USRP_NPZ_MAX_WAIT_SEC"):
            max_wait = float(os.getenv("DEEPEM_USRP_NPZ_MAX_WAIT_SEC") or "0")
        else:
            duration = float(step.get("duration") or 0.0)
            max_wait = max(60.0, duration + 60.0) if duration > 0 else 3600.0
        started_at = time.monotonic()
        last_count = -1
        stable_since: float | None = None
        self._log(f"开始轮询并下载 USRP 任务 .npz：{usrp_task_id}，预计文件数={expected_count or '未知'}")
        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            session = collector.get("sessions", {}).get(session_id)
            if session:
                self._record_collector_activity(
                    collector,
                    summary=f"主平台开始从采集服务主动下载 .npz：{usrp_task_id}",
                    level="info",
                    details=[
                        {"kind_label": "保存目录", "content": str(Path(session.get("session_dir", "")) / usrp_task_id)},
                        {"kind_label": "下载接口", "content": f"/api/usrp/tasks/{usrp_task_id}/files/{{filename}}"},
                    ],
                )
                self._save_collector_state(state, collector)

        while True:
            newly_downloaded = self._download_and_ingest_usrp_task_files(session_id=session_id, usrp_task_id=usrp_task_id)
            if newly_downloaded:
                self._log(f"USRP 任务 {usrp_task_id} 新下载 {len(newly_downloaded)} 个 .npz 分片。")
            downloaded_count = self._downloaded_count_for_task(session_id, usrp_task_id)
            if self._session_is_cancelled_or_stopped(session_id):
                return {
                    "status": "stopped",
                    "file_count": downloaded_count,
                    "downloaded_files": newly_downloaded,
                    "message": "会话已停止，主平台结束 .npz 下载轮询。",
                }
            running = self._is_usrp_task_running(usrp_task_id)
            now = time.monotonic()
            enough_files = expected_count is not None and downloaded_count >= expected_count
            if enough_files and not running:
                return {
                    "status": "completed",
                    "file_count": downloaded_count,
                    "downloaded_files": newly_downloaded,
                    "message": f"主平台已下载预期数量 .npz：{downloaded_count}/{expected_count}",
                }
            if downloaded_count > 0 and not running:
                if downloaded_count != last_count:
                    last_count = downloaded_count
                    stable_since = now
                elif stable_since is not None and now - stable_since >= settle_seconds:
                    return {
                        "status": "completed",
                        "file_count": downloaded_count,
                        "downloaded_files": newly_downloaded,
                        "message": f"USRP 任务已结束，主平台已下载稳定文件数：{downloaded_count}",
                    }
            if now - started_at > max_wait:
                if downloaded_count > 0:
                    return {
                        "status": "completed",
                        "file_count": downloaded_count,
                        "downloaded_files": newly_downloaded,
                        "message": f"等待 .npz 超过上限，已下载 {downloaded_count} 个文件，继续后续流程。",
                    }
                raise TimeoutError(f"等待 USRP 任务 {usrp_task_id} 生成 .npz 超时")
            time.sleep(poll_interval)

    def _load_usrp_plan(self, overrides: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        raw_plan = (os.getenv("DEEPEM_USRP_PLAN_JSON") or "").strip()
        if raw_plan:
            payload = json.loads(raw_plan)
            if not isinstance(payload, list):
                raise ValueError("DEEPEM_USRP_PLAN_JSON must be a JSON array")
            return [self._normalize_usrp_plan_step(item, index) for index, item in enumerate(payload)]

        ov = dict(self.get_device_params())
        ov.update(dict(overrides or {}))
        sample_rate = self._mhz_value(ov.get("sample_rate") or os.getenv("DEEPEM_USRP_SAMPLE_RATE") or "1000000")
        bandwidth = self._mhz_value(ov.get("bandwidth") or os.getenv("DEEPEM_USRP_BANDWIDTH") or "1000000")
        gain = float(ov.get("gain") or os.getenv("DEEPEM_USRP_GAIN") or "40")
        slice_duration = float(ov.get("slice_duration") or os.getenv("DEEPEM_USRP_SLICE_DURATION") or "1")
        duration = None if ov.get("duration") is None else float(ov.get("duration") or os.getenv("DEEPEM_USRP_STEP_DURATION") or "3")
        antenna = ov.get("antenna") or (os.getenv("DEEPEM_USRP_ANTENNA") or "").strip() or None
        usrp_channel = int(ov.get("channel")) if ov.get("channel") is not None else int(os.getenv("DEEPEM_USRP_CHANNEL") or "0")

        user_freq = ov.get("freq")
        if user_freq is not None:
            freq = float(user_freq)
            return [
                self._normalize_usrp_plan_step(
                    {
                        "step_id": "user_configured",
                        "label": f"用户配置频点 {self._mhz_value(freq):.6g} MHz",
                        "channel": 1,
                        "freq": self._mhz_value(freq),
                        "sample_rate": sample_rate,
                        "bandwidth": bandwidth,
                        "gain": gain,
                        "slice_duration": slice_duration,
                        "duration": duration,
                        "antenna": antenna,
                        "usrp_channel": usrp_channel,
                    },
                    0,
                )
            ]

        return [
            self._normalize_usrp_plan_step(
                {
                    "step_id": "wifi_ch1",
                    "label": "2412 MHz WiFi channel 1",
                    "channel": 1,
                    "freq": 2412.0,
                    "sample_rate": sample_rate,
                    "bandwidth": bandwidth,
                    "gain": gain,
                    "slice_duration": slice_duration,
                    "duration": duration,
                    "antenna": antenna,
                    "usrp_channel": usrp_channel,
                },
                0,
            ),
            self._normalize_usrp_plan_step(
                {
                    "step_id": "wifi_ch6",
                    "label": "2437 MHz WiFi channel 6",
                    "channel": 6,
                    "freq": 2437.0,
                    "sample_rate": sample_rate,
                    "bandwidth": bandwidth,
                    "gain": gain,
                    "slice_duration": slice_duration,
                    "duration": duration,
                    "antenna": antenna,
                    "usrp_channel": usrp_channel,
                },
                1,
            ),
            self._normalize_usrp_plan_step(
                {
                    "step_id": "wifi_ch11",
                    "label": "2462 MHz WiFi channel 11",
                    "channel": 11,
                    "freq": 2462.0,
                    "sample_rate": sample_rate,
                    "bandwidth": bandwidth,
                    "gain": gain,
                    "slice_duration": slice_duration,
                    "duration": duration,
                    "antenna": antenna,
                    "usrp_channel": usrp_channel,
                },
                2,
            ),
        ]

    @staticmethod
    def _normalize_usrp_plan_step(item: dict[str, Any], index: int) -> dict[str, Any]:
        step = dict(item)
        step["step_index"] = index
        step["step_id"] = str(step.get("step_id") or f"step_{index + 1:02d}")
        step["label"] = str(step.get("label") or step["step_id"])
        step["freq"] = DemoPlatform._mhz_value(step["freq"])
        step["sample_rate"] = DemoPlatform._mhz_value(step["sample_rate"])
        step["bandwidth"] = DemoPlatform._mhz_value(step["bandwidth"])
        step["gain"] = float(step["gain"])
        step["slice_duration"] = float(step["slice_duration"])
        if step.get("duration") is not None:
            step["duration"] = float(step["duration"])
        if step.get("antenna") in {"", "null", "None"}:
            step["antenna"] = None
        step["channel"] = int(step.get("channel") or index + 1)
        step["usrp_channel"] = int(step.get("usrp_channel") or 0)
        return step

    def _start_next_usrp_step(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            session = collector["sessions"].get(session_id)
            if not session:
                raise KeyError(session_id)
            if session.get("status") == "cancelled":
                return session
            plan = list(session.get("acquisition_plan") or [])
            next_index = int(session.get("current_plan_index", -1)) + 1
            if next_index >= len(plan):
                session["status"] = "completed"
                session["completed_at"] = utc_now_iso()
                collector["active_session"] = session
                self._save_collector_state(state, collector)
                return session
            step = dict(plan[next_index])
            task_id = f"{session_id}_step{next_index + 1:02d}"
            config_payload = self._build_usrp_config_payload(
                {
                    "dev_id": session["device_id"],
                    "freq": step["freq"],
                    "sample_rate": step["sample_rate"],
                    "bandwidth": step["bandwidth"],
                    "gain": step["gain"],
                    "slice_duration": step["slice_duration"],
                    "duration": step.get("duration"),
                    "antenna": step.get("antenna"),
                }
            )
            request_payload = dict(config_payload)
            request_payload["task_id"] = task_id
            session["status"] = "starting"
            session["current_plan_index"] = next_index
            session["current_plan_step"] = step
            session["usrp_task_id"] = task_id
            session["last_configure_request"] = config_payload
            session["last_start_request"] = request_payload
            collector.setdefault("task_session_map", {})[task_id] = session_id
            collector["active_session"] = session
            self._record_collector_activity(
                collector,
                summary=f"正在调用 USRP 启动接口：{task_id} / {step['label']}。",
                level="info",
                details=[
                    {"kind_label": "频点", "content": f"{step['freq']} MHz"},
                    {"kind_label": "采样率", "content": f"{step['sample_rate']} MHz"},
                    {"kind_label": "说明", "content": "平台会先调用 configure 校验/保存参数，再调用 start 提交完整任务参数；设备平台尚未返回时页面会保持 starting 状态并继续轮询。"},
                ],
            )
            self._save_collector_state(state, collector)

        try:
            configure_response = self._usrp_client.configure(config_payload)
            start_response = self._usrp_client.start(request_payload)
        except Exception as exc:
            with self._lock:
                state = self.app.runtime.state_repo.get(self.task.id)
                collector = self._collector_meta(state)
                session = collector["sessions"].get(session_id)
                if session:
                    session["status"] = "error"
                    session["error"] = str(exc)
                    session["completed_at"] = utc_now_iso()
                    collector["active_session"] = session
                    self._record_collector_activity(
                        collector,
                        summary=f"USRP 启动接口失败：{task_id} / {exc}",
                        level="warning",
                        details=[{"kind_label": "请求", "content": str(request_payload)}],
                    )
                    self._save_collector_state(state, collector)
            self._log(f"USRP 采集启动失败：{exc}")
            raise

        with self._lock:
            state = self.app.runtime.state_repo.get(self.task.id)
            collector = self._collector_meta(state)
            session = collector["sessions"][session_id]
            if session.get("status") == "cancelled":
                self._record_collector_activity(
                    collector,
                    summary=f"USRP 启动接口已返回，但会话 {session_id} 已取消。",
                    level="warning",
                    details=[
                    {"kind_label": "configure响应", "content": str(configure_response)},
                    {"kind_label": "start响应", "content": str(start_response)},
                    {"kind_label": "WebSocket", "content": session.get("usrp_stream_url", "")},
                ],
                )
                self._save_collector_state(state, collector)
                return session
            session["status"] = "running"
            session["started_at"] = session.get("started_at") or utc_now_iso()
            session["last_configure_response"] = configure_response
            session["last_start_response"] = start_response
            session.setdefault("usrp_tasks", []).append(
                {
                    "usrp_task_id": task_id,
                    "step": step,
                    "started_at": utc_now_iso(),
                    "response": start_response,
                }
            )
            collector["active_session"] = session
            self._record_collector_activity(
                collector,
                summary=f"USRP 已接受采集任务：{task_id}，主平台将轮询并下载 .npz 分片。",
                level="info",
                details=[
                    {"kind_label": "configure响应", "content": str(configure_response)},
                    {"kind_label": "start响应", "content": str(start_response)},
                    {"kind_label": "WebSocket", "content": session.get("usrp_stream_url", "")},
                ],
            )
            self._save_collector_state(state, collector)
            self._log(f"USRP 频点采集已启动：{task_id} / {step['label']} / {step['freq']} MHz")
            return session

    def _resolve_parent_session_id(self, collector: dict[str, Any], reported_session_id: str) -> str:
        if reported_session_id in (collector.get("sessions") or {}):
            return reported_session_id
        return str((collector.get("task_session_map") or {}).get(reported_session_id) or reported_session_id)

    @staticmethod
    def _current_plan_step(session: dict[str, Any]) -> dict[str, Any] | None:
        step = session.get("current_plan_step")
        return dict(step) if isinstance(step, dict) else None

    @staticmethod
    def _has_next_plan_step(session: dict[str, Any]) -> bool:
        return int(session.get("current_plan_index", -1)) + 1 < len(session.get("acquisition_plan") or [])

    @staticmethod
    def _usrp_stop_info(session: dict[str, Any]) -> dict[str, str] | None:
        task_id = str(session.get("usrp_task_id") or "")
        dev_id = str(session.get("device_id") or "")
        if not task_id or not dev_id:
            return None
        return {"task_id": task_id, "dev_id": dev_id}

    def _stop_usrp_task_safe(self, stop_info: dict[str, str]) -> dict[str, Any]:
        try:
            payload = self._usrp_client.stop(dev_id=stop_info["dev_id"], task_id=stop_info["task_id"])
            self._log(f"USRP 任务已停止：{stop_info['task_id']}")
            return {"ok": True, "payload": payload}
        except Exception as exc:
            self._log(f"USRP 停止请求失败：{stop_info['task_id']} / {exc}")
            return {"ok": False, "error": str(exc)}

    def _build_signal_evidence_document(
        self,
        *,
        event_id: str,
        observation: dict[str, Any],
        analysis: dict[str, Any],
        meta: dict[str, Any],
        filepath: Path,
        raw_sample_count: int,
    ) -> dict[str, Any]:
        document_id = f"{event_id}:{observation['signal_id']}"
        return {
            "document_id": document_id,
            "event_id": event_id,
            "signal_id": observation["signal_id"],
            "batch_id": observation["batch_id"],
            "session_id": str(meta.get("session_id", "")),
            "usrp_task_id": str(meta.get("usrp_task_id", "")),
            "collector_id": str(meta.get("collector_id", "")),
            "device_id": str(meta.get("device_id", "")),
            "file_name": filepath.name,
            "file_path": str(filepath),
            "classification": observation.get("classification"),
            "fingerprint": observation.get("fingerprint"),
            "channel": int(observation.get("channel") or 0),
            "center_freq_mhz": self._mhz_value(meta.get("center_freq") or meta.get("freq") or 0.0),
            "sample_rate_mhz": self._mhz_value(meta.get("sample_rate") or 0.0),
            "bandwidth_mhz": self._mhz_value(meta.get("bandwidth") or 0.0),
            "gain": float(meta.get("gain") or 0.0),
            "antenna": meta.get("antenna"),
            "raw_sample_count": raw_sample_count,
            "score": float(observation.get("score") or analysis.get("score") or 0.0),
            "analysis": dict(analysis),
            "features": dict(observation),
            "created_at": utc_now_iso(),
        }

    def _record_observation(
        self,
        *,
        state,
        collector: dict[str, Any],
        session: dict[str, Any],
        event_id: str,
        observation: dict[str, Any],
        analysis: dict[str, Any],
        spectrogram: dict[str, Any],
        meta: dict[str, Any],
        filepath: Path,
        raw_sample_count: int,
    ) -> None:
        observation_history = list(state.metadata.get("observation_history", []))
        observation_history.append(observation)
        state.metadata["observation_history"] = observation_history[-1000:]
        state.metadata["recent_observations"] = state.metadata["observation_history"][-50:]
        is_alert = bool(analysis.get("alert") or observation.get("classification") == "suspicious")
        state.metadata["latest_batch"] = {
            "batch_id": observation["batch_id"],
            "file_name": filepath.name,
            "raw_sample_count": raw_sample_count,
            "has_anomaly": is_alert,
            "signal_id": observation["signal_id"],
            "channel": observation["channel"],
            "freq": observation["freq"],
            "session_id": str(meta.get("session_id", "")),
            "collector_id": str(meta.get("collector_id", "")),
            "timestamp": observation["timestamp"],
        }
        collector["latest_spectrogram"] = spectrogram
        collector["current_session_id"] = str(meta.get("session_id", ""))
        collector["active_session"] = session
        collector["last_upload_at"] = utc_now_iso()

        session["files_received"] = int(session.get("files_received", 0)) + 1
        session["collector_id"] = str(meta.get("collector_id", ""))
        session.setdefault("latest_results", {})[str(analysis["channel"])] = analysis
        session.setdefault("events", []).append(
            {
                "event_id": event_id,
                "signal_id": observation["signal_id"],
                "channel": observation["channel"],
                "timestamp": observation["timestamp"],
                "score": observation["score"],
                "alert": is_alert,
                "file_name": filepath.name,
            }
        )
        session["events"] = session["events"][-200:]
        if is_alert:
            session.setdefault("alerts", []).append(session["events"][-1])
            session["alerts"] = session["alerts"][-50:]
        session.setdefault("files", []).append(str(filepath))
        session["files"] = session["files"][-200:]

        evidence_document = self._build_signal_evidence_document(
            event_id=event_id,
            observation=observation,
            analysis=analysis,
            meta=meta,
            filepath=filepath,
            raw_sample_count=raw_sample_count,
        )
        evidence_result = self._evidence_store.index_signal_evidence(
            document_id=str(evidence_document["document_id"]),
            document=evidence_document,
        )
        evidence_document["evidence_store"] = {
            "status": evidence_result.status,
            "target": evidence_result.target,
            "error": evidence_result.error,
        }
        state.metadata.setdefault("signal_evidence", []).append(evidence_document)
        state.metadata["signal_evidence"] = state.metadata["signal_evidence"][-500:]
        session.setdefault("evidence", []).append(evidence_document)
        session["evidence"] = session["evidence"][-200:]
        self._record_collector_activity(
            collector,
            summary=f"收到 USRP 分片 {filepath.name}，已生成结构化事件 {event_id} 并完成证据元信息落库。",
            level="warning" if is_alert else "normal",
            details=[
                {"kind_label": "信号", "content": observation["signal_id"]},
                {"kind_label": "频点", "content": f"{self._mhz_value(observation['freq'])} MHz"},
                {"kind_label": "证据", "content": str(evidence_document["evidence_store"])},
            ],
        )

        self._inject_platform_metadata(state)
        self.app.runtime.state_repo.save(state)

    def _maybe_complete_session(self, *, session: dict[str, Any], collector: dict[str, Any]) -> bool:
        if session.get("status") == "completed":
            return False

        expected_file_count = session.get("expected_file_count")
        if expected_file_count is not None:
            if int(session.get("files_received", 0)) < int(expected_file_count):
                return False
        else:
            return False

        session["status"] = "completed"
        session["completed_at"] = utc_now_iso()
        collector["pending_queue"] = [item for item in collector.get("pending_queue", []) if item != session.get("session_id")]
        collector["active_session"] = session
        self._log(f"采集任务自动完成：{session.get('session_id')}（已收到本轮全部信道数据）")
        return True

    def _create_capture_session(
        self,
        *,
        device: dict[str, Any],
        acquisition_plan: list[dict[str, Any]],
        source_mode: str = "real",
        expected_file_count: int | None = None,
    ) -> dict[str, Any]:
        state = self.app.runtime.state_repo.get(self.task.id)
        collector = self._collector_meta(state)
        session_id = uuid.uuid4().hex[:12]
        session_dir = self._uploads_root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        channels_to_scan = [int(step.get("channel") or 0) for step in acquisition_plan]
        duration_sec = sum(float(step.get("duration") or step.get("slice_duration") or 0.0) for step in acquisition_plan)
        usrp_stream_url = self._usrp_client.ws_url(str(device.get("dev_id") or "")) if device.get("dev_id") else ""
        session = {
            "session_id": session_id,
            "status": "pending",
            "created_at": utc_now_iso(),
            "started_at": None,
            "completed_at": None,
            "duration_sec": float(duration_sec),
            "channels_to_scan": list(channels_to_scan),
            "source_mode": source_mode,
            "device_id": str(device.get("dev_id") or ""),
            "device": device,
            "usrp_stream_url": usrp_stream_url,
            "usrp_stream": {"ws_url": usrp_stream_url, "fft_size": 1024},
            "acquisition_plan": acquisition_plan,
            "current_plan_index": -1,
            "current_plan_step": None,
            "usrp_task_id": None,
            "usrp_tasks": [],
            "usrp_completed_tasks": [],
            "downloaded_npz_files": [],
            "expected_file_count": int(expected_file_count) if expected_file_count is not None else None,
            "collector_id": None,
            "files_received": 0,
            "session_dir": str(session_dir),
            "latest_results": {},
            "events": [],
            "alerts": [],
            "files": [],
            "analyzer_state": SessionAnalyzer(channels_to_scan).to_payload(),
        }
        collector["mode"] = "real_collector"
        collector["devices"] = [device]
        collector["sessions"][session_id] = session
        collector["session_order"] = (collector.get("session_order", []) + [session_id])[-50:]
        collector["current_session_id"] = session_id
        collector["active_session"] = session
        collector["stream_info"] = session.get("usrp_stream")
        self._save_collector_state(state, collector)
        return session

    def _active_session(self) -> dict[str, Any] | None:
        state = self.app.runtime.state_repo.get(self.task.id)
        collector = self._collector_meta(state)
        current_id = collector.get("current_session_id")
        if current_id:
            return collector.get("sessions", {}).get(current_id)
        return None

    def _cancel_active_session_locked(self, active: dict[str, Any], *, reason: str) -> dict[str, Any]:
        state = self.app.runtime.state_repo.get(self.task.id)
        collector = self._collector_meta(state)
        session_id = str(active.get("session_id") or "")
        collector["pending_queue"] = [item for item in collector.get("pending_queue", []) if item != session_id]
        active["status"] = "cancelled"
        active["completed_at"] = utc_now_iso()
        active["cancel_reason"] = reason
        collector["active_session"] = active
        if session_id:
            collector.setdefault("sessions", {})[session_id] = active
        self._record_collector_activity(
            collector,
            summary=f"采集会话 {session_id} 已请求停止。",
            level="warning",
            details=[{"kind_label": "原因", "content": reason}],
        )
        self._save_collector_state(state, collector)
        self._log(f"已停止采集任务：{session_id}")
        return active

    def _cancel_detection_reviews_locked(self) -> dict[str, Any]:
        queued = 0
        while True:
            try:
                self._detection_queue.get_nowait()
            except queue.Empty:
                break
            queued += 1
            self._detection_queue.task_done()
        running = bool(self._active_detection_job)
        if self._active_detection_cancel is not None:
            self._active_detection_cancel.set()
        if queued or running:
            self._log(f"已请求停止大模型研判：运行中 {1 if running else 0}，清空排队 {queued}")
        return {"queued": queued, "running": running}

    @staticmethod
    def _record_collector_activity(
        collector: dict[str, Any],
        *,
        summary: str,
        level: str = "info",
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        item = {
            "timestamp": utc_now_iso(),
            "event_type": "collector.activity",
            "event_type_label": "USRP采集",
            "agent": "采集控制",
            "summary": summary,
            "level": level,
            "details": list(details or []),
            "normal_evidence": [],
            "expandable": bool(details),
        }
        collector.setdefault("activity", []).append(item)
        collector["activity"] = collector["activity"][-200:]

    def _collector_meta(self, state) -> dict[str, Any]:
        return state.metadata.setdefault(
            "collector",
            {
                "mode": "real_collector",
                "uploads_root": str(self._uploads_root),
                "evidence_root": str(self._evidence_root),
                "pending_queue": [],
                "task_session_map": {},
                "sessions": {},
                "session_order": [],
                "current_session_id": None,
                "active_session": None,
                "latest_spectrogram": None,
                "stream_info": None,
                "last_scan": None,
                "last_configure_result": None,
                "last_configure_error": None,
                "devices": [],
                "activity": [],
            },
        )

    def _save_collector_state(self, state, collector: dict[str, Any]) -> None:
        state.metadata["collector"] = collector
        self._inject_platform_metadata(state)
        self.app.runtime.state_repo.save(state)

    def _detection_loop(self) -> None:
        while not self._detection_stop.is_set():
            try:
                job = self._detection_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            cancel_event = threading.Event()
            try:
                with self._lock:
                    self._active_detection_job = job
                    self._active_detection_cancel = cancel_event
                self._log(f"异常复核智能体开始处理事件：{job['event_id']}")
                self.app.run_engine.run_event_with_cancel(
                    job["task_id"],
                    job["event_id"],
                    cancel_checker=lambda: cancel_event.is_set() or self._detection_stop.is_set(),
                )
            except Exception as exc:
                self._log(f"检测线程异常：{exc}")
            finally:
                with self._lock:
                    if self._active_detection_job is job:
                        self._active_detection_job = None
                    if self._active_detection_cancel is cancel_event:
                        self._active_detection_cancel = None
                self._detection_queue.task_done()

    def _chat_loop(self) -> None:
        while not self._chat_stop.is_set():
            try:
                job = self._chat_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._log(f"对话智能体开始处理消息：{job['message_id']}")
                self.app.chat_service.process_message(task_id=job["task_id"], message_id=job["message_id"])
            except Exception as exc:
                self._log(f"聊天线程异常：{exc}")
            finally:
                self._chat_queue.task_done()

    def _start_workers(self) -> None:
        self._detection_stop.clear()
        self._chat_stop.clear()
        self._detection_thread = threading.Thread(target=self._detection_loop, name="deepem-detection", daemon=True)
        self._chat_thread = threading.Thread(target=self._chat_loop, name="deepem-chat", daemon=True)
        self._detection_thread.start()
        self._chat_thread.start()

    def _stop_workers(self) -> None:
        self._detection_stop.set()
        self._chat_stop.set()
        if self._active_detection_cancel is not None:
            self._active_detection_cancel.set()
        for thread in (self._detection_thread, self._chat_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=1.5)
        self._detection_thread = None
        self._chat_thread = None
        self._detection_queue = queue.Queue()
        self._chat_queue = queue.Queue()

    def _seed_chat(self) -> None:
        self.app.append_chat(task_id=self.task.id, content="您好，我是 DeepEM 问答智能体。", role=ChatRole.ASSISTANT)

    def _load_platform_state(self) -> None:
        try:
            state = self.app.runtime.state_repo.get(self.task.id)
        except KeyError:
            self._logs = []
            return
        self._logs = list(state.metadata.get("platform_logs", []))[-500:]
        collector = self._collector_meta(state)
        collector["mode"] = "real_collector"
        collector.pop("demo_data_root", None)
        collector.setdefault("uploads_root", str(self._uploads_root))
        collector.setdefault("evidence_root", str(self._evidence_root))
        collector.setdefault("sessions", {})
        collector.setdefault("session_order", [])
        collector.setdefault("pending_queue", [])
        collector.setdefault("task_session_map", {})
        collector.setdefault("devices", [])
        collector.setdefault("stream_info", None)
        collector.setdefault("last_scan", None)
        collector.setdefault("last_configure_result", None)
        collector.setdefault("last_configure_error", None)
        collector.setdefault("activity", [])
        collector.setdefault("stream_info", None)
        state.metadata["collector"] = collector
        self._sync_place_baseline_from_state(state)
        self.app.runtime.state_repo.save(state)

    def _persist_platform_state(self) -> None:
        if self.app is None or self.task is None:
            return
        try:
            state = self.app.runtime.state_repo.get(self.task.id)
        except KeyError:
            return
        self._inject_platform_metadata(state)
        self.app.runtime.state_repo.save(state)

    def _inject_platform_metadata(self, state) -> None:
        state.metadata["platform_logs"] = self._logs[-500:]
        collector = self._collector_meta(state)
        collector["mode"] = "real_collector"
        collector.pop("demo_data_root", None)
        collector["uploads_root"] = str(self._uploads_root)
        collector["evidence_root"] = str(self._evidence_root)
        collector.setdefault("activity", [])
        collector.setdefault("stream_info", None)
        state.metadata["collector"] = collector

    def _sync_place_baseline_from_state(self, state) -> None:
        baseline = state.metadata.get("place_baseline")
        if not isinstance(baseline, dict) or not baseline.get("initialized"):
            return
        place_id = str(baseline.get("place_id") or state.place_id)
        fingerprints = [str(item).strip() for item in baseline.get("fingerprints", []) if str(item).strip()]
        setter = getattr(self.app.runtime.knowledge_base, "set_place_baseline", None)
        if callable(setter):
            setter(place_id, fingerprints)

    def _log(self, message: str) -> None:
        self._logs.append(f"[{utc_now_iso()}] {message}")
        self._persist_platform_state()
