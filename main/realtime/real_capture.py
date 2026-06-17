from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


DEFAULT_BURST_SIGMA = 50.0
DEFAULT_HISTORY_LEN = 12
DEFAULT_MIN_EVENT_SCORE = 30.0
DEFAULT_COOLDOWN_SCANS = 4


@dataclass(slots=True)
class ChannelHistory:
    power_hist: deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LEN))
    burst_hist: deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LEN))
    peak_hist: deque[float] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LEN))
    cooldown: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "power_hist": list(self.power_hist),
            "burst_hist": list(self.burst_hist),
            "peak_hist": list(self.peak_hist),
            "cooldown": self.cooldown,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "ChannelHistory":
        obj = cls()
        if not payload:
            return obj
        obj.power_hist.extend(float(item) for item in payload.get("power_hist", []))
        obj.burst_hist.extend(float(item) for item in payload.get("burst_hist", []))
        obj.peak_hist.extend(float(item) for item in payload.get("peak_hist", []))
        obj.cooldown = int(payload.get("cooldown", 0))
        return obj


class SessionAnalyzer:
    def __init__(self, channels: list[int]):
        self.channels = channels
        self.channel_state: dict[int, ChannelHistory] = {int(ch): ChannelHistory() for ch in channels}

    def process(self, iq: np.ndarray, meta: dict[str, Any]) -> dict[str, Any]:
        channel = int(meta.get("channel") or infer_channel_from_frequency(float(meta.get("freq") or meta.get("center_freq") or 0.0)))
        state = self.channel_state.setdefault(channel, ChannelHistory())
        feat = compute_features(iq)

        power_z = zscore_against_history(feat["power"], state.power_hist)
        burst_z = zscore_against_history(feat["burst_ratio"], state.burst_hist)
        peak_z = zscore_against_history(feat["peak_power"], state.peak_hist)
        score = 0.45 * power_z + 0.40 * burst_z + 0.15 * peak_z

        state.power_hist.append(feat["power"])
        state.burst_hist.append(feat["burst_ratio"])
        state.peak_hist.append(feat["peak_power"])

        alert = False
        alert_reasons: list[str] = []
        if len(state.power_hist) >= 5:
            if state.cooldown == 0 and score >= DEFAULT_MIN_EVENT_SCORE:
                alert = True
                state.cooldown = DEFAULT_COOLDOWN_SCANS
                alert_reasons.append("robust_zscore_threshold")
            elif state.cooldown > 0:
                state.cooldown -= 1

        # # Warm-up fallback to make the workflow usable before sufficient history accumulates.
        # if not alert and (feat["crest"] >= 6.5 or feat["burst_ratio"] >= 0.025):
        #     alert = True
        #     alert_reasons.append("warmup_feature_threshold")

        return {
            "scan_index": int(meta.get("scan_index", 0)),
            "channel": channel,
            "freq": float(meta.get("freq", 0.0)),
            "timestamp": str(meta.get("timestamp", "")),
            "power": feat["power"],
            "burst_ratio": feat["burst_ratio"],
            "peak_power": feat["peak_power"],
            "crest": feat["crest"],
            "noise_med": feat["noise_med"],
            "noise_mad": feat["noise_mad"],
            "burst_thr": feat["burst_thr"],
            "power_z": power_z,
            "burst_z": burst_z,
            "peak_z": peak_z,
            "score": score,
            "alert": alert,
            "alert_reasons": alert_reasons,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "channels": list(self.channels),
            "channel_state": {str(ch): state.to_payload() for ch, state in self.channel_state.items()},
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None, channels: list[int]) -> "SessionAnalyzer":
        analyzer = cls(channels=payload.get("channels", channels) if payload else channels)
        if payload:
            for key, value in dict(payload.get("channel_state") or {}).items():
                analyzer.channel_state[int(key)] = ChannelHistory.from_payload(value)
        return analyzer


@dataclass(slots=True)
class AdaptedRealtimeSignal:
    signal_id: str
    batch_id: str
    fingerprint: str
    classification: str
    carries_information: bool
    suspected_device_type: str | None
    features: dict[str, Any]
    observation_record: dict[str, Any]


def _meta_text(meta: dict[str, Any], key: str) -> str:
    value = meta.get(key)
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _meta_bool(meta: dict[str, Any], key: str, default: bool) -> bool:
    if key not in meta:
        return default
    value = meta.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是", "有"}


