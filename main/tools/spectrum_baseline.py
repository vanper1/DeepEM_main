from __future__ import annotations

import io
import json
import math
import tempfile
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
import numpy as np

from deepem.protocol import ToolResult, new_id, utc_now
from deepem.spectrum_visualization import load_spectrum_npz
from deepem.tools.base import ToolContext, ToolDefinition, ToolExecutionResult


DEFAULT_THRESHOLD_DB = 8.0
DEFAULT_DC_EXCLUSION_BINS = 5
DEFAULT_EDGE_EXCLUSION_BINS = 2
DEFAULT_MAX_PEAKS = 2000


def _format_freq(freq_hz: float | int | None) -> str:
    if freq_hz is None:
        return "-"
    try:
        value = float(freq_hz)
    except Exception:
        return str(freq_hz)
    if abs(value) >= 1e9:
        return f"{value / 1e9:.6g} GHz"
    if abs(value) >= 1e6:
        return f"{value / 1e6:.6g} MHz"
    if abs(value) >= 1e3:
        return f"{value / 1e3:.6g} kHz"
    return f"{value:.6g} Hz"


def _float_arg(args: dict[str, Any], key: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        value = float(args.get(key, default))
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _int_arg(args: dict[str, Any], key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(float(args.get(key, default)))
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _state_meta(context: ToolContext) -> dict[str, Any]:
    try:
        state = context.state_repo.get(context.task.id)
        return dict(state.metadata or {})
    except Exception:
        return {}


def _selected_baseline_record(context: ToolContext) -> dict[str, Any]:
    meta = _state_meta(context)
    record = meta.get("selected_spectrum_baseline")
    return dict(record) if isinstance(record, dict) else {}


def _judgement_context_record(context: ToolContext) -> dict[str, Any]:
    meta = _state_meta(context)
    record = meta.get("spectrum_judgement_context")
    return dict(record) if isinstance(record, dict) else {}


def _resolve_path(value: Any, *, context: ToolContext, role: str) -> Path:
    text = str(value or "").strip()
    if not text:
        if role == "baseline":
            text = str(_selected_baseline_record(context).get("path") or "").strip()
        elif role == "current":
            text = str(_judgement_context_record(context).get("current_npz_path") or "").strip()
    if not text:
        raise ValueError(f"未找到{role} NPZ 文件路径；请先在采集智能体中选择基线并从采集产物点击“研判”。")
    path = Path(text).expanduser().resolve()
    if path.suffix.lower() != ".npz":
        raise ValueError(f"{role} 文件不是 .npz：{path.name}")
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"{role} NPZ 文件不存在：{path}")
    return path


def _coverage_stats(data: dict[str, Any]) -> dict[str, Any]:
    intervals: list[tuple[float, float]] = []
    for axis in data["frequency_axis_hz"]:
        arr = np.asarray(axis, dtype=float).reshape(-1)
        arr = arr[np.isfinite(arr)]
        if arr.size:
            lo, hi = float(np.min(arr)), float(np.max(arr))
            if hi < lo:
                lo, hi = hi, lo
            intervals.append((lo, hi))
    if not intervals:
        return {"covered_hz": 0.0, "span_hz": 0.0, "coverage_ratio": 0.0, "interval_count": 0}
    intervals.sort()
    merged: list[list[float]] = []
    for lo, hi in intervals:
        if not merged or lo > merged[-1][1]:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    covered = sum(max(0.0, hi - lo) for lo, hi in merged)
    span = max(1.0, max(hi for _, hi in intervals) - min(lo for lo, _ in intervals))
    return {
        "covered_hz": covered,
        "span_hz": span,
        "coverage_ratio": covered / span,
        "interval_count": len(merged),
    }


def _median_bin_spacing(data: dict[str, Any]) -> float:
    spacings: list[float] = []
    for axis in data["frequency_axis_hz"]:
        arr = np.asarray(axis, dtype=float).reshape(-1)
        arr = arr[np.isfinite(arr)]
        if arr.size >= 2:
            diffs = np.diff(np.sort(arr))
            diffs = np.abs(diffs[np.isfinite(diffs) & (diffs > 0)])
            if diffs.size:
                spacings.append(float(np.median(diffs)))
    return float(np.median(spacings)) if spacings else 1.0


def _valid_mask(width: int, *, dc_exclusion_bins: int, edge_exclusion_bins: int) -> np.ndarray:
    mask = np.ones(width, dtype=bool)
    if edge_exclusion_bins > 0 and width > edge_exclusion_bins * 2:
        mask[:edge_exclusion_bins] = False
        mask[-edge_exclusion_bins:] = False
    if dc_exclusion_bins > 0 and width > dc_exclusion_bins * 2 + 1:
        center = width // 2
        lo = max(0, center - dc_exclusion_bins)
        hi = min(width, center + dc_exclusion_bins + 1)
        mask[lo:hi] = False
    return mask


def _robust_stats(values: np.ndarray) -> tuple[float, float]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 0.0, 0.0
    median = float(np.median(finite))
    mad = float(np.median(np.abs(finite - median)))
    sigma = 1.4826 * mad
    if not np.isfinite(sigma) or sigma <= 1e-9:
        sigma = float(np.std(finite)) if finite.size > 1 else 0.0
    return median, max(0.0, sigma)


def _extract_peaks_from_data(
    data: dict[str, Any],
    *,
    threshold_db: float,
    dc_exclusion_bins: int,
    edge_exclusion_bins: int,
    max_peaks: int,
    merge_hz: float | None = None,
) -> dict[str, Any]:
    median_bin_hz = _median_bin_spacing(data)
    merge_hz = float(merge_hz if merge_hz is not None and merge_hz > 0 else max(median_bin_hz * 2.5, 1.0))
    peaks: list[dict[str, Any]] = []
    dc_candidate_count = 0
    analyzed_bins = 0
    suppressed_bins = 0

    for wi, (center_hz, axis, mean, std) in enumerate(zip(data["freqs_hz"], data["frequency_axis_hz"], data["power_mean"], data["power_std"])):
        freq = np.asarray(axis, dtype=float).reshape(-1)
        power = np.asarray(mean, dtype=float).reshape(-1)
        std_arr = np.asarray(std, dtype=float).reshape(-1)
        width = min(freq.size, power.size, std_arr.size)
        if width < 3:
            continue
        freq = freq[:width]
        power = power[:width]
        std_arr = std_arr[:width]
        finite = np.isfinite(freq) & np.isfinite(power)
        mask = _valid_mask(width, dc_exclusion_bins=dc_exclusion_bins, edge_exclusion_bins=edge_exclusion_bins) & finite
        center_mask = np.zeros(width, dtype=bool)
        if dc_exclusion_bins > 0:
            center = width // 2
            center_mask[max(0, center - dc_exclusion_bins):min(width, center + dc_exclusion_bins + 1)] = True
        valid_power = power[mask]
        if valid_power.size < 3:
            continue
        noise_floor, robust_sigma = _robust_stats(valid_power)
        cutoff = noise_floor + threshold_db
        analyzed_bins += int(mask.sum())
        suppressed_bins += int((~mask & finite).sum())
        dc_values = power[center_mask & finite]
        if dc_values.size and float(np.max(dc_values)) >= cutoff:
            dc_candidate_count += 1
        for idx in range(1, width - 1):
            if not mask[idx]:
                continue
            value = float(power[idx])
            if value < cutoff:
                continue
            if not (value >= float(power[idx - 1]) and value > float(power[idx + 1])):
                continue
            peaks.append(
                {
                    "frequency_hz": float(freq[idx]),
                    "frequency_label": _format_freq(float(freq[idx])),
                    "center_frequency_hz": float(center_hz),
                    "center_frequency_label": _format_freq(float(center_hz)),
                    "power_db": round(value, 3),
                    "noise_floor_db": round(noise_floor, 3),
                    "prominence_db": round(value - noise_floor, 3),
                    "robust_sigma_db": round(robust_sigma, 3),
                    "std_db": round(float(std_arr[idx]) if np.isfinite(std_arr[idx]) else 0.0, 3),
                    "window_index": wi,
                    "bin_index": idx,
                }
            )

    peaks.sort(key=lambda item: (float(item["frequency_hz"]), -float(item["power_db"])))
    merged: list[dict[str, Any]] = []
    for peak in peaks:
        if not merged or abs(float(peak["frequency_hz"]) - float(merged[-1]["frequency_hz"])) > merge_hz:
            merged.append(peak)
            continue
        if float(peak["power_db"]) > float(merged[-1]["power_db"]):
            merged[-1] = peak
    if len(merged) > max_peaks:
        merged = sorted(merged, key=lambda item: float(item.get("prominence_db", 0.0)), reverse=True)[:max_peaks]
        merged.sort(key=lambda item: float(item["frequency_hz"]))
    return {
        "peaks": merged,
        "raw_peak_count": len(peaks),
        "merged_peak_count": len(merged),
        "analyzed_bins": analyzed_bins,
        "suppressed_bins": suppressed_bins,
        "dc_candidate_count": dc_candidate_count,
        "median_bin_spacing_hz": median_bin_hz,
        "merge_hz": merge_hz,
    }


def _flatten_for_compare(data: dict[str, Any], *, dc_exclusion_bins: int, edge_exclusion_bins: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    freq_parts: list[np.ndarray] = []
    power_parts: list[np.ndarray] = []
    std_parts: list[np.ndarray] = []
    for axis, mean, std in zip(data["frequency_axis_hz"], data["power_mean"], data["power_std"]):
        f = np.asarray(axis, dtype=float).reshape(-1)
        p = np.asarray(mean, dtype=float).reshape(-1)
        s = np.asarray(std, dtype=float).reshape(-1)
        width = min(f.size, p.size, s.size)
        if width <= 0:
            continue
        f, p, s = f[:width], p[:width], s[:width]
        mask = _valid_mask(width, dc_exclusion_bins=dc_exclusion_bins, edge_exclusion_bins=edge_exclusion_bins)
        mask &= np.isfinite(f) & np.isfinite(p)
        if mask.any():
            freq_parts.append(f[mask])
            power_parts.append(p[mask])
            std_parts.append(np.where(np.isfinite(s[mask]), s[mask], 0.0))
    if not freq_parts:
        return np.array([], dtype=float), np.array([], dtype=float), np.array([], dtype=float)
    f = np.concatenate(freq_parts)
    p = np.concatenate(power_parts)
    s = np.concatenate(std_parts)
    order = np.argsort(f)
    return f[order], p[order], s[order]


def _nearest_baseline_residual(
    baseline_freq: np.ndarray,
    baseline_power: np.ndarray,
    baseline_std: np.ndarray,
    current_freq: np.ndarray,
    current_power: np.ndarray,
    *,
    tolerance_hz: float,
) -> dict[str, np.ndarray]:
    if baseline_freq.size == 0 or current_freq.size == 0:
        return {
            "freq": np.array([], dtype=float),
            "current_power": np.array([], dtype=float),
            "baseline_power": np.array([], dtype=float),
            "baseline_std": np.array([], dtype=float),
            "residual_db": np.array([], dtype=float),
            "distance_hz": np.array([], dtype=float),
        }
    idx = np.searchsorted(baseline_freq, current_freq)
    left = np.clip(idx - 1, 0, baseline_freq.size - 1)
    right = np.clip(idx, 0, baseline_freq.size - 1)
    left_dist = np.abs(current_freq - baseline_freq[left])
    right_dist = np.abs(current_freq - baseline_freq[right])
    choose_right = right_dist < left_dist
    nearest = np.where(choose_right, right, left)
    dist = np.where(choose_right, right_dist, left_dist)
    valid = dist <= tolerance_hz
    return {
        "freq": current_freq[valid],
        "current_power": current_power[valid],
        "baseline_power": baseline_power[nearest][valid],
        "baseline_std": baseline_std[nearest][valid],
        "residual_db": current_power[valid] - baseline_power[nearest][valid],
        "distance_hz": dist[valid],
    }


def _match_peaks(current_peaks: list[dict[str, Any]], baseline_peaks: list[dict[str, Any]], *, tolerance_hz: float, enhancement_db: float) -> dict[str, Any]:
    baseline_freqs = np.asarray([float(item["frequency_hz"]) for item in baseline_peaks], dtype=float)
    new_peaks: list[dict[str, Any]] = []
    enhanced_peaks: list[dict[str, Any]] = []
    matched_peaks: list[dict[str, Any]] = []
    if baseline_freqs.size:
        order = np.argsort(baseline_freqs)
        baseline_freqs = baseline_freqs[order]
        baseline_sorted = [baseline_peaks[int(i)] for i in order]
    else:
        baseline_sorted = []
    for peak in current_peaks:
        freq = float(peak["frequency_hz"])
        if not baseline_freqs.size:
            new_peaks.append({**peak, "match_status": "new"})
            continue
        idx = int(np.searchsorted(baseline_freqs, freq))
        candidates = []
        for j in (idx - 1, idx, idx + 1):
            if 0 <= j < len(baseline_sorted):
                candidates.append((abs(freq - float(baseline_sorted[j]["frequency_hz"])), baseline_sorted[j]))
        if not candidates:
            new_peaks.append({**peak, "match_status": "new"})
            continue
        dist, baseline = min(candidates, key=lambda item: item[0])
        if dist > tolerance_hz:
            new_peaks.append({**peak, "match_status": "new", "nearest_baseline_distance_hz": round(float(dist), 3)})
            continue
        delta = float(peak["power_db"]) - float(baseline["power_db"])
        enriched = {
            **peak,
            "match_status": "matched",
            "baseline_frequency_hz": float(baseline["frequency_hz"]),
            "baseline_power_db": float(baseline["power_db"]),
            "power_delta_db": round(delta, 3),
            "nearest_baseline_distance_hz": round(float(dist), 3),
        }
        matched_peaks.append(enriched)
        if delta >= enhancement_db:
            enhanced_peaks.append({**enriched, "match_status": "enhanced"})
    return {
        "new_peaks": new_peaks,
        "enhanced_peaks": enhanced_peaks,
        "matched_peaks": matched_peaks,
    }


def _group_residual_segments(freq: np.ndarray, residual: np.ndarray, mask: np.ndarray, *, max_gap_hz: float) -> list[dict[str, Any]]:
    if freq.size == 0 or residual.size == 0 or not mask.any():
        return []
    selected_freq = freq[mask]
    selected_residual = residual[mask]
    if selected_freq.size == 0:
        return []
    order = np.argsort(selected_freq)
    selected_freq = selected_freq[order]
    selected_residual = selected_residual[order]
    groups: list[tuple[int, int]] = []
    start = 0
    for i in range(1, selected_freq.size):
        if selected_freq[i] - selected_freq[i - 1] > max_gap_hz:
            groups.append((start, i))
            start = i
    groups.append((start, selected_freq.size))
    segments: list[dict[str, Any]] = []
    for start, end in groups:
        gf = selected_freq[start:end]
        gr = selected_residual[start:end]
        if gf.size < 5:
            continue
        segments.append(
            {
                "start_hz": float(gf[0]),
                "stop_hz": float(gf[-1]),
                "start_label": _format_freq(float(gf[0])),
                "stop_label": _format_freq(float(gf[-1])),
                "bandwidth_hz": float(max(0.0, gf[-1] - gf[0])),
                "bin_count": int(gf.size),
                "mean_residual_db": round(float(np.mean(gr)), 3),
                "max_residual_db": round(float(np.max(gr)), 3),
            }
        )
    segments.sort(key=lambda item: float(item["max_residual_db"]), reverse=True)
    return segments[:100]


def _figure_to_png_bytes(fig: Figure, *, dpi: int = 150) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    fig.clear()
    return buf.getvalue()


def _save_png_asset(context: ToolContext, content: bytes, *, file_name: str, metadata: dict[str, Any]) -> str:
    manager = context.asset_manager
    if manager is not None:
        asset = manager.save_bytes(
            asset_id=new_id("asset"),
            file_name=file_name,
            content=content,
            upload_kind="spectrum_baseline_analysis",
            conversation_id=context.run.conversation_id,
            mime_type="image/png",
            metadata=metadata,
        )
        return manager.public_url(asset.asset_id)
    fallback_dir = Path(tempfile.gettempdir()) / "deepem_spectrum_baseline"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    path = fallback_dir / file_name
    path.write_bytes(content)
    return str(path)


def _plot_baseline_peaks(data: dict[str, Any], peaks: list[dict[str, Any]], *, title: str) -> bytes:
    fig = Figure(figsize=(14, 8), dpi=150, facecolor="white")
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(2, 1, 1)
    for axis, mean in zip(data["frequency_axis_hz"], data["power_mean"]):
        ax.plot(np.asarray(axis) / 1e6, np.asarray(mean), linewidth=0.75, alpha=0.75)
    if peaks:
        pf = np.asarray([float(item["frequency_hz"]) for item in peaks]) / 1e6
        pp = np.asarray([float(item["power_db"]) for item in peaks])
        ax.scatter(pf, pp, s=16, marker="x", zorder=5, label="Detected major peaks")
        ax.legend(loc="best", fontsize=8)
    ax.set_title(title)
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Power (dB)")
    ax.grid(True, alpha=0.28)

    ax2 = fig.add_subplot(2, 1, 2)
    top = sorted(peaks, key=lambda item: float(item.get("prominence_db", 0.0)), reverse=True)[:25]
    if top:
        labels = [_format_freq(item["frequency_hz"]) for item in top]
        values = [float(item["prominence_db"]) for item in top]
        ax2.bar(np.arange(len(top)), values)
        ax2.set_xticks(np.arange(len(top)))
        ax2.set_xticklabels(labels, rotation=65, ha="right", fontsize=7)
        ax2.set_ylabel("Prominence (dB)")
        ax2.set_title("Top baseline peaks by prominence")
    else:
        ax2.text(0.5, 0.5, "No major peaks over threshold", ha="center", va="center", transform=ax2.transAxes)
        ax2.set_axis_off()
    fig.tight_layout()
    return _figure_to_png_bytes(fig)


def _plot_comparison(
    baseline_data: dict[str, Any],
    current_data: dict[str, Any],
    baseline_peaks: list[dict[str, Any]],
    current_peaks: list[dict[str, Any]],
    residual_info: dict[str, np.ndarray],
    *,
    threshold_db: float,
    title: str,
) -> bytes:
    fig = Figure(figsize=(14, 9), dpi=150, facecolor="white")
    FigureCanvasAgg(fig)
    ax1 = fig.add_subplot(3, 1, 1)
    for axis, mean in zip(baseline_data["frequency_axis_hz"], baseline_data["power_mean"]):
        ax1.plot(np.asarray(axis) / 1e6, np.asarray(mean), linewidth=0.65, alpha=0.45)
    ax1.set_title("Baseline spectrum")
    ax1.set_xlabel("Frequency (MHz)")
    ax1.set_ylabel("Power (dB)")
    ax1.grid(True, alpha=0.25)

    ax2 = fig.add_subplot(3, 1, 2)
    for axis, mean in zip(current_data["frequency_axis_hz"], current_data["power_mean"]):
        ax2.plot(np.asarray(axis) / 1e6, np.asarray(mean), linewidth=0.65, alpha=0.65)
    if current_peaks:
        ax2.scatter(
            np.asarray([float(item["frequency_hz"]) for item in current_peaks]) / 1e6,
            np.asarray([float(item["power_db"]) for item in current_peaks]),
            s=14,
            marker="x",
            label="Current peaks",
            zorder=5,
        )
        ax2.legend(fontsize=8)
    ax2.set_title("Current spectrum and detected peaks")
    ax2.set_xlabel("Frequency (MHz)")
    ax2.set_ylabel("Power (dB)")
    ax2.grid(True, alpha=0.25)

    ax3 = fig.add_subplot(3, 1, 3)
    rf = residual_info.get("freq", np.array([], dtype=float))
    rr = residual_info.get("residual_db", np.array([], dtype=float))
    if rf.size and rr.size:
        ax3.plot(rf / 1e6, rr, linewidth=0.65, alpha=0.8)
        ax3.axhline(threshold_db, linestyle="--", linewidth=1.0, label=f"Threshold {threshold_db:g} dB")
        ax3.axhline(0, linestyle=":", linewidth=0.8)
        ax3.legend(fontsize=8)
    else:
        ax3.text(0.5, 0.5, "No overlapped bins for residual comparison", ha="center", va="center", transform=ax3.transAxes)
    ax3.set_title("Current minus baseline residual")
    ax3.set_xlabel("Frequency (MHz)")
    ax3.set_ylabel("Δ Power (dB)")
    ax3.grid(True, alpha=0.25)
    fig.suptitle(title, fontsize=13, fontweight="bold")
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    return _figure_to_png_bytes(fig)


def _baseline_thresholds_from_context(context: ToolContext) -> dict[str, Any]:
    record = _selected_baseline_record(context)
    judgement = _judgement_context_record(context)
    merged = {**record, **judgement}
    return dict(merged)


def _extract_baseline_spectrum_peaks(args: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
    defaults = _baseline_thresholds_from_context(context)
    threshold_db = _float_arg(args, "threshold_db", float(defaults.get("threshold_db") or DEFAULT_THRESHOLD_DB), minimum=0.1, maximum=80.0)
    dc_bins = _int_arg(args, "dc_exclusion_bins", int(defaults.get("dc_exclusion_bins") or DEFAULT_DC_EXCLUSION_BINS), minimum=0, maximum=128)
    edge_bins = _int_arg(args, "edge_exclusion_bins", int(defaults.get("edge_exclusion_bins") or DEFAULT_EDGE_EXCLUSION_BINS), minimum=0, maximum=256)
    max_peaks = _int_arg(args, "max_peaks", DEFAULT_MAX_PEAKS, minimum=1, maximum=20000)
    baseline_path = _resolve_path(args.get("baseline_npz_path"), context=context, role="baseline")
    data = load_spectrum_npz(baseline_path)
    analysis = _extract_peaks_from_data(
        data,
        threshold_db=threshold_db,
        dc_exclusion_bins=dc_bins,
        edge_exclusion_bins=edge_bins,
        max_peaks=max_peaks,
    )
    peaks = analysis["peaks"]
    coverage = _coverage_stats(data)
    top_peaks = sorted(peaks, key=lambda item: float(item.get("prominence_db", 0.0)), reverse=True)[:20]
    title = "Baseline major peaks"
    image_url = _save_png_asset(
        context,
        _plot_baseline_peaks(data, peaks, title=title),
        file_name=f"baseline_peaks_{new_id('img')}.png",
        metadata={"source_npz": str(baseline_path), "tool": "extract_baseline_spectrum_peaks"},
    )
    summary = (
        f"基线文件 {baseline_path.name} 已完成主要频谱点提取：阈值 {threshold_db:g} dB，"
        f"剔除中心伪峰 ±{dc_bins} bins、边缘 {edge_bins} bins；"
        f"共得到 {len(peaks)} 个主要频谱点，中心伪峰候选被抑制 {analysis['dc_candidate_count']} 个扫频窗口。"
    )
    result = {
        "summary": summary,
        "baseline_file_name": baseline_path.name,
        "baseline_npz_path": str(baseline_path),
        "threshold_db": threshold_db,
        "dc_exclusion_bins": dc_bins,
        "edge_exclusion_bins": edge_bins,
        "center_frequency_count": int(len(data["freqs_hz"])),
        "peak_count": int(len(peaks)),
        "raw_peak_count": int(analysis["raw_peak_count"]),
        "dc_candidate_count": int(analysis["dc_candidate_count"]),
        "median_bin_spacing_hz": float(analysis["median_bin_spacing_hz"]),
        "coverage": coverage,
        "top_peaks": top_peaks,
        "peaks": peaks[:max_peaks],
        "visualizations": [
            {"title": "Baseline major peaks", "url": image_url, "kind": "image/png"},
        ],
        "key_conclusion": f"已识别 {len(peaks)} 个基线主要频谱点；中心频点伪峰已按规则剔除，不参与异常判断。",
    }
    return ToolExecutionResult(result=ToolResult(status="success", data=result, metadata={"display_in_chat": True}))


def _compare_spectrum_with_baseline(args: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
    defaults = _baseline_thresholds_from_context(context)
    threshold_db = _float_arg(args, "threshold_db", float(defaults.get("threshold_db") or DEFAULT_THRESHOLD_DB), minimum=0.1, maximum=80.0)
    enhancement_db = _float_arg(args, "enhancement_db", float(args.get("enhancement_db") or threshold_db), minimum=0.1, maximum=80.0)
    dc_bins = _int_arg(args, "dc_exclusion_bins", int(defaults.get("dc_exclusion_bins") or DEFAULT_DC_EXCLUSION_BINS), minimum=0, maximum=128)
    edge_bins = _int_arg(args, "edge_exclusion_bins", int(defaults.get("edge_exclusion_bins") or DEFAULT_EDGE_EXCLUSION_BINS), minimum=0, maximum=256)
    max_peaks = _int_arg(args, "max_peaks", DEFAULT_MAX_PEAKS, minimum=1, maximum=20000)

    baseline_path = _resolve_path(args.get("baseline_npz_path"), context=context, role="baseline")
    current_path = _resolve_path(args.get("current_npz_path"), context=context, role="current")
    baseline_data = load_spectrum_npz(baseline_path)
    current_data = load_spectrum_npz(current_path)
    baseline_peak_result = _extract_peaks_from_data(
        baseline_data,
        threshold_db=threshold_db,
        dc_exclusion_bins=dc_bins,
        edge_exclusion_bins=edge_bins,
        max_peaks=max_peaks,
    )
    current_peak_result = _extract_peaks_from_data(
        current_data,
        threshold_db=threshold_db,
        dc_exclusion_bins=dc_bins,
        edge_exclusion_bins=edge_bins,
        max_peaks=max_peaks,
    )
    baseline_peaks = baseline_peak_result["peaks"]
    current_peaks = current_peak_result["peaks"]

    b_freq, b_power, b_std = _flatten_for_compare(baseline_data, dc_exclusion_bins=dc_bins, edge_exclusion_bins=edge_bins)
    c_freq, c_power, c_std = _flatten_for_compare(current_data, dc_exclusion_bins=dc_bins, edge_exclusion_bins=edge_bins)
    median_bin_hz = max(_median_bin_spacing(baseline_data), _median_bin_spacing(current_data), 1.0)
    tolerance_hz = _float_arg(args, "frequency_tolerance_hz", max(median_bin_hz * 3.0, 1.0), minimum=1.0)
    residual_tolerance_hz = _float_arg(args, "residual_tolerance_hz", max(median_bin_hz * 1.5, tolerance_hz), minimum=1.0)

    matched = _match_peaks(current_peaks, baseline_peaks, tolerance_hz=tolerance_hz, enhancement_db=enhancement_db)
    residual_info = _nearest_baseline_residual(b_freq, b_power, b_std, c_freq, c_power, tolerance_hz=residual_tolerance_hz)
    residual = residual_info["residual_db"]
    if residual.size:
        over_mask = residual >= threshold_db
        residual_summary = {
            "overlapped_bin_count": int(residual.size),
            "bins_over_threshold": int(np.sum(over_mask)),
            "ratio_over_threshold": float(np.sum(over_mask) / max(1, residual.size)),
            "median_residual_db": round(float(np.median(residual)), 3),
            "p95_residual_db": round(float(np.percentile(residual, 95)), 3),
            "max_residual_db": round(float(np.max(residual)), 3),
        }
        wideband_segments = _group_residual_segments(
            residual_info["freq"],
            residual,
            over_mask,
            max_gap_hz=max(median_bin_hz * 3.0, 1.0),
        )
    else:
        residual_summary = {
            "overlapped_bin_count": 0,
            "bins_over_threshold": 0,
            "ratio_over_threshold": 0.0,
            "median_residual_db": 0.0,
            "p95_residual_db": 0.0,
            "max_residual_db": 0.0,
        }
        wideband_segments = []

    new_peaks = matched["new_peaks"]
    enhanced_peaks = matched["enhanced_peaks"]
    score = len(new_peaks) * 2.0 + len(enhanced_peaks) * 1.3 + len(wideband_segments) * 3.0
    score += max(0.0, float(residual_summary["p95_residual_db"]) - threshold_db) / 2.0
    if len(new_peaks) or len(enhanced_peaks) or len(wideband_segments):
        verdict = "异常"
    elif residual_summary["bins_over_threshold"] > 0:
        verdict = "疑似异常"
    else:
        verdict = "正常"
    if score >= 12 or len(wideband_segments) >= 3:
        risk_level = "high"
    elif score >= 4 or verdict == "异常":
        risk_level = "medium"
    else:
        risk_level = "low"

    title = f"Baseline comparison · threshold {threshold_db:g} dB"
    image_url = _save_png_asset(
        context,
        _plot_comparison(
            baseline_data,
            current_data,
            baseline_peaks,
            current_peaks,
            residual_info,
            threshold_db=threshold_db,
            title=title,
        ),
        file_name=f"baseline_compare_{new_id('img')}.png",
        metadata={"baseline_npz": str(baseline_path), "current_npz": str(current_path), "tool": "compare_spectrum_with_baseline"},
    )
    summary = (
        f"当前采集 {current_path.name} 与基线 {baseline_path.name} 完成比对：判定={verdict}，风险={risk_level}；"
        f"新增峰 {len(new_peaks)} 个，已有峰增强 {len(enhanced_peaks)} 个，宽带抬升段 {len(wideband_segments)} 个；"
        f"残差 P95={residual_summary['p95_residual_db']} dB，阈值={threshold_db:g} dB。"
    )
    result = {
        "summary": summary,
        "key_conclusion": summary,
        "verdict": verdict,
        "risk_level": risk_level,
        "abnormal_score": round(float(score), 3),
        "threshold_db": threshold_db,
        "enhancement_db": enhancement_db,
        "frequency_tolerance_hz": tolerance_hz,
        "residual_tolerance_hz": residual_tolerance_hz,
        "dc_exclusion_bins": dc_bins,
        "edge_exclusion_bins": edge_bins,
        "baseline_file_name": baseline_path.name,
        "current_file_name": current_path.name,
        "baseline_peak_count": int(len(baseline_peaks)),
        "current_peak_count": int(len(current_peaks)),
        "new_peak_count": int(len(new_peaks)),
        "enhanced_peak_count": int(len(enhanced_peaks)),
        "matched_peak_count": int(len(matched["matched_peaks"])),
        "wideband_segment_count": int(len(wideband_segments)),
        "residual_summary": residual_summary,
        "new_peaks": sorted(new_peaks, key=lambda item: float(item.get("prominence_db", 0.0)), reverse=True)[:50],
        "enhanced_peaks": sorted(enhanced_peaks, key=lambda item: float(item.get("power_delta_db", 0.0)), reverse=True)[:50],
        "wideband_segments": wideband_segments[:30],
        "visualizations": [
            {"title": "Baseline comparison and residual", "url": image_url, "kind": "image/png"},
        ],
        "judgement_basis": [
            "峰值比对：当前主要峰在基线频点容差内找不到对应峰时记为新增峰。",
            "功率增强：当前峰与基线峰匹配但功率差超过可调阈值时记为已有峰增强。",
            "全谱残差：剔除中心伪峰和边缘 bin 后，计算当前功率减基线功率，识别宽带抬升。",
            "阈值越低越敏感；阈值越高越保守。",
        ],
    }
    return ToolExecutionResult(result=ToolResult(status="success", data=result, metadata={"display_in_chat": True}))


def build_extract_baseline_spectrum_peaks_tool() -> ToolDefinition:
    return ToolDefinition(
        name="extract_baseline_spectrum_peaks",
        description=(
            "从当前选定的场所基线 .npz 中提取所有主要频谱点；自动剔除扫频窗口中心 DC/本振泄漏伪峰和边缘 bin；"
            "threshold_db 越低越敏感，越高越保守。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "baseline_npz_path": {"type": "string"},
                "threshold_db": {"type": "number"},
                "dc_exclusion_bins": {"type": "integer"},
                "edge_exclusion_bins": {"type": "integer"},
                "max_peaks": {"type": "integer"},
            },
        },
        handler=_extract_baseline_spectrum_peaks,
    )


def build_compare_spectrum_with_baseline_tool() -> ToolDefinition:
    return ToolDefinition(
        name="compare_spectrum_with_baseline",
        description=(
            "将当前采集 .npz 与当前选定的场所基线 .npz 做异常研判；不只比较新增峰，也比较已有峰增强和全谱残差/宽带噪声底抬升；"
            "threshold_db 越低越敏感，越高越不容易判异常。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "baseline_npz_path": {"type": "string"},
                "current_npz_path": {"type": "string"},
                "threshold_db": {"type": "number"},
                "enhancement_db": {"type": "number"},
                "frequency_tolerance_hz": {"type": "number"},
                "residual_tolerance_hz": {"type": "number"},
                "dc_exclusion_bins": {"type": "integer"},
                "edge_exclusion_bins": {"type": "integer"},
                "max_peaks": {"type": "integer"},
            },
        },
        handler=_compare_spectrum_with_baseline,
    )
