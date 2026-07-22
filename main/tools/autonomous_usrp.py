from __future__ import annotations

import ast
import json
import os
import re
import shutil
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
        cancel_checker: Callable[[], bool] | None = None,
        output_dir: Path | str | None = None,
    ) -> None:
        self.client = UsrpClient.from_env()
        self.logger = logger
        self.cancel_checker = cancel_checker
        self.output_dir = Path(output_dir or AUTONOMOUS_OUTPUT_ROOT).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
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
        configure_payload = dict(request)
        configure_payload.pop("task_id", None)
        self.client.configure(configure_payload)
        response = self.client.start(request)
        self.completed_tasks.append({"task_id": task_id, "request": request, "response": response})
        self.logger.emit("capture", "USRP 已接受采集任务", {"request": request, "response": response})
        return response

    def wait_until_idle(self, dev_id: str, *, timeout_sec: float = 90.0, poll_interval_sec: float = 0.5) -> dict[str, Any]:
        self.check_cancelled()
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

        返回值包含 power_db_mean、fft_frames、frequency_axis_mhz、frame_count 等字段。
        本方法是当前自主智能体的主入口，不依赖采集端生成的 slice_*.npz。
        """
        self.check_cancelled()
        sample_rate = float(sample_rate or self.default_sample_rate)
        bandwidth = float(bandwidth or self.default_bandwidth)
        gain = float(self.default_gain if gain is None else gain)
        antenna = self.default_antenna if antenna is None else antenna
        device = device or {"dev_id": dev_id, "dev_config": {}}
        expected = int(expected_frames or self.expected_fft_frames or 1)
        expected = max(1, min(expected, 200))
        frame_timeout = float(frame_timeout_sec or self.frame_timeout_sec or 1.5)
        timeout_sec = max(30.0, float(duration or slice_duration or 0.1) * 20 + 20.0)

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
            "freq_mhz": frame_freq / 1_000_000,
            "sample_rate_mhz": frame_sample_rate / 1_000_000,
            "fft_size": fft_size,
            "frame_count": int(frame_array.shape[0]),
            "fft_frames": frame_array,
            "power_db_mean": power_db_mean,
            "frequency_axis_mhz": frequency_axis / 1_000_000,
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

    def fft_frequency_axis(self, *, center_freq: float, sample_rate: float, fft_size: int = 1024) -> np.ndarray:
        return float(center_freq) + (np.arange(int(fft_size)) - int(fft_size) / 2) * float(sample_rate) / int(fft_size)

    def save_spectrum_npz(self, output_name: str, **arrays: Any) -> str:
        self.check_cancelled()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[\\/:*?\"<>|]+", "_", Path(str(output_name)).name).strip() or f"autonomous_spectrum_{uuid.uuid4().hex[:8]}.npz"
        if not safe_name.endswith(".npz"):
            safe_name += ".npz"
        path = self.output_dir / safe_name
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
        now = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
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
        "freq_start_mhz": start_hz / 1e6,
        "freq_stop_mhz": stop_hz / 1e6,
        "freq_step_mhz": step_hz / 1e6,
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
    sample_rate = float(plan.get("sample_rate", 1))
    bandwidth = float(plan.get("bandwidth", 1))
    if sample_rate < 100000: sample_rate *= 1000000
    if bandwidth < 100000: bandwidth *= 1000000
    gain = float(plan.get("gain", 40))
    antenna = plan.get("antenna", "RX2")
    dwell_time = float(plan.get("dwell_time_sec", 0.1))
    repeat_count = int(plan.get("repeat_count", 3))
    expected_frames = int(plan.get("expected_fft_frames_per_capture", 1))

    ctx.emit("plan", "开始执行代码生成型自主 USRP WebSocket FFT 采集任务", {"plan": plan})
    device = ctx.usrp.scan_and_get_idle_device(plan.get("dev_id"))
    dev_id = device["dev_id"]
    center_freqs = ctx.usrp.generate_frequency_list(
        float(plan.get("freq_start_mhz", 70)) * 1000000,
        float(plan.get("freq_stop_mhz", 6000)) * 1000000,
        float(plan.get("freq_step_mhz", 50)) * 1000000,
    )
    ctx.emit("plan", "已生成频点列表", {"freq_count": len(center_freqs), "first_freq_mhz": center_freqs[0] / 1_000_000, "last_freq_mhz": center_freqs[-1] / 1_000_000})

    all_freq_axis = []
    all_power_mean = []
    all_power_repeats = []
    all_fft_frame_counts = []
    raw_task_ids = []

    for freq_index, freq in enumerate(center_freqs):
        repeat_powers = []
        repeat_frame_counts = []
        ctx.emit("capture", "开始采集频点", {"index": freq_index + 1, "total": len(center_freqs), "freq_mhz": freq / 1000000})
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
            freq_axis = capture["frequency_axis_mhz"]
            raw_task_ids.append(task_id)
            ctx.emit("fft", "完成一次 WebSocket FFT 采集", {"task_id": task_id, "freq_mhz": freq / 1000000, "frame_count": capture["frame_count"]})
        mean_power = sum(repeat_powers) / len(repeat_powers)
        all_freq_axis.append(freq_axis)
        all_power_repeats.append(repeat_powers)
        all_power_mean.append(mean_power)
        all_fft_frame_counts.append(repeat_frame_counts)

    output_name = ctx.make_output_name(room_name=room_name, mode=mode)
    output_file = ctx.usrp.save_spectrum_npz(
        output_name,
        center_freqs_mhz=np.asarray(center_freqs, dtype=float) / 1_000_000,
        frequency_axis_mhz=all_freq_axis,
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
    # 数值和容器操作。这里必须与生成代码契约保持一致，否则代码会通过
    # AST 安全检查，却在真正采集时因安全内建函数缺失而失败。
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "range": range,
    "reversed": reversed,
    "round": round,
    "set": set,
    "slice": slice,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    # 允许生成代码做受控的参数校验和异常处理。
    "Exception": Exception,
    "IndexError": IndexError,
    "KeyError": KeyError,
    "RuntimeError": RuntimeError,
    "TypeError": TypeError,
    "ValueError": ValueError,
}
_FORBIDDEN_IMPORTS = {"os", "sys", "subprocess", "socket", "requests", "urllib", "shutil", "pathlib", "builtins", "pickle"}
_FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open", "input", "globals", "locals", "vars", "dir"}
_FORBIDDEN_SDK_ATTRS = {"load_task_iq", "compute_fft_power_db", "task_dir", "_write_mock_task_npz"}
# 生成代码不得直接通过 NumPy 写文件；所有产物必须经过
# ctx.usrp.save_spectrum_npz，由运行时强制落入统一任务目录。
_FORBIDDEN_FILE_WRITE_ATTRS = {"save", "savez", "savez_compressed", "savetxt", "tofile", "dump"}


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
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in _FORBIDDEN_IMPORTS:
                    raise AutonomousUsrpError(f"禁止导入模块：{name}")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
                raise AutonomousUsrpError(f"禁止调用函数：{node.func.id}")
            if isinstance(node.func, ast.Attribute):
                if node.func.attr.startswith("__"):
                    raise AutonomousUsrpError(f"禁止访问魔术方法：{node.func.attr}")
                if node.func.attr in _FORBIDDEN_SDK_ATTRS:
                    raise AutonomousUsrpError(f"当前自主采集禁止调用 {node.func.attr}，请直接使用 WebSocket FFT 方法 capture_fft_once")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise AutonomousUsrpError(f"禁止访问魔术属性：{node.attr}")
            if node.attr in _FORBIDDEN_SDK_ATTRS:
                raise AutonomousUsrpError(f"当前自主采集禁止访问 {node.attr}，请直接使用 WebSocket FFT 方法 capture_fft_once")
            if node.attr in _FORBIDDEN_FILE_WRITE_ATTRS:
                raise AutonomousUsrpError(
                    f"禁止在生成代码中直接调用 {node.attr} 写文件；"
                    "请统一使用 ctx.usrp.save_spectrum_npz，确保结果进入任务目录"
                )
    return {"status": "passed", "function": "run_task", "node_count": sum(1 for _ in ast.walk(tree)), "data_source": "usrp_websocket_fft"}


def _unique_workspace_path(workspace: Path, name: str) -> Path:
    safe_name = re.sub(r"[\\/:*?\"<>|]+", "_", Path(name).name).strip() or f"autonomous_result_{uuid.uuid4().hex[:8]}.npz"
    target = workspace / safe_name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for index in range(1, 1000):
        candidate = workspace / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    return workspace / f"{stem}_{uuid.uuid4().hex[:8]}{suffix}"


def _normalize_output_file(output_file: Any, workspace: Path, logger: SafeExecutionLogger) -> Path:
    """保证模型生成代码返回的主结果位于统一任务目录。

    新代码应只使用 SafeUsrpRuntime.save_spectrum_npz。为兼容已经生成或缓存的旧代码，
    若结果真实存在但落在项目工作目录，会在返回前安全迁移到 workspace，而不是让整次
    硬件采集在最后一步失败。
    """
    raw = str(output_file or "").strip()
    if not raw:
        raise AutonomousUsrpError("生成代码未返回 output_file")
    source = Path(raw).expanduser().resolve()
    workspace = workspace.resolve()
    if source == workspace or workspace in source.parents:
        if not source.is_file():
            raise AutonomousUsrpError(f"采集结果文件不存在：{source}")
        return source
    if not source.is_file():
        # 某些旧代码只返回文件名，但实际已由安全 SDK 保存到 workspace。
        in_workspace = workspace / Path(raw).name
        if in_workspace.is_file():
            return in_workspace.resolve()
        raise AutonomousUsrpError(f"采集结果未写入统一任务目录且源文件不存在：{source}")
    if source.suffix.lower() != ".npz":
        raise AutonomousUsrpError(f"拒绝迁移非 NPZ 采集产物：{source}")
    destination = _unique_workspace_path(workspace, source.name)
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    logger.emit(
        "artifact_relocated",
        "检测到旧版生成代码将结果写入任务目录外，已自动迁移到统一任务目录",
        {"source": str(source), "output_file": str(destination)},
    )
    return destination.resolve()


def execute_generated_code(code: str, task_plan: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    validation = validate_generated_code(code)
    requested_task_id = str(task_plan.get("capture_agent_task_id") or task_plan.get("task_id") or context.run.id)
    safe_task_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", requested_task_id).strip("._") or context.run.id
    requested_output_dir = str(task_plan.get("output_dir") or "").strip()
    if requested_output_dir:
        candidate = Path(requested_output_dir).resolve()
        root = AUTONOMOUS_OUTPUT_ROOT.resolve()
        if candidate != root and root not in candidate.parents:
            raise AutonomousUsrpError("output_dir 必须位于 main/data/autonomous_usrp 下")
        workspace = candidate
    else:
        workspace = AUTONOMOUS_OUTPUT_ROOT / safe_task_id
    workspace.mkdir(parents=True, exist_ok=True)
    logger = SafeExecutionLogger(stream_handler=context.stream_handler, run_id=context.run.id)
    usrp = SafeUsrpRuntime(logger=logger, cancel_checker=context.cancel_checker, output_dir=workspace)
    normalized_plan = dict(task_plan)
    normalized_plan["capture_agent_task_id"] = safe_task_id
    normalized_plan["output_dir"] = str(workspace)
    task_ctx = AutonomousTaskContext(usrp=usrp, task=normalized_plan, workspace=str(workspace), logger=logger)
    (workspace / "generated_task.py").write_text(code, encoding="utf-8")
    (workspace / "task_plan.json").write_text(json.dumps(normalized_plan, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    logger.emit("validate", "生成代码静态安全检查通过", validation)
    logger.emit("sandbox", "受控执行环境已就绪，安全内建函数与统一输出目录已注入", {"workspace": str(workspace)})
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
        "completed_tasks": usrp.completed_tasks[-20:],
        "data_source": result.get("data_source") or "usrp_websocket_fft",
        "workspace": str(workspace),
    }
    output_file = result.get("output_file")
    if output_file:
        output_path = _normalize_output_file(output_file, workspace, logger)
        result["output_file"] = str(output_path)
    result["artifact_files"] = [
        str(workspace / "generated_task.py"),
        str(workspace / "task_plan.json"),
        str(workspace / "execution_result.json"),
        str(workspace / "execution_logs.jsonl"),
    ]
    if output_file:
        result["artifact_files"].append(str(output_path))
    logger.emit("completed", "自主生成代码执行完成", result)
    result["log_count"] = len(logger.logs)
    with (workspace / "execution_logs.jsonl").open("w", encoding="utf-8") as handle:
        for item in logger.logs:
            handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
    (workspace / "execution_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
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


def _emit_tool_stream(context: ToolContext, event_type: str, payload: dict[str, Any] | None = None) -> None:
    if context.stream_handler is None:
        return
    enriched = dict(payload or {})
    enriched.setdefault("run_id", context.run.id)
    enriched.setdefault("tool_name", "run_autonomous_usrp_task")
    context.stream_handler(event_type, enriched)


def _codegen_stream_handler(context: ToolContext) -> tuple[Callable[[str, dict[str, Any]], None], dict[str, bool]]:
    seen = {"reasoning": False, "content": False}

    def handler(event_type: str, payload: dict[str, Any]) -> None:
        forwarded = dict(payload or {})
        forwarded.update({"run_id": context.run.id, "tool_name": "run_autonomous_usrp_task", "phase": "code_generation"})
        if event_type == "reasoning_delta" and forwarded.get("delta"):
            seen["reasoning"] = True
        if event_type == "content_delta" and forwarded.get("delta"):
            seen["content"] = True
        if context.stream_handler is not None:
            context.stream_handler(event_type, forwarded)

    return handler, seen


def generate_code_with_llm(task_description: str, context: ToolContext, *, task_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = task_plan or _extract_plan_from_task(task_description)
    _emit_tool_stream(context, "tool_trace", {"stage": "knowledge", "message": "正在检索 USRP API 与安全 SDK 约束。"})
    store = MarkdownKnowledgeStore()
    chunks = store.search(task_description + " USRP WebSocket stream fft_data start devices configure 实时 FFT", top_k=8)
    _emit_tool_stream(context, "tool_trace", {"stage": "knowledge", "message": f"已检索到 {len(chunks)} 个相关知识片段，正在构造代码生成提示。"})
    knowledge = "\n\n".join(f"### {item.title}\n{item.text[:2200]}" for item in chunks)
    sdk_contract = """