class RealSignalAdapter:
    @staticmethod
    def adapt(
        *,
        analysis: dict[str, Any],
        meta: dict[str, Any],
        filepath: Path,
        iq: np.ndarray,
    ) -> AdaptedRealtimeSignal:
        session_id = str(meta.get("session_id", "session"))
        channel = int(meta.get("channel", 0))
        scan_index = int(meta.get("scan_index", 0))
        timestamp = str(meta.get("timestamp", ""))
        batch_id = f"{session_id}_scan{scan_index:04d}_ch{channel}"
        signal_id = f"real_{session_id}_{scan_index:04d}_ch{channel}"
        classification_hint = _meta_text(meta, "classification_hint") or _meta_text(meta, "classification")
        classification_hint = classification_hint.lower()
        if classification_hint in {"observed", "suspicious", "unknown"}:
            classification = classification_hint
        else:
            classification = "suspicious" if analysis.get("alert") else "observed"
        is_alert = bool(analysis.get("alert") or classification == "suspicious")
        carries_information = _meta_bool(meta, "carries_information", bool(analysis.get("burst_ratio", 0.0) >= 0.01 or is_alert))
        fingerprint = _meta_text(meta, "fingerprint") or f"wifi-ch{channel}-{'alert' if is_alert else 'scan'}"
        suspected_device_type = _meta_text(meta, "suspected_device_type") or "wifi_2.4g"
        semantic_fields = {
            "source_table": _meta_text(meta, "source_table"),
            "source_name": _meta_text(meta, "source_name"),
            "frequency_label": _meta_text(meta, "frequency_label"),
            "bandwidth_label": _meta_text(meta, "bandwidth_label"),
            "frequency_shape": _meta_text(meta, "frequency_shape"),
            "power_dbm": _meta_text(meta, "power_dbm"),
            "risk_reason": _meta_text(meta, "risk_reason"),
            "db_reference": _meta_text(meta, "db_reference"),
            "ssid": _meta_text(meta, "ssid"),
            "mac": _meta_text(meta, "mac"),
            "bd_addr": _meta_text(meta, "bd_addr"),
            "device_name": _meta_text(meta, "device_name"),
            "item_name": _meta_text(meta, "item_name"),
            "category_hint": _meta_text(meta, "category_hint"),
            "risk_level_hint": _meta_text(meta, "risk_level_hint"),
        }
        semantic_fields = {key: value for key, value in semantic_fields.items() if value}
        alert_reasons = list(analysis.get("alert_reasons", []))
        if is_alert and semantic_fields.get("risk_reason") and "semantic_risk_hint" not in alert_reasons:
            alert_reasons.append("semantic_risk_hint")

        features = {
            "power": round(float(analysis.get("power", 0.0)), 8),
            "peak_power": round(float(analysis.get("peak_power", 0.0)), 8),
            "burst_ratio": round(float(analysis.get("burst_ratio", 0.0)), 8),
            "crest": round(float(analysis.get("crest", 0.0)), 6),
            "power_z": round(float(analysis.get("power_z", 0.0)), 6),
            "burst_z": round(float(analysis.get("burst_z", 0.0)), 6),
            "peak_z": round(float(analysis.get("peak_z", 0.0)), 6),
            "score": round(float(analysis.get("score", 0.0)), 6),
            "alert": is_alert,
            "alert_reasons": alert_reasons,
            "rms_energy": round(math.sqrt(max(float(analysis.get("power", 0.0)), 0.0)), 6),
            "peak_amplitude": round(math.sqrt(max(float(analysis.get("peak_power", 0.0)), 0.0)), 6),
            # Keep original structure keys for UI compatibility while switching to real parsed values.
            "baseline_deviation_db": round(float(analysis.get("score", 0.0)), 6),
            "peak_to_rms": round(float(analysis.get("crest", 0.0)), 6),
            "high_frequency_score": round(float(analysis.get("burst_ratio", 0.0)), 8),
            "channel": channel,
            "freq": float(meta.get("freq", 0.0)),
            "sample_rate": float(meta.get("sample_rate", 0.0)),
            "raw_file": str(filepath),
            "file_name": filepath.name,
            "iq_sample_count": int(len(iq)),
            "collector_id": str(meta.get("collector_id", "")),
            "capture_info": dict(meta.get("capture_info") or {}),
        }
        features.update(semantic_fields)
        observation_record = {
            "timestamp": timestamp,
            "signal_id": signal_id,
            "batch_id": batch_id,
            "file_name": filepath.name,
            "channel": channel,
            "freq": float(meta.get("freq", 0.0)),
            "rms_energy": features["rms_energy"],
            "classification": classification,
            "classification_label": "异常" if classification == "suspicious" else "正常",
            "fingerprint": fingerprint,
            "carries_information": carries_information,
            "baseline_deviation_db": features["baseline_deviation_db"],
            "high_frequency_score": features["high_frequency_score"],
            "peak_to_rms": features["peak_to_rms"],
            "score": features["score"],
            "suspected_device_type": suspected_device_type,
        }
        observation_record.update(semantic_fields)
        return AdaptedRealtimeSignal(
            signal_id=signal_id,
            batch_id=batch_id,
            fingerprint=fingerprint,
            classification=classification,
            carries_information=carries_information,
            suspected_device_type=suspected_device_type,
            features=features,
            observation_record=observation_record,
        )


