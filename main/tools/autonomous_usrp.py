from __future__ import annotations

import ast
import json
import os
import re
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from deepem.devices.usrp import UsrpApiError, UsrpClient
from deepem.protocol import ToolResult
from deepem.tools.base import ToolContext, ToolDefinition, ToolExecutionResult


KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"
USRP_API_DOC = KNOWLEDGE_DIR / "usrp_api.md"
AUTONOMOUS_OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "data" / "autonomous_usrp"


class AutonomousUsrpError(RuntimeError):
    """代码生成型 USRP 智能体工具链的统一异常。"""


@dataclass(slots=True)
class KnowledgeChunk:
    title: str
    text: str
    score: int


class MarkdownKnowledgeStore:
    """把内置 Markdown 文档切成小块，用简单关键词检索作为本地知识库。"""

    def __init__(self, doc_path: Path = USRP_API_DOC) -> None:
        self.doc_path = doc_path
        self._chunks: list[KnowledgeChunk] | None = None

    def search(self, query: str, top_k: int = 6) -> list[KnowledgeChunk]:
        chunks = self._load_chunks()
        terms = self._terms(query)
        if not terms:
            return chunks[:top_k]
        ranked: list[KnowledgeChunk] = []
        for item in chunks:
            haystack = f"{item.title}\n{item.text}".lower()
            score = 0
            for term in terms:
                lowered = term.lower()
                score += haystack.count(lowered) * (4 if lowered in item.title.lower() else 1)
            if score > 0:
                ranked.append(KnowledgeChunk(title=item.title, text=item.text, score=score))
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:top_k] or chunks[:top_k]

    def read_all(self, limit: int = 16000) -> str:
        if not self.doc_path.exists():
            return ""
        text = self.doc_path.read_text(encoding="utf-8", errors="replace")
        return text[:limit]

    def _load_chunks(self) -> list[KnowledgeChunk]:
        if self._chunks is not None:
            return self._chunks
        if not self.doc_path.exists():
            self._chunks = []
            return self._chunks
        raw = self.doc_path.read_text(encoding="utf-8", errors="replace")
        sections: list[KnowledgeChunk] = []
        current_title = "USRP API 文档"
        current_lines: list[str] = []
        for line in raw.splitlines():
            if line.startswith("## ") and current_lines:
                sections.append(KnowledgeChunk(current_title, "\n".join(current_lines).strip(), 0))
                current_title = line.strip("# ").strip()
                current_lines = [line]
            else:
                if line.startswith("## "):
                    current_title = line.strip("# ").strip()
                current_lines.append(line)
        if current_lines:
            sections.append(KnowledgeChunk(current_title, "\n".join(current_lines).strip(), 0))
        self._chunks = [item for item in sections if item.text]
        return self._chunks

    @staticmethod
    def _terms(query: str) -> list[str]:
        raw = re.findall(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]{2,}", query or "")
        stop = {"的", "和", "或者", "以及", "采集", "任务", "智能体"}
        terms: list[str] = []
        for item in raw:
            item = item.strip()
            if len(item) < 2 or item in stop:
                continue
            if item not in terms:
                terms.append(item)
        return terms[:16]