你只能生成一个 Python 函数：def run_task(ctx):
可用对象：
- ctx.task: dict，结构化任务计划。
- ctx.emit(stage, message, data=None): 向前端输出中间过程。
- ctx.make_task_id(prefix, **parts): 生成安全 task_id。
- ctx.make_output_name(room_name, mode): 生成“会议室名称_背景/正常频谱_日期时间.npz”。
- ctx.usrp.scan_and_get_idle_device(dev_id=None): 扫描并返回 IDLE 设备。
- ctx.usrp.generate_frequency_list(start_hz, stop_hz, step_hz): 生成频点列表。
- ctx.usrp.capture_fft_once(task_id, dev_id, freq, sample_rate, bandwidth, gain, slice_duration, duration, antenna, device=None, expected_frames=1): 启动一次采集，并直接从 USRP WebSocket 接收 fft_data，返回 power_db_mean、frequency_axis_mhz、fft_frames、frame_count。
- ctx.usrp.fft_frequency_axis(center_freq, sample_rate, fft_size=1024): 生成 FFT 频率轴。
- ctx.usrp.save_spectrum_npz(output_name, **arrays): 保存最终汇总 npz，注意这个 npz 是平台基于 WebSocket FFT 生成的结果文件，不是读取采集端 slice_*.npz。
禁止 import、open、requests、subprocess、os、eval、exec。
禁止调用 load_task_iq、compute_fft_power_db、task_dir 或读取 slice_*.npz。
不要直接访问 HTTP/WebSocket 原始接口，只能使用 ctx.usrp。
""".strip()
    prompt = f"""