def parse_npz(path: Path) -> tuple[np.ndarray, dict[str, Any]]:
    with np.load(path, allow_pickle=True) as npz:
        iq = np.asarray(npz["iq"], dtype=np.complex64)
        if "meta" in npz:
            raw_meta = npz["meta"]
            if isinstance(raw_meta, np.ndarray):
                meta = raw_meta.item()
            else:
                meta = raw_meta
        else:
            meta = {
                "sample_rate": _npz_scalar(npz, "sample_rate", 0.0),
                "freq": _npz_scalar(npz, "center_freq", _npz_scalar(npz, "freq", 0.0)),
                "center_freq": _npz_scalar(npz, "center_freq", _npz_scalar(npz, "freq", 0.0)),
                "bandwidth": _npz_scalar(npz, "bandwidth", 0.0),
                "gain": _npz_scalar(npz, "gain", 0.0),
                "antenna": _npz_scalar(npz, "antenna", None),
                "captured_at": _npz_scalar(npz, "captured_at", None),
            }
    meta = dict(meta)
    freq = float(meta.get("freq") or meta.get("center_freq") or 0.0)
    meta.setdefault("freq", freq)
    meta.setdefault("center_freq", freq)
    meta.setdefault("sample_rate", float(meta.get("sample_rate") or 0.0))
    meta.setdefault("channel", infer_channel_from_frequency(freq))
    meta.setdefault("scan_index", infer_scan_index_from_filename(path.name))
    meta.setdefault("timestamp", timestamp_from_meta(meta))
    return iq, meta


def _npz_scalar(npz: Any, key: str, default: Any) -> Any:
    if key not in npz:
        return default
    value = npz[key]
    if isinstance(value, np.ndarray) and value.shape == ():
        return value.item()
    return value