@dataclass(slots=True)
class SafeExecutionLogger:
    stream_handler: Callable[[str, dict[str, Any]], None] | None
    run_id: str
    logs: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, stage: str, message: str, data: dict[str, Any] | None = None) -> None:
        payload = {
            "stage": stage,
            "message": message,
            "data": data or {},
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.logs.append(payload)
        if self.stream_handler is not None:
            self.stream_handler(
                "tool_result",
                {
                    "run_id": self.run_id,
                    "tool_name": "autonomous_usrp_progress",
                    "data": payload,
                },
            )


class SafeUsrpRuntime:
    """给智能体生成代码使用的受控 USRP SDK。

    本版不再读取远端 slice_*.npz 文件。自主采集逻辑直接通过 USRP WebSocket
    返回的实时 fft_data 完成多频点、多重复次数平均和最终结果保存。
    """

    def __init__(
        self,
        *,
        logger: SafeExecutionLogger,
        dry_run: bool = False,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> None:
        self.client = UsrpClient.from_env()
        self.logger = logger
        self.dry_run = bool(dry_run or os.getenv("DEEPEM_AUTONOMOUS_USRP_FORCE_DRY_RUN") == "1")
        self.cancel_checker = cancel_checker
        self.default_dev_id = (os.getenv("DEEPEM_USRP_DEVICE_ID") or "usrp-30B1FDE").strip()
        self.default_sample_rate = float(os.getenv("DEEPEM_USRP_SAMPLE_RATE") or "1000000")
        self.default_bandwidth = float(os.getenv("DEEPEM_USRP_BANDWIDTH") or "1000000")
        self.default_gain = float(os.getenv("DEEPEM_USRP_GAIN") or "40")
        self.default_antenna = (os.getenv("DEEPEM_USRP_ANTENNA") or "RX2").strip() or None
        self.expected_fft_frames = int(os.getenv("DEEPEM_AUTONOMOUS_USRP_EXPECTED_FFT_FRAMES") or "1")
        self.frame_timeout_sec = float(os.getenv("DEEPEM_AUTONOMOUS_USRP_FRAME_TIMEOUT_SEC") or "1.5")
        self.completed_tasks: list[dict[str, Any]] = []

    def check_cancelled(self) -> None:
        if self.cancel_checker is not None and self.cancel_checker():
            raise AutonomousUsrpError("用户已停止本轮自主采集任务")

    def generate_frequency_list(self, start_hz: float, stop_hz: float, step_hz: float, *, include_stop: bool = True) -> list[float]:
        start = float(start_hz)
        stop = float(stop_hz)
        step = float(step_hz)
        if step <= 0:
            raise ValueError("step_hz 必须大于 0")
        if stop < start:
            raise ValueError("stop_hz 必须大于等于 start_hz")
        freqs: list[float] = []
        current = start
        # 防止智能体生成异常循环。
        for _ in range(20000):
            if current > stop + 1e-6:
                break
            freqs.append(float(current))
            current += step
        if include_stop and freqs and abs(freqs[-1] - stop) > max(1.0, step * 1e-9) and freqs[-1] < stop:
            freqs.append(stop)
        if len(freqs) > int(os.getenv("DEEPEM_AUTONOMOUS_USRP_MAX_FREQS") or "500"):
            raise ValueError(f"频点数量过多：{len(freqs)}，请增大步长或缩小范围")
        return freqs

    def make_task_id(self, prefix: str = "auto_usrp", **parts: Any) -> str:
        safe_parts = [prefix]
        for key, value in parts.items():
            text = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(value))[:32]
            safe_parts.append(f"{key}-{text}")
        safe_parts.append(uuid.uuid4().hex[:8])
        return "_".join(safe_parts)[:160]

    def scan_and_get_idle_device(self, dev_id: str | None = None) -> dict[str, Any]:
        self.check_cancelled()
        target = str(dev_id or self.default_dev_id or "").strip()
        if self.dry_run:
            device = {
                "dev_id": target or "usrp-mock",
                "status": "IDLE",
                "task_id": None,
                "dev_config": {
                    "freq_range": {"min": 42_000_000.0, "max": 6_008_000_000.0},
                    "sample_rate_range": {"min": 31_250.0, "max": 16_000_000.0},
                    "bandwidth_range": {"min": 200_000.0, "max": 56_000_000.0},
                    "gain_range": {"min": 0.0, "max": 76.0},
                    "rx_antennas": ["TX/RX", "RX2"],
                },
            }
            self.logger.emit("scan", "Dry-run：已返回模拟 IDLE 设备", {"device": device})
            return device
        payload = self.client.scan()
        if payload.get("scan_error"):
            raise AutonomousUsrpError(f"USRP scan_error: {payload.get('scan_error')}")
        devices = list(payload.get("devices") or [])
        candidates = [item for item in devices if str(item.get("status") or "").upper() == "IDLE"]
        if target:
            selected = next((item for item in candidates if str(item.get("dev_id") or "") == target), None)
            selected = selected or next((item for item in devices if str(item.get("dev_id") or "") == target), None)
        else:
            selected = candidates[0] if candidates else (devices[0] if devices else None)
        if not selected:
            raise AutonomousUsrpError("未发现 USRP 设备")
        if str(selected.get("status") or "").upper() != "IDLE":
            raise AutonomousUsrpError(f"目标设备不是 IDLE：{selected.get('dev_id')} / {selected.get('status')}")
        self.logger.emit("scan", "已扫描到可用 USRP 设备", {"device": selected, "found_count": payload.get("found_count")})
        return dict(selected)

    def list_devices(self) -> list[dict[str, Any]]:
        self.check_cancelled()
        if self.dry_run:
            return [self.scan_and_get_idle_device()]
        return self.client.list_devices()

    def validate_capture_params(
        self,
        *,
        device: dict[str, Any],
        freq: float,
        sample_rate: float,
        bandwidth: float,
        gain: float,
        antenna: str | None,
    ) -> None:
        cfg = dict(device.get("dev_config") or {})

        def rng(name: str, default_min: float, default_max: float) -> tuple[float, float]:
            raw = cfg.get(name) or {}
            return float(raw.get("min", default_min)), float(raw.get("max", default_max))

        f_min, f_max = rng("freq_range", 42_000_000.0, 6_008_000_000.0)
        sr_min, sr_max = rng("sample_rate_range", 31_250.0, 16_000_000.0)
        bw_min, bw_max = rng("bandwidth_range", 200_000.0, 56_000_000.0)
        g_min, g_max = rng("gain_range", 0.0, 76.0)
        if not (f_min <= float(freq) <= f_max):
            raise ValueError(f"freq 超出设备范围：{freq} not in [{f_min}, {f_max}]")
        if not (sr_min <= float(sample_rate) <= sr_max):
            raise ValueError(f"sample_rate 超出设备范围：{sample_rate} not in [{sr_min}, {sr_max}]")
        if not (bw_min <= float(bandwidth) <= bw_max):
            raise ValueError(f"bandwidth 超出设备范围：{bandwidth} not in [{bw_min}, {bw_max}]")
        if float(bandwidth) > float(sample_rate):
            raise ValueError("bandwidth 不能大于 sample_rate")
        if not (g_min <= float(gain) <= g_max):
            raise ValueError(f"gain 超出设备范围：{gain} not in [{g_min}, {g_max}]")
        antennas = [str(item) for item in cfg.get("rx_antennas") or ["TX/RX", "RX2"]]
        if antenna is not None and str(antenna) not in antennas:
            raise ValueError(f"antenna 不在设备支持列表中：{antenna} not in {antennas}")

    def start_capture(
        self,
        *,
        task_id: str,
        dev_id: str,
        freq: float,
        sample_rate: float | None = None,
        bandwidth: float | None = None,
        gain: float | None = None,
        slice_duration: float = 0.1,
        duration: float | None = 0.1,
        antenna: str | None = None,
        device: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """启动一次 USRP 采集。注意：本方法只启动任务，不读取远端 npz 文件。"""
        self.check_cancelled()
        task_id = str(task_id or "").strip()
        dev_id = str(dev_id or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        if not dev_id:
            raise ValueError("dev_id is required")
        sample_rate = float(sample_rate or self.default_sample_rate)
        bandwidth = float(bandwidth or self.default_bandwidth)
        gain = float(self.default_gain if gain is None else gain)
        antenna = self.default_antenna if antenna is None else antenna
        if float(slice_duration) <= 0:
            raise ValueError("slice_duration 必须大于 0")
        if duration is not None and float(duration) <= 0:
            raise ValueError("duration 必须大于 0 或为 None")
        device = device or {"dev_id": dev_id, "dev_config": {}}
        self.validate_capture_params(device=device, freq=freq, sample_rate=sample_rate, bandwidth=bandwidth, gain=gain, antenna=antenna)
        request = {
            "task_id": str(task_id),
            "dev_id": str(dev_id),
            "freq": float(freq),
            "sample_rate": sample_rate,
            "bandwidth": bandwidth,
            "gain": gain,
            "slice_duration": float(slice_duration),
            "duration": None if duration is None else float(duration),
            "antenna": antenna,
        }
        if self.dry_run:
            response = {"status": "DRY_RUN", "task_id": task_id, "dev_id": dev_id, "message": "mock capture accepted"}
            self.completed_tasks.append({"task_id": task_id, "request": request, "response": response, "dry_run": True})
            self.logger.emit("capture", "Dry-run：已模拟启动采集任务（不生成 slice_*.npz）", {"request": request, "response": response})
            return response
        configure_payload = dict(request)
        configure_payload.pop("task_id", None)
        self.client.configure(configure_payload)
        response = self.client.start(request)
        self.completed_tasks.append({"task_id": task_id, "request": request, "response": response})
        self.logger.emit("capture", "USRP 已接受采集任务", {"request": request, "response": response})
        return response

    def wait_until_idle(self, dev_id: str, *, timeout_sec: float = 90.0, poll_interval_sec: float = 0.5) -> dict[str, Any]:
        self.check_cancelled()
        dev_id = str(dev_id or "").strip()
        if not dev_id:
            raise ValueError("dev_id is required")
        if self.dry_run:
            return {"dev_id": dev_id, "status": "IDLE", "task_id": None}
        deadline = time.monotonic() + float(timeout_sec)
        last_device: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            self.check_cancelled()
            devices = self.client.list_devices()
            last_device = next((dict(item) for item in devices if str(item.get("dev_id") or "") == str(dev_id)), None)
            if last_device and str(last_device.get("status") or "").upper() == "IDLE" and not last_device.get("task_id"):
                self.logger.emit("wait", "设备已恢复 IDLE", {"device": last_device})
                return last_device
            time.sleep(float(poll_interval_sec))
        raise TimeoutError(f"等待设备 {dev_id} 恢复 IDLE 超时，最后状态：{last_device}")

    def capture_fft_once(
        self,
        *,
        task_id: str,
        dev_id: str,
        freq: float,
        sample_rate: float | None = None,
        bandwidth: float | None = None,
        gain: float | None = None,
        slice_duration: float = 0.1,
        duration: float | None = 0.1,
        antenna: str | None = None,
        device: dict[str, Any] | None = None,
        expected_frames: int | None = None,
        frame_timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """启动一次采集并直接从 USRP WebSocket 收集 fft_data。

        返回值包含 power_db_mean、fft_frames、frequency_axis_hz、frame_count 等字段。
        本方法是当前自主智能体的主入口，不依赖采集端生成的 slice_*.npz。
        """
        self.check_cancelled()
        task_id = str(task_id or "").strip()
        dev_id = str(dev_id or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        if not dev_id:
            raise ValueError("dev_id is required")
        sample_rate = float(sample_rate or self.default_sample_rate)
        bandwidth = float(bandwidth or self.default_bandwidth)
        gain = float(self.default_gain if gain is None else gain)
        antenna = self.default_antenna if antenna is None else antenna
        device = device or {"dev_id": dev_id, "dev_config": {}}
        expected = int(expected_frames or self.expected_fft_frames or 1)
        expected = max(1, min(expected, 200))
        frame_timeout = float(frame_timeout_sec or self.frame_timeout_sec or 1.5)
        timeout_sec = max(30.0, float(duration or slice_duration or 0.1) * 20 + 20.0)

        if self.dry_run:
            self.start_capture(
                task_id=task_id,
                dev_id=dev_id,
                freq=freq,
                sample_rate=sample_rate,
                bandwidth=bandwidth,
                gain=gain,
                slice_duration=slice_duration,
                duration=duration,
                antenna=antenna,
                device=device,
            )
            result = self._mock_fft_capture(
                task_id=task_id,
                freq=freq,
                sample_rate=sample_rate,
                fft_size=1024,
                frame_count=expected,
            )
            self.logger.emit("fft", "Dry-run：已生成模拟 WebSocket FFT 帧", {"task_id": task_id, "frame_count": result["frame_count"]})
            return result

        ws_url = self.client.ws_url(dev_id)
        frames: list[np.ndarray] = []
        frame_meta: list[dict[str, Any]] = []
        response: dict[str, Any] | None = None
        self.logger.emit("fft", "准备连接 USRP WebSocket 以接收实时 FFT", {"ws_url": ws_url, "task_id": task_id})
        try:
            from websockets.sync.client import connect
        except Exception as exc:
            raise AutonomousUsrpError("缺少 websockets 依赖，无法直接接收 USRP WebSocket FFT。请安装 websockets，或使用 uvicorn[standard] 依赖。") from exc

        try:
            with connect(ws_url, open_timeout=5, close_timeout=2) as ws:
                # 连接后 USRP 服务通常会先发送一次 status 消息，这里尽量消费掉，避免与 FFT 混杂。
                try:
                    raw = ws.recv(timeout=1.0)
                    msg = json.loads(raw) if isinstance(raw, str) else {}
                    if isinstance(msg, dict) and msg.get("type") == "status":
                        self.logger.emit("fft", "已收到 WebSocket 初始状态", {"status": msg})
                    elif isinstance(msg, dict) and msg.get("type") == "fft":
                        self._append_fft_frame(msg, frames, frame_meta)
                except TimeoutError:
                    pass
                except Exception as exc:
                    self.logger.emit("fft", "读取 WebSocket 初始状态失败，继续启动采集", {"error": str(exc)})

                response = self.start_capture(
                    task_id=task_id,
                    dev_id=dev_id,
                    freq=freq,
                    sample_rate=sample_rate,
                    bandwidth=bandwidth,
                    gain=gain,
                    slice_duration=slice_duration,
                    duration=duration,
                    antenna=antenna,
                    device=device,
                )

                deadline = time.monotonic() + timeout_sec
                while time.monotonic() < deadline and len(frames) < expected:
                    self.check_cancelled()
                    try:
                        raw = ws.recv(timeout=frame_timeout)
                    except TimeoutError:
                        # 如果设备已经空闲且已有数据，就认为本频点 FFT 采集完成。
                        if frames and self._device_is_idle(dev_id):
                            break
                        continue
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw) if isinstance(raw, str) else {}
                    except Exception:
                        continue
                    if isinstance(msg, dict) and msg.get("type") == "fft":
                        self._append_fft_frame(msg, frames, frame_meta)
                        self.logger.emit("fft", "收到 USRP WebSocket FFT 帧", {"task_id": task_id, "frame_count": len(frames), "freq": msg.get("freq")})
        finally:
            # 不管 FFT 是否收满，都等待采集任务真正结束，避免下一频点启动时设备仍为 BUSY。
            try:
                self.wait_until_idle(dev_id, timeout_sec=timeout_sec, poll_interval_sec=0.5)
            except Exception as exc:
                self.logger.emit("wait", "等待设备空闲时出现异常", {"error": str(exc), "task_id": task_id})
                raise

        if not frames:
            raise AutonomousUsrpError(
                f"任务 {task_id} 未收到任何 WebSocket FFT 帧。请检查 USRP stream 接口、duration 是否过短、网络连通性，或适当增大扫描时间。"
            )
        frame_array = np.vstack(frames).astype(float)
        first_meta = frame_meta[0] if frame_meta else {}
        fft_size = int(first_meta.get("fft_size") or frame_array.shape[1])
        frame_freq = float(first_meta.get("freq") or freq)
        frame_sample_rate = float(first_meta.get("sample_rate") or sample_rate)
        power_db_mean = np.mean(frame_array, axis=0)
        frequency_axis = self.fft_frequency_axis(center_freq=frame_freq, sample_rate=frame_sample_rate, fft_size=fft_size)
        result = {
            "task_id": task_id,
            "response": response or {},
            "source": "websocket_fft",
            "freq_hz": frame_freq,
            "sample_rate": frame_sample_rate,
            "fft_size": fft_size,
            "frame_count": int(frame_array.shape[0]),
            "fft_frames": frame_array,
            "power_db_mean": power_db_mean,
            "frequency_axis_hz": frequency_axis,
            "frame_timestamps": [item.get("timestamp") for item in frame_meta],
            "metadata": frame_meta,
        }
        self.logger.emit("fft", "本频点 WebSocket FFT 采集完成", {"task_id": task_id, "frame_count": result["frame_count"], "fft_size": fft_size})
        return result

    def _append_fft_frame(self, msg: dict[str, Any], frames: list[np.ndarray], frame_meta: list[dict[str, Any]]) -> None:
        fft_data = msg.get("fft_data")
        if not isinstance(fft_data, list) or not fft_data:
            return
        arr = np.asarray(fft_data, dtype=float)
        if arr.ndim != 1 or arr.size == 0:
            return
        frames.append(arr)
        frame_meta.append(
            {
                "timestamp": msg.get("timestamp"),
                "freq": msg.get("freq"),
                "sample_rate": msg.get("sample_rate"),
                "fft_size": msg.get("fft_size") or int(arr.size),
            }
        )

    def _device_is_idle(self, dev_id: str) -> bool:
        try:
            devices = self.client.list_devices()
        except Exception:
            return False
        item = next((dict(dev) for dev in devices if str(dev.get("dev_id") or "") == str(dev_id)), None)
        return bool(item and str(item.get("status") or "").upper() == "IDLE" and not item.get("task_id"))

    def _mock_fft_capture(self, *, task_id: str, freq: float, sample_rate: float, fft_size: int = 1024, frame_count: int = 1) -> dict[str, Any]:
        rng = np.random.default_rng(abs(hash(task_id)) % (2**32))
        base = rng.normal(-82.0, 2.0, (frame_count, fft_size))
        peak_bin = int(rng.integers(low=max(1, fft_size // 8), high=max(2, fft_size * 7 // 8)))
        for offset, boost in [(-1, 8.0), (0, 15.0), (1, 8.0)]:
            idx = min(max(peak_bin + offset, 0), fft_size - 1)
            base[:, idx] += boost
        axis = self.fft_frequency_axis(center_freq=freq, sample_rate=sample_rate, fft_size=fft_size)
        return {
            "task_id": task_id,
            "response": {"status": "DRY_RUN", "task_id": task_id},
            "source": "mock_websocket_fft",
            "freq_hz": float(freq),
            "sample_rate": float(sample_rate),
            "fft_size": int(fft_size),
            "frame_count": int(frame_count),
            "fft_frames": base.astype(float),
            "power_db_mean": np.mean(base, axis=0),
            "frequency_axis_hz": axis,
            "frame_timestamps": [datetime.now(timezone.utc).isoformat() for _ in range(frame_count)],
            "metadata": [],
        }

    def fft_frequency_axis(self, *, center_freq: float, sample_rate: float, fft_size: int = 1024) -> np.ndarray:
        return float(center_freq) + (np.arange(int(fft_size)) - int(fft_size) / 2) * float(sample_rate) / int(fft_size)

    def save_spectrum_npz(self, output_name: str, **arrays: Any) -> str:
        self.check_cancelled()
        AUTONOMOUS_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[\\/:*?\"<>|]+", "_", output_name).strip() or f"autonomous_spectrum_{uuid.uuid4().hex[:8]}.npz"
        if not safe_name.endswith(".npz"):
            safe_name += ".npz"
        path = AUTONOMOUS_OUTPUT_ROOT / safe_name
        payload: dict[str, Any] = {}
        for key, value in arrays.items():
            if isinstance(value, dict):
                payload[f"{key}_json"] = np.array(json.dumps(value, ensure_ascii=False))
            elif isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = np.array(value if value is not None else "")
            else:
                payload[key] = np.asarray(value)
        np.savez_compressed(path, **payload)
        self.logger.emit("save", "频谱结果已保存（由 WebSocket FFT 汇总生成，不依赖远端 slice_*.npz）", {"output_file": str(path)})
        return str(path)


@dataclass(slots=True)
class AutonomousTaskContext:
    usrp: SafeUsrpRuntime
    task: dict[str, Any]
    workspace: str
    logger: SafeExecutionLogger

    def emit(self, stage: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.logger.emit(stage, message, data)

    def make_task_id(self, prefix: str = "auto_usrp", **parts: Any) -> str:
        return self.usrp.make_task_id(prefix, **parts)

    def make_output_name(self, room_name: str, mode: str, suffix: str = "npz") -> str:
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        mode_text = "背景频谱" if mode in {"background", "背景", "background_spectrum"} else ("正常频谱" if mode in {"normal", "正常", "normal_spectrum"} else str(mode))
        clean_room = re.sub(r"[\\/:*?\"<>|]+", "_", str(room_name or "会议室")).strip()
        return f"{clean_room}_{mode_text}_{now}.{suffix.lstrip('.')}"


def _extract_plan_from_task(task_description: str) -> dict[str, Any]:
    text = str(task_description or "")
    room = "会议室"
    room_match = re.search(r"([\u4e00-\u9fa5A-Za-z0-9_-]{1,32})\s*(会议室|房间|实验室|教室|room)", text, flags=re.I)
    if room_match:
        prefix = re.sub(r"^(请|帮我|采集|检测|检查|扫描|对|通过|代码生成型自主智能体)", "", room_match.group(1)).strip()
        room = (prefix + room_match.group(2)).strip() or room_match.group(0).replace(" ", "")
    mode = "background" if any(k in text for k in ["背景", "background", "底噪", "空场"]) else "normal"

    def freq_value(num: str, unit: str) -> float:
        value = float(num)
        unit_l = unit.lower()
        if "ghz" in unit_l or "g" == unit_l:
            return value * 1e9
        if "mhz" in unit_l or "m" == unit_l:
            return value * 1e6
        if "khz" in unit_l or "k" == unit_l:
            return value * 1e3
        return value

    start_hz = 70e6
    stop_hz = 6e9
    step_hz = 50e6
    range_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(MHz|M|GHz|G|Hz)\s*(?:-|~|到|至|—|–)\s*([0-9]+(?:\.[0-9]+)?)\s*(MHz|M|GHz|G|Hz)", text, flags=re.I)
    if range_match:
        start_hz = freq_value(range_match.group(1), range_match.group(2))
        stop_hz = freq_value(range_match.group(3), range_match.group(4))
    step_match = re.search(r"(?:步长|step)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*(MHz|M|GHz|G|Hz)", text, flags=re.I)
    if step_match:
        step_hz = freq_value(step_match.group(1), step_match.group(2))
    dwell_sec = 0.1
    dwell_match = re.search(r"(?:扫描时间|采集时间|驻留|dwell|duration)\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)\s*(ms|毫秒|s|秒)", text, flags=re.I)
    if dwell_match:
        dwell_sec = float(dwell_match.group(1)) / 1000 if dwell_match.group(2).lower() in {"ms", "毫秒"} else float(dwell_match.group(1))
    repeat_count = 3
    repeat_match = re.search(r"(?:扫描次数|重复|repeat)\s*[:：]?\s*([0-9]+)\s*(?:次)?", text, flags=re.I)
    if repeat_match:
        repeat_count = int(repeat_match.group(1))
    return {
        "task_type": "autonomous_usrp_codegen_capture_fft_stream",
        "data_source": "usrp_websocket_fft",
        "room_name": room,
        "mode": mode,
        "freq_start_hz": start_hz,
        "freq_stop_hz": stop_hz,
        "freq_step_hz": step_hz,
        "dwell_time_sec": dwell_sec,
        "repeat_count": repeat_count,
        "aggregation": "mean",
        "sample_rate": float(os.getenv("DEEPEM_USRP_SAMPLE_RATE") or "1000000"),
        "bandwidth": float(os.getenv("DEEPEM_USRP_BANDWIDTH") or "1000000"),
        "gain": float(os.getenv("DEEPEM_USRP_GAIN") or "40"),
        "antenna": (os.getenv("DEEPEM_USRP_ANTENNA") or "RX2").strip() or None,
        "expected_fft_frames_per_capture": int(os.getenv("DEEPEM_AUTONOMOUS_USRP_EXPECTED_FFT_FRAMES") or "1"),
    }


def _fallback_generated_code(plan: dict[str, Any]) -> str:
    return '''def run_task(ctx):
    # 读取结构化任务计划，所有硬件操作都必须通过 ctx.usrp 这个安全 SDK 完成。
    # 本版本直接使用 USRP WebSocket 返回的 fft_data，不读取远端 slice_*.npz 文件。
    plan = ctx.task
    room_name = plan.get("room_name", "会议室")
    mode = plan.get("mode", "background")
    sample_rate = float(plan.get("sample_rate", 1000000))
    bandwidth = float(plan.get("bandwidth", 1000000))
    gain = float(plan.get("gain", 40))
    antenna = plan.get("antenna", "RX2")
    dwell_time = float(plan.get("dwell_time_sec", 0.1))
    repeat_count = int(plan.get("repeat_count", 3))
    expected_frames = int(plan.get("expected_fft_frames_per_capture", 1))

    ctx.emit("plan", "开始执行代码生成型自主 USRP WebSocket FFT 采集任务", {"plan": plan})
    device = ctx.usrp.scan_and_get_idle_device(plan.get("dev_id"))
    dev_id = device["dev_id"]
    center_freqs = ctx.usrp.generate_frequency_list(
        float(plan.get("freq_start_hz", 70000000)),
        float(plan.get("freq_stop_hz", 6000000000)),
        float(plan.get("freq_step_hz", 50000000)),
    )
    ctx.emit("plan", "已生成频点列表", {"freq_count": len(center_freqs), "first_freq": center_freqs[0], "last_freq": center_freqs[-1]})

    all_freq_axis = []
    all_power_mean = []
    all_power_repeats = []
    all_fft_frame_counts = []
    raw_task_ids = []

    for freq_index, freq in enumerate(center_freqs):
        repeat_powers = []
        repeat_frame_counts = []
        ctx.emit("capture", "开始采集频点", {"index": freq_index + 1, "total": len(center_freqs), "freq_hz": freq})
        freq_axis = None
        for repeat_index in range(repeat_count):
            task_id = ctx.make_task_id("auto_usrp_fft", f=freq, r=repeat_index + 1)
            capture = ctx.usrp.capture_fft_once(
                task_id=task_id,
                dev_id=dev_id,
                freq=freq,
                sample_rate=sample_rate,
                bandwidth=bandwidth,
                gain=gain,
                slice_duration=dwell_time,
                duration=dwell_time,
                antenna=antenna,
                device=device,
                expected_frames=expected_frames,
            )
            repeat_powers.append(capture["power_db_mean"])
            repeat_frame_counts.append(capture["frame_count"])
            freq_axis = capture["frequency_axis_hz"]
            raw_task_ids.append(task_id)
            ctx.emit("fft", "完成一次 WebSocket FFT 采集", {"task_id": task_id, "freq_hz": freq, "frame_count": capture["frame_count"]})
        mean_power = sum(repeat_powers) / len(repeat_powers)
        all_freq_axis.append(freq_axis)
        all_power_repeats.append(repeat_powers)
        all_power_mean.append(mean_power)
        all_fft_frame_counts.append(repeat_frame_counts)

    output_name = ctx.make_output_name(room_name=room_name, mode=mode)
    output_file = ctx.usrp.save_spectrum_npz(
        output_name,
        center_freqs_hz=center_freqs,
        frequency_axis_hz=all_freq_axis,
        power_db_mean=all_power_mean,
        power_db_repeats=all_power_repeats,
        fft_frame_counts=all_fft_frame_counts,
        raw_task_ids=raw_task_ids,
        metadata=plan,
        data_source="usrp_websocket_fft",
    )
    return {
        "status": "completed",
        "output_file": output_file,
        "freq_count": len(center_freqs),
        "repeat_count": repeat_count,
        "raw_task_count": len(raw_task_ids),
        "data_source": "usrp_websocket_fft",
        "mode": mode,
        "room_name": room_name,
    }
'''


_ALLOWED_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "callable": callable,
    "dict": dict,
    "enumerate": enumerate,
    "Exception": Exception,
    "float": float,
    "getattr": getattr,
    "hasattr": hasattr,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "object": object,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "TypeError": TypeError,
    "type": type,
    "ValueError": ValueError,
    "zip": zip,
}
_FORBIDDEN_IMPORTS = {"os", "sys", "subprocess", "socket", "requests", "urllib", "shutil", "pathlib", "builtins", "pickle"}
_FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open", "input", "globals", "locals", "vars", "dir"}
_FORBIDDEN_SDK_ATTRS = {"load_task_iq", "compute_fft_power_db", "task_dir", "_write_mock_task_npz"}

AUTONOMOUS_USRP_SDK_CONTRACT = """
你只能生成一个 Python 函数：def run_task(ctx):
可用对象：
- ctx.task: dict，结构化任务计划。
- ctx.emit(stage, message, data=None): 向前端输出中间过程。
- ctx.make_task_id(prefix, **parts): 生成安全 task_id。
- ctx.make_output_name(room_name, mode): 生成“会议室名称_背景/正常频谱_日期时间.npz”。
- ctx.usrp.scan_and_get_idle_device(dev_id=None): 扫描并返回 IDLE 设备。
- ctx.usrp.generate_frequency_list(start_hz, stop_hz, step_hz): 生成频点列表。
- ctx.usrp.capture_fft_once(task_id, dev_id, freq, sample_rate, bandwidth, gain, slice_duration, duration, antenna, device=None, expected_frames=1): 启动一次采集，并直接从 USRP WebSocket 接收 fft_data，返回 dict；必须通过 capture["power_db_mean"]、capture["frequency_axis_hz"]、capture["fft_frames"]、capture["frame_count"] 取值，禁止写 a, b, c, d = ctx.usrp.capture_fft_once(...)。
- ctx.usrp.fft_frequency_axis(center_freq, sample_rate, fft_size=1024): 生成 FFT 频率轴。
- ctx.usrp.save_spectrum_npz(output_name, **arrays): 保存最终汇总 npz，注意这个 npz 是平台基于 WebSocket FFT 生成的结果文件，不是读取采集端 slice_*.npz。
禁止任何 import、open、requests、subprocess、os、eval、exec。
禁止调用 load_task_iq、compute_fft_power_db、task_dir 或读取 slice_*.npz。
不要直接访问 HTTP/WebSocket 原始接口，只能使用 ctx.usrp。
不要把 power_db_mean、fft_frames、frequency_axis_hz 这类数组对象直接拼进 ctx.emit(...) 的 message，也不要对它们使用 `:.2f` 这类标量格式化；只输出摘要信息，比如 frame_count、峰值、均值或数组长度。
""".strip()


def validate_generated_code(code: str) -> dict[str, Any]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise AutonomousUsrpError(f"生成代码语法错误：{exc}") from exc
    funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if len(funcs) != 1 or funcs[0].name != "run_task":
        raise AutonomousUsrpError("生成代码必须且只能定义一个 run_task(ctx) 函数")
    if len(funcs[0].args.args) != 1 or funcs[0].args.args[0].arg != "ctx":
        raise AutonomousUsrpError("run_task 函数签名必须是 run_task(ctx)")

    assigned_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise AutonomousUsrpError("禁止 import；请直接使用 ctx、ctx.usrp、json、time 以及安全 SDK。")
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Tuple) and _is_capture_fft_call(node.value):
                raise AutonomousUsrpError("capture_fft_once(...) 返回 dict，禁止按多返回值解包；请使用 result[\"power_db_mean\"] 等方式取值。")
            value = node.value
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if _is_capture_fft_call(value):
                        assigned_names[target.id] = "fft_capture"
                    elif _looks_like_capture_fft_array(value, assigned_names):
                        assigned_names[target.id] = "fft_array"
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _is_capture_fft_call(node.value):
                assigned_names[node.target.id] = "fft_capture"
            elif _looks_like_capture_fft_array(node.value, assigned_names):
                assigned_names[node.target.id] = "fft_array"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
                raise AutonomousUsrpError(f"禁止调用函数：{node.func.id}")
            if isinstance(node.func, ast.Attribute):
                if node.func.attr.startswith("__"):
                    raise AutonomousUsrpError(f"禁止访问魔术方法：{node.func.attr}")
                if node.func.attr in _FORBIDDEN_SDK_ATTRS:
                    raise AutonomousUsrpError(f"当前自主采集禁止调用 {node.func.attr}，请直接使用 WebSocket FFT 方法 capture_fft_once")
                if (
                    node.func.attr == "emit"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "ctx"
                    and len(node.args) >= 2
                    and _format_expr_uses_array(node.args[1], assigned_names)
                ):
                    raise AutonomousUsrpError("禁止将数组对象直接格式化到 ctx.emit(...)；请只输出摘要信息，如 frame_count、峰值或数组长度。")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise AutonomousUsrpError(f"禁止访问魔术属性：{node.attr}")
            if node.attr in _FORBIDDEN_SDK_ATTRS:
                raise AutonomousUsrpError(f"当前自主采集禁止访问 {node.attr}，请直接使用 WebSocket FFT 方法 capture_fft_once")
    return {"status": "passed", "function": "run_task", "node_count": sum(1 for _ in ast.walk(tree)), "data_source": "usrp_websocket_fft"}


def _looks_like_capture_fft_power(value: ast.AST | None) -> bool:
    return _looks_like_capture_fft_array(value, {})


def _looks_like_capture_fft_array(value: ast.AST | None, assigned_names: dict[str, str]) -> bool:
    if not isinstance(value, ast.Subscript):
        return False
    if _is_capture_fft_call(value.value):
        pass
    elif isinstance(value.value, ast.Name) and assigned_names.get(value.value.id) == "fft_capture":
        pass
    else:
        return False
    slice_node = value.slice
    if isinstance(slice_node, ast.Constant):
        return slice_node.value in {"power_db_mean", "fft_frames", "frequency_axis_hz"}
    return False


def _is_capture_fft_call(value: ast.AST | None) -> bool:
    if not isinstance(value, ast.Call):
        return False
    if not isinstance(value.func, ast.Attribute):
        return False
    return value.func.attr == "capture_fft_once"


def _format_expr_uses_array(expr: ast.AST, assigned_names: dict[str, str]) -> bool:
    for node in ast.walk(expr):
        if isinstance(node, ast.FormattedValue):
            if _expr_is_array_like(node.value, assigned_names):
                if node.format_spec is not None:
                    raise AutonomousUsrpError("禁止将 FFT 数组按标量格式化；请只输出 frame_count、数组长度、峰值或均值摘要。")
                return True
        if isinstance(node, ast.Name) and assigned_names.get(node.id) == "fft_array":
            return True
    return False


def _expr_is_array_like(expr: ast.AST, assigned_names: dict[str, str]) -> bool:
    if isinstance(expr, ast.Name):
        return assigned_names.get(expr.id) == "fft_array"
    return _looks_like_capture_fft_power(expr)


def execute_generated_code(code: str, task_plan: dict[str, Any], context: ToolContext, *, dry_run: bool = False) -> dict[str, Any]:
    validation = validate_generated_code(code)
    workspace = AUTONOMOUS_OUTPUT_ROOT / context.run.id
    workspace.mkdir(parents=True, exist_ok=True)
    logger = SafeExecutionLogger(stream_handler=context.stream_handler, run_id=context.run.id)
    usrp = SafeUsrpRuntime(logger=logger, dry_run=dry_run, cancel_checker=context.cancel_checker)
    task_ctx = AutonomousTaskContext(usrp=usrp, task=dict(task_plan), workspace=str(workspace), logger=logger)
    logger.emit("validate", "生成代码静态安全检查通过", validation)
    local_env: dict[str, Any] = {}
    global_env = {
        "__builtins__": _ALLOWED_BUILTINS,
        "np": np,
        "time": time,
        "json": json,
    }
    exec(compile(code, "<autonomous_usrp_generated>", "exec"), global_env, local_env)
    run_task = local_env.get("run_task") or global_env.get("run_task")
    if not callable(run_task):
        raise AutonomousUsrpError("未找到可执行的 run_task(ctx)")
    started = time.time()
    try:
        result = run_task(task_ctx)
    except Exception as exc:
        logger.emit("error", "生成代码执行失败", {"error": str(exc), "traceback": traceback.format_exc(limit=8)})
        raise
    elapsed = time.time() - started
    if not isinstance(result, dict):
        result = {"status": "completed", "return_value": str(result)}
    result = {
        **result,
        "elapsed_sec": round(elapsed, 3),
        "dry_run": bool(usrp.dry_run),
        "completed_tasks": usrp.completed_tasks[-20:],
        "log_count": len(logger.logs),
        "data_source": result.get("data_source") or "usrp_websocket_fft",
    }
    logger.emit("completed", "自主生成代码执行完成", result)
    return result


def _extract_code(text: str) -> str:
    text = str(text or "").strip()
    match = re.search(r"```(?:python)?\s*(.*?)```", text, flags=re.S | re.I)
    if match:
        return match.group(1).strip()
    start = text.find("def run_task")
    if start >= 0:
        return text[start:].strip()
    return text


def generate_code_with_llm(task_description: str, context: ToolContext, *, task_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = task_plan or _extract_plan_from_task(task_description)
    store = MarkdownKnowledgeStore()
    chunks = store.search(task_description + " USRP WebSocket stream fft_data start devices configure 实时 FFT", top_k=8)
    knowledge = "\n\n".join(f"### {item.title}\n{item.text[:2200]}" for item in chunks)
    prompt = f"""
你是 DeepEM 的代码生成型 USRP 频谱采集智能体。请根据用户任务、结构化计划和 USRP API 知识，生成可执行的 run_task(ctx) 函数。

用户任务：
{task_description}

结构化计划：
{json.dumps(plan, ensure_ascii=False, indent=2)}

USRP API 知识片段：
{knowledge}

安全 SDK 契约：
{AUTONOMOUS_USRP_SDK_CONTRACT}

输出要求：
1. 只输出 Python 代码，不要解释。
2. 只定义 def run_task(ctx): 一个函数。
3. 对多频点、多次重复采集要循环执行 capture_fft_once，并使用其返回的 power_db_mean 做平均。
4. 绝对不要读取 slice_*.npz，不要调用 load_task_iq，不要自己计算 IQ FFT；直接使用 USRP 平台 WebSocket 返回的 fft_data。
5. 不要把 ndarray 或其他数组对象直接格式化到 ctx.emit(...) 的字符串中。
6. 保存最终汇总结果 npz，并 return dict，至少包含 status、output_file、freq_count、repeat_count、data_source。
""".strip()
    code = ""
    llm_error = ""
    if context.llm_client is not None:
        try:
            response = context.llm_client.complete(messages=[{"role": "user", "content": prompt}], tools=[], temperature=0.1)
            code = _extract_code(response.content)
        except Exception as exc:
            llm_error = str(exc)
    if not code:
        code = _fallback_generated_code(plan)
        llm_error = llm_error or "LLM 未返回代码，已使用内置 WebSocket FFT fallback 采集函数。"
    try:
        validation = validate_generated_code(code)
    except Exception as exc:
        code = _fallback_generated_code(plan)
        validation = validate_generated_code(code)
        llm_error = (llm_error + "；" if llm_error else "") + f"模型代码未通过安全检查（{exc}），已自动替换为 WebSocket FFT 安全模板。"
    return {
        "code": code,
        "task_plan": plan,
        "knowledge_chunks": [{"title": item.title, "score": item.score, "preview": item.text[:360]} for item in chunks],
        "validation": validation,
        "llm_error": llm_error,
        "data_source": "usrp_websocket_fft",
    }


def _retrieve_usrp_api_knowledge(args: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
    query = str(args.get("query") or "USRP WebSocket stream fft_data start devices 实时 FFT")
    top_k = int(args.get("top_k") or 6)
    chunks = MarkdownKnowledgeStore().search(query, top_k=max(1, min(top_k, 12)))
    data = {
        "query": query,
        "items": [{"title": item.title, "score": item.score, "text": item.text[:1800]} for item in chunks],
        "doc_path": str(USRP_API_DOC),
    }
    return ToolExecutionResult(result=ToolResult(status="success", data=data, metadata={"display_in_chat": True}))


def _generate_usrp_task_code(args: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
    task_description = str(args.get("task_description") or (context.trigger_message.content if context.trigger_message else ""))
    plan = dict(args.get("task_plan") or _extract_plan_from_task(task_description))
    data = generate_code_with_llm(task_description, context, task_plan=plan)
    if context.stream_handler is not None:
        context.stream_handler("tool_result", {"run_id": context.run.id, "tool_name": "generate_usrp_task_code", "data": data})
    return ToolExecutionResult(result=ToolResult(status="success", data=data, metadata={"display_in_chat": True}))


def _execute_usrp_task_code(args: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
    code = str(args.get("code") or "")
    if not code:
        raise AutonomousUsrpError("execute_usrp_task_code 需要 code 参数")
    task_description = str(args.get("task_description") or (context.trigger_message.content if context.trigger_message else ""))
    task_plan = dict(args.get("task_plan") or _extract_plan_from_task(task_description))
    dry_run = bool(args.get("dry_run", False))
    result = execute_generated_code(code, task_plan, context, dry_run=dry_run)
    data = {"execution_result": result, "task_plan": task_plan, "code": code, "data_source": "usrp_websocket_fft"}
    if context.stream_handler is not None:
        context.stream_handler("tool_result", {"run_id": context.run.id, "tool_name": "execute_usrp_task_code", "data": data})
    return ToolExecutionResult(result=ToolResult(status="success", data=data, metadata={"display_in_chat": True}))


def _run_autonomous_usrp_task(args: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
    task_description = str(args.get("task_description") or (context.trigger_message.content if context.trigger_message else ""))
    user_plan = args.get("task_plan") if isinstance(args.get("task_plan"), dict) else None
    dry_run = bool(args.get("dry_run", False))
    generated = generate_code_with_llm(task_description, context, task_plan=user_plan)
    if context.stream_handler is not None:
        context.stream_handler("tool_result", {"run_id": context.run.id, "tool_name": "generate_usrp_task_code", "data": generated})
    execution = execute_generated_code(generated["code"], dict(generated["task_plan"]), context, dry_run=dry_run)
    data = {
        "stage": "completed",
        "task_description": task_description,
        "task_plan": generated["task_plan"],
        "generated_code": generated["code"],
        "validation": generated["validation"],
        "knowledge_chunks": generated["knowledge_chunks"],
        "execution_result": execution,
        "dry_run": dry_run,
        "data_source": "usrp_websocket_fft",
    }
    if context.stream_handler is not None:
        context.stream_handler("tool_result", {"run_id": context.run.id, "tool_name": "run_autonomous_usrp_task", "data": data})
    return ToolExecutionResult(result=ToolResult(status="success", data=data, metadata={"display_in_chat": True}))


def build_retrieve_usrp_api_knowledge_tool() -> ToolDefinition:
    return ToolDefinition(
        name="retrieve_usrp_api_knowledge",
        description="从内置 USRP API Markdown 知识库中检索与当前采集任务相关的接口说明、参数约束、WebSocket FFT、实时频谱和注意事项。",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
        },
        handler=_retrieve_usrp_api_knowledge,
    )


def build_generate_usrp_task_code_tool() -> ToolDefinition:
    return ToolDefinition(
        name="generate_usrp_task_code",
        description="根据自然语言采集任务、内置 USRP API 知识库和安全 SDK 契约，自主生成 def run_task(ctx) 采集函数代码。当前版本直接使用 USRP WebSocket fft_data，不读取 slice_*.npz。",
        input_schema={
            "type": "object",
            "properties": {
                "task_description": {"type": "string"},
                "task_plan": {"type": "object"},
            },
        },
        handler=_generate_usrp_task_code,
    )


def build_execute_usrp_task_code_tool() -> ToolDefinition:
    return ToolDefinition(
        name="execute_usrp_task_code",
        description="执行 generate_usrp_task_code 生成的 run_task(ctx) 函数。执行前会做 AST 安全检查，执行过程通过安全 USRP SDK 控制真实设备，并直接收集 WebSocket FFT。",
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "task_description": {"type": "string"},
                "task_plan": {"type": "object"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["code"],
        },
        handler=_execute_usrp_task_code,
    )


def build_run_autonomous_usrp_task_tool() -> ToolDefinition:
    return ToolDefinition(
        name="run_autonomous_usrp_task",
        description=(
            "端到端代码生成型自主 USRP 采集工具：检索内置 Markdown API 知识库，生成采集函数代码，展示代码，静态安全检查，"
            "然后执行代码完成多频点、多重复次数、WebSocket FFT 接收、平均和最终结果 npz 保存。当前版本不读取远端 slice_*.npz。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_description": {"type": "string"},
                "task_plan": {"type": "object"},
                "dry_run": {"type": "boolean"},
            },
        },
        handler=_run_autonomous_usrp_task,
    )