你是 DeepEM 的代码生成型 USRP 频谱采集智能体。请根据用户任务、结构化计划和 USRP API 知识，生成可执行的 run_task(ctx) 函数。

用户任务：
{task_description}

结构化计划：
{json.dumps(plan, ensure_ascii=False, indent=2)}

USRP API 知识片段：
{knowledge}

安全 SDK 契约：
{sdk_contract}

输出要求：
1. 只输出 Python 代码，不要解释。
2. 只定义 def run_task(ctx): 一个函数。
3. 对多频点、多次重复采集要循环执行 capture_fft_once，并使用其返回的 power_db_mean 做平均。
4. 绝对不要读取 slice_*.npz，不要调用 load_task_iq，不要自己计算 IQ FFT；直接使用 USRP 平台 WebSocket 返回的 fft_data。
5. 保存最终汇总结果 npz，并 return dict，至少包含 status、output_file、freq_count、repeat_count、data_source。
""".strip()
    code = ""
    llm_error = ""
    strict_llm = bool(plan.get("strict_llm", False))
    if context.llm_client is not None:
        try:
            _emit_tool_stream(context, "tool_trace", {"stage": "code_generation", "message": "正在调用模型生成受控 USRP 采集函数。"})
            llm_stream, seen = _codegen_stream_handler(context)
            response = context.llm_client.complete(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                temperature=0.1,
                stream_handler=llm_stream,
                cancel_checker=context.cancel_checker,
            )
            # 兼容不主动触发 stream_handler、但在最终响应中提供 reasoning/content 的客户端。
            if response.reasoning and not seen["reasoning"]:
                _emit_tool_stream(context, "reasoning_start", {"phase": "code_generation"})
                _emit_tool_stream(context, "reasoning_delta", {"phase": "code_generation", "delta": response.reasoning})
                _emit_tool_stream(context, "reasoning_done", {"phase": "code_generation"})
            if response.content and not seen["content"]:
                _emit_tool_stream(context, "content_start", {"phase": "code_generation"})
                _emit_tool_stream(context, "content_delta", {"phase": "code_generation", "delta": response.content})
                _emit_tool_stream(context, "content_done", {"phase": "code_generation", "finish_reason": "stop"})
            code = _extract_code(response.content)
        except Exception as exc:
            llm_error = str(exc)
            _emit_tool_stream(context, "tool_trace", {"stage": "code_generation", "message": f"模型代码生成失败，正在执行安全回退：{exc}"})
    if not code:
        if strict_llm:
            raise AutonomousUsrpError(f"真实 LLM 未返回采集代码：{llm_error or 'empty response'}")
        code = _fallback_generated_code(plan)
        llm_error = llm_error or "LLM 未返回代码，已使用内置 WebSocket FFT fallback 采集函数。"
    _emit_tool_stream(context, "tool_trace", {"stage": "validation", "message": "代码生成完成，正在进行 AST 静态安全检查与输出目录规则检查。"})
    try:
        validation = validate_generated_code(code)
    except Exception as exc:
        if strict_llm:
            raise AutonomousUsrpError(f"真实 LLM 生成的采集代码未通过安全检查：{exc}") from exc
        code = _fallback_generated_code(plan)
        validation = validate_generated_code(code)
        llm_error = (llm_error + "；" if llm_error else "") + f"模型代码未通过安全检查（{exc}），已自动替换为 WebSocket FFT 安全模板。"
        _emit_tool_stream(context, "tool_trace", {"stage": "validation", "message": f"模型代码未通过安全检查，已替换为安全模板：{exc}"})
    _emit_tool_stream(context, "tool_trace", {"stage": "validation", "message": "代码静态安全检查通过，准备进入受控执行环境。"})
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
    result = execute_generated_code(code, task_plan, context)
    data = {"execution_result": result, "task_plan": task_plan, "code": code, "data_source": "usrp_websocket_fft"}
    if context.stream_handler is not None:
        context.stream_handler("tool_result", {"run_id": context.run.id, "tool_name": "execute_usrp_task_code", "data": data})
    return ToolExecutionResult(result=ToolResult(status="success", data=data, metadata={"display_in_chat": True}))


def _run_autonomous_usrp_task(args: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
    task_description = str(args.get("task_description") or (context.trigger_message.content if context.trigger_message else ""))
    user_plan = args.get("task_plan") if isinstance(args.get("task_plan"), dict) else None
    _emit_tool_stream(context, "tool_trace", {"stage": "initializing", "message": "端到端自主采集工具已启动，正在解析已批准的采集子计划。"})
    generated = generate_code_with_llm(task_description, context, task_plan=user_plan)
    if context.stream_handler is not None:
        context.stream_handler("tool_result", {"run_id": context.run.id, "tool_name": "generate_usrp_task_code", "data": generated})
    _emit_tool_stream(context, "tool_trace", {"stage": "execution", "message": "采集函数已锁定，开始连接设备并执行端到端采集。"})
    execution = execute_generated_code(generated["code"], dict(generated["task_plan"]), context)
    _emit_tool_stream(context, "tool_trace", {"stage": "completed", "message": "设备采集与统一目录归档完成，正在汇总工具结果。"})
    data = {
        "stage": "completed",
        "task_description": task_description,
        "task_plan": generated["task_plan"],
        "generated_code": generated["code"],
        "validation": generated["validation"],
        "knowledge_chunks": generated["knowledge_chunks"],
        "llm_error": generated.get("llm_error", ""),
        "execution_result": execution,
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
            },
        },
        handler=_run_autonomous_usrp_task,
    )