def timestamp_from_meta(meta: dict[str, Any]) -> str:
    timestamp = meta.get("timestamp")
    if timestamp:
        return str(timestamp)
    captured_at = meta.get("captured_at")
    if captured_at is not None:
        try:
            return datetime.fromtimestamp(float(captured_at), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            pass
    return datetime.now(timezone.utc).isoformat()


def infer_scan_index_from_filename(filename: str) -> int:
    match = re.search(r"slice_(\d+)", filename, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"scan(\d+)", filename, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 0


def infer_channel_from_frequency(freq_hz: float) -> int:
    if freq_hz <= 0:
        return 0
    freq_mhz = freq_hz / 1_000_000.0
    if 2401.0 <= freq_mhz <= 2484.0:
        if abs(freq_mhz - 2484.0) <= 1.5:
            return 14
        channel = round((freq_mhz - 2407.0) / 5.0)
        if 1 <= channel <= 13:
            return int(channel)
    return int(round(freq_mhz))


def robust_noise_floor(x_mag: np.ndarray) -> tuple[float, float]:
    med = np.median(x_mag)
    mad = np.median(np.abs(x_mag - med)) + 1e-12
    return float(med), float(mad)


def compute_features(iq: np.ndarray, burst_sigma: float = DEFAULT_BURST_SIGMA) -> dict[str, float]:
    mag = np.abs(iq).astype(np.float64)
    power = float(np.mean(mag**2))
    peak = float(np.max(mag**2))
    med, mad = robust_noise_floor(mag)
    burst_thr = med + burst_sigma * mad
    burst_ratio = float(np.mean(mag > burst_thr))
    crest = float((np.max(mag) + 1e-12) / (math.sqrt(power) + 1e-12))
    return {
        "power": power,
        "peak_power": peak,
        "burst_ratio": burst_ratio,
        "crest": crest,
        "noise_med": med,
        "noise_mad": mad,
        "burst_thr": float(burst_thr),
    }


def zscore_against_history(value: float, hist: deque[float]) -> float:
    if len(hist) < 4:
        return 0.0
    arr = np.array(list(hist), dtype=np.float64)
    med = np.median(arr)
    mad = np.median(np.abs(arr - med)) + 1e-12
    robust_std = 1.4826 * mad + 1e-12
    return float((value - med) / robust_std)


def build_spectrogram_payload(iq: np.ndarray, sample_rate: float) -> dict[str, Any]:
    if iq.size == 0:
        iq = np.zeros(256, dtype=np.complex64)
    iq = np.asarray(iq, dtype=np.complex64)
    if iq.size < 128:
        iq = np.pad(iq, (0, 128 - iq.size))

    nperseg = min(256, max(64, 2 ** int(math.log2(iq.size if iq.size > 0 else 64))))
    if nperseg > iq.size:
        nperseg = iq.size
    if nperseg < 32:
        nperseg = min(32, iq.size)
    if nperseg % 2 == 1:
        nperseg -= 1
    nperseg = max(nperseg, 32)
    hop = max(nperseg // 2, 1)
    window = np.hanning(nperseg).astype(np.float32)

    frames: list[np.ndarray] = []
    for start in range(0, max(iq.size - nperseg + 1, 1), hop):
        chunk = iq[start : start + nperseg]
        if chunk.size < nperseg:
            chunk = np.pad(chunk, (0, nperseg - chunk.size))
        spec = np.fft.fftshift(np.fft.fft(chunk * window, n=nperseg))
        power_db = 20.0 * np.log10(np.abs(spec) + 1e-12)
        frames.append(power_db.astype(np.float32))
    if not frames:
        spec = np.fft.fftshift(np.fft.fft(iq[:nperseg] * window, n=nperseg))
        frames.append((20.0 * np.log10(np.abs(spec) + 1e-12)).astype(np.float32))

    matrix = np.stack(frames, axis=1)
    matrix = _resize_2d(matrix, target_h=min(96, matrix.shape[0]), target_w=min(128, matrix.shape[1]))
    vmin = float(np.percentile(matrix, 5))
    vmax = float(np.percentile(matrix, 98))
    if vmax <= vmin:
        vmax = vmin + 1.0
    norm = np.clip((matrix - vmin) / (vmax - vmin), 0.0, 1.0)
    pixels = np.round(norm * 255.0).astype(np.uint8)
    spectrum_db = np.mean(matrix, axis=1)
    spectrum_bins = _downsample_1d(spectrum_db, target_len=128)
    duration_ms = 1000.0 * float(iq.size / max(sample_rate, 1.0))
    return {
        "width": int(pixels.shape[1]),
        "height": int(pixels.shape[0]),
        "pixels": pixels.tolist(),
        "value_min_db": round(vmin, 3),
        "value_max_db": round(vmax, 3),
        "sample_rate_hz": float(sample_rate),
        "duration_ms": round(duration_ms, 3),
        "freq_min_hz": round(-float(sample_rate) / 2.0, 3),
        "freq_max_hz": round(float(sample_rate) / 2.0, 3),
        "spectrum_db": [round(float(item), 3) for item in spectrum_bins],
    }


def _resize_2d(matrix: np.ndarray, *, target_h: int, target_w: int) -> np.ndarray:
    src_h, src_w = matrix.shape
    y_idx = np.linspace(0, src_h - 1, num=max(1, target_h)).astype(int)
    x_idx = np.linspace(0, src_w - 1, num=max(1, target_w)).astype(int)
    return matrix[np.ix_(y_idx, x_idx)]


def _downsample_1d(values: np.ndarray, *, target_len: int) -> np.ndarray:
    if values.size <= target_len:
        return values.astype(np.float64)
    idx = np.linspace(0, values.size - 1, num=target_len).astype(int)
    return values[idx].astype(np.float64)
