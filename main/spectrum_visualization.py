from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
import numpy as np

_RENDER_LOCK = threading.RLock()
_CJK_FONT_AVAILABLE = False


def _configure_fonts() -> None:
    global _CJK_FONT_AVAILABLE
    candidates = (
        "Noto Sans CJK SC",
        "Source Han Sans CN",
        "WenQuanYi Micro Hei",
        "Microsoft YaHei",
        "PingFang SC",
        "SimHei",
    )
    for name in candidates:
        try:
            fm.findfont(name, fallback_to_default=False)
        except Exception:
            continue
        matplotlib.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
        _CJK_FONT_AVAILABLE = True
        break
    matplotlib.rcParams["axes.unicode_minus"] = False


_configure_fonts()


def _scalar(value: Any, default: Any = "") -> Any:
    if value is None:
        return default
    if isinstance(value, np.ndarray):
        if value.size == 0:
            return default
        value = value.flat[0]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, np.generic):
        return value.item()
    return value


def _display_text(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if _CJK_FONT_AVAILABLE or text.isascii():
        return text
    ascii_text = "".join(ch for ch in text if ch.isascii() and (ch.isalnum() or ch in " _-./" )).strip()
    return ascii_text or fallback


def _json_value(raw: Any) -> dict[str, Any]:
    value = _scalar(raw, "")
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _as_rows(value: Any, *, name: str) -> list[np.ndarray]:
    arr = np.asarray(value, dtype=object if getattr(value, "dtype", None) == object else None)
    if arr.ndim == 0:
        raise ValueError(f"{name} 不是有效数组")
    if arr.dtype != object:
        numeric = np.asarray(arr, dtype=float)
        if numeric.ndim == 1:
            return [numeric]
        if numeric.ndim == 2:
            return [numeric[i] for i in range(numeric.shape[0])]
        raise ValueError(f"{name} 维度不受支持：{numeric.shape}")
    rows: list[np.ndarray] = []
    for item in arr:
        row = np.asarray(item, dtype=float).reshape(-1)
        if row.size:
            rows.append(row)
    if not rows:
        raise ValueError(f"{name} 中没有有效数据")
    return rows


def _as_repeat_groups(value: Any, freq_count: int) -> list[list[np.ndarray]]:
    arr = np.asarray(value, dtype=object if getattr(value, "dtype", None) == object else None)
    groups: list[list[np.ndarray]] = []
    if arr.dtype != object:
        numeric = np.asarray(arr, dtype=float)
        if numeric.ndim == 2:
            numeric = numeric[:, None, :]
        if numeric.ndim != 3:
            raise ValueError(f"重复采集数组维度不受支持：{numeric.shape}")
        for fi in range(numeric.shape[0]):
            groups.append([numeric[fi, ri] for ri in range(numeric.shape[1])])
    else:
        for freq_item in arr:
            freq_arr = np.asarray(freq_item, dtype=object if getattr(freq_item, "dtype", None) == object else None)
            if freq_arr.dtype != object:
                numeric = np.asarray(freq_arr, dtype=float)
                if numeric.ndim == 1:
                    numeric = numeric[None, :]
                groups.append([numeric[ri].reshape(-1) for ri in range(numeric.shape[0])])
            else:
                group = [np.asarray(item, dtype=float).reshape(-1) for item in freq_arr if np.asarray(item).size]
                groups.append(group)
    if len(groups) != freq_count:
        raise ValueError(f"重复采集频点数不匹配：{len(groups)} != {freq_count}")
    return groups


def load_spectrum_npz(path: Path | str) -> dict[str, Any]:
    source = Path(path).resolve()
    if source.suffix.lower() != ".npz":
        raise ValueError("仅支持 .npz 频谱结果")
    with np.load(source, allow_pickle=True) as npz:
        raw = {key: npz[key] for key in npz.files}

    freqs_raw = raw.get("freqs_mhz", raw.get("center_freqs_mhz", raw.get("freqs_hz", raw.get("center_freqs_hz"))))
    means_raw = raw.get("power_mean", raw.get("power_db_mean"))
    if freqs_raw is None or means_raw is None:
        raise ValueError("NPZ 缺少 freqs_mhz/center_freqs_mhz 或 power_mean/power_db_mean")

    freqs = np.asarray(freqs_raw, dtype=float).reshape(-1)
    if freqs.size and float(np.nanmax(np.abs(freqs))) > 100000:
        freqs = freqs / 1e6
    mean_rows = _as_rows(means_raw, name="power_mean")
    if len(mean_rows) != len(freqs):
        if len(freqs) == 1 and len(mean_rows) > 1:
            freqs = np.arange(len(mean_rows), dtype=float)
        else:
            raise ValueError(f"中心频率数量与功率数组不匹配：{len(freqs)} != {len(mean_rows)}")

    repeats_raw = raw.get("power_repeats", raw.get("power_db_repeats"))
    if repeats_raw is None:
        repeat_groups = [[row.copy()] for row in mean_rows]
    else:
        repeat_groups = _as_repeat_groups(repeats_raw, len(mean_rows))

    std_raw = raw.get("power_std", raw.get("power_db_std"))
    if std_raw is not None:
        std_rows = _as_rows(std_raw, name="power_std")
    else:
        std_rows = []
        for group in repeat_groups:
            width = min(row.size for row in group)
            std_rows.append(np.std(np.vstack([row[:width] for row in group]), axis=0))

    config = {}
    for key in ("config_json", "task_plan_json", "metadata_json", "config", "metadata"):
        if key in raw:
            config.update(_json_value(raw[key]))

    axis_raw = raw.get("frequency_axis_mhz", raw.get("frequency_axes_mhz", raw.get("frequency_axis_hz", raw.get("frequency_axes_hz"))))
    if axis_raw is not None:
        axis_rows = _as_rows(axis_raw, name="frequency_axis_mhz")
        axis_rows = [row / 1e6 if row.size and float(np.nanmax(np.abs(row))) > 100000 else row for row in axis_rows]
        if len(axis_rows) == 1 and len(mean_rows) > 1:
            shared = axis_rows[0]
            offset = shared - float(freqs[0])
            axis_rows = [float(freq) + offset for freq in freqs]
    else:
        sample_rate = float(config.get("sample_rate") or config.get("sample_rate_hz") or _scalar(raw.get("sample_rate", 0), 0) or 0)
        if abs(sample_rate) > 100000:
            sample_rate = sample_rate / 1e6
        axis_rows = []
        for freq, row in zip(freqs, mean_rows):
            width = row.size
            if sample_rate > 0:
                axis_rows.append(float(freq) + (np.arange(width) - width / 2) * sample_rate / width)
            else:
                axis_rows.append(np.full(width, float(freq)))

    count = len(mean_rows)
    if len(axis_rows) != count or len(std_rows) != count:
        raise ValueError("频率轴或标准差数组的频点数不匹配")

    normalized_mean: list[np.ndarray] = []
    normalized_std: list[np.ndarray] = []
    normalized_axis: list[np.ndarray] = []
    normalized_repeats: list[list[np.ndarray]] = []
    for mean, std, axis, group in zip(mean_rows, std_rows, axis_rows, repeat_groups):
        valid_group = [np.asarray(row, dtype=float).reshape(-1) for row in group if np.asarray(row).size]
        if not valid_group:
            valid_group = [np.asarray(mean, dtype=float).reshape(-1)]
        width = min([np.asarray(mean).size, np.asarray(std).size, np.asarray(axis).size, *[row.size for row in valid_group]])
        if width <= 0:
            raise ValueError("NPZ 中存在空频谱")
        normalized_mean.append(np.asarray(mean, dtype=float).reshape(-1)[:width])
        normalized_std.append(np.asarray(std, dtype=float).reshape(-1)[:width])
        normalized_axis.append(np.asarray(axis, dtype=float).reshape(-1)[:width])
        normalized_repeats.append([row[:width] for row in valid_group])

    return {
        "source": source,
        "freqs_mhz": freqs,
        "frequency_axis_mhz": normalized_axis,
        "power_mean": normalized_mean,
        "power_std": normalized_std,
        "power_repeats": normalized_repeats,
        "config": config,
        "room_name": str(_scalar(raw.get("room_name"), config.get("room_name", "?"))),
        "device_id": str(_scalar(raw.get("device_id"), config.get("device_id", "?"))),
        "mode": str(_scalar(raw.get("mode"), config.get("mode", "?"))),
        "created_at": str(_scalar(raw.get("created_at"), "?")),
        "task_id": str(_scalar(raw.get("task_id"), "?")),
        "plan_id": str(_scalar(raw.get("plan_id"), "?")),
        "dry_run": str(_scalar(raw.get("dry_run"), "?")),
    }


def _cache_path(source: Path) -> Path:
    return source.with_name(f".{source.stem}.spectrum.png")


def render_spectrum_png(path: Path | str, *, force: bool = False, dpi: int = 150) -> Path:
    source = Path(path).resolve()
    cache = _cache_path(source)
    if not force and cache.exists() and cache.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return cache

    with _RENDER_LOCK:
        if not force and cache.exists() and cache.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return cache
        data = load_spectrum_npz(source)
        freqs = data["freqs_mhz"]
        axes = data["frequency_axis_mhz"]
        means = data["power_mean"]
        stds = data["power_std"]
        repeats = data["power_repeats"]
        config = data["config"]
        count = len(freqs)
        repeat_count = max(len(group) for group in repeats)
        labels = [f"{freq:.6g} MHz" for freq in freqs]

        fig = Figure(figsize=(16, 10), dpi=dpi, facecolor="white")
        FigureCanvasAgg(fig)
        room_label = _display_text(data["room_name"], "Collection Site")
        mode_label = _display_text(data["mode"], "spectrum")
        device_label = _display_text(data["device_id"], "unknown")
        fig.suptitle(
            f"{room_label} Spectrum Collection · {freqs[0]:.6g}–{freqs[-1]:.6g} MHz · "
            f"{count} center frequencies × {repeat_count} repeats · {mode_label}",
            fontsize=14,
            fontweight="bold",
        )

        ax1 = fig.add_subplot(2, 3, (1, 3))
        colors = matplotlib.colormaps["viridis"](np.linspace(0.15, 0.9, count))
        for index, (axis, mean, std) in enumerate(zip(axes, means, stds)):
            mhz = axis if float(np.nanmax(np.abs(axis))) < 100000 else axis / 1e6
            ax1.plot(mhz, mean, color=colors[index], linewidth=1.0, alpha=0.9)
            ax1.fill_between(mhz, mean - std, mean + std, color=colors[index], alpha=0.12)
        ax1.set_xlabel("Frequency (MHz)")
        ax1.set_ylabel("Power (dB)")
        ax1.set_title("Panoramic Spectrum (center frequencies stitched, shaded = ±1σ)")
        ax1.grid(True, alpha=0.3)
        for freq in freqs:
            ax1.axvline(freq, color="gray", linestyle=":", linewidth=0.5, alpha=0.4)

        ax2 = fig.add_subplot(2, 3, 4)
        colors = matplotlib.colormaps["viridis"](np.linspace(0, 1, count))
        for index, (axis, mean) in enumerate(zip(axes, means)):
            ax2.plot(axis, mean, color=colors[index], linewidth=0.8, alpha=0.8, label=labels[index])
        ax2.set_xlabel("Frequency (MHz)")
        ax2.set_ylabel("Power Mean (dB)")
        ax2.set_title("Per-Frequency Power Spectrum (mean)")
        if count <= 24:
            ax2.legend(fontsize=5, loc="lower right", ncol=2)
        ax2.grid(True, alpha=0.3)

        ax3 = fig.add_subplot(2, 3, 5)
        mid = count // 2
        axis = axes[mid]
        for ri, repeat in enumerate(repeats[mid]):
            ax3.plot(axis, repeat, linewidth=0.8, alpha=0.7, label=f"Repeat {ri + 1}")
        ax3.plot(axis, means[mid], "k--", linewidth=1.2, label="Mean")
        ax3.fill_between(axis, means[mid] - stds[mid], means[mid] + stds[mid], alpha=0.2, color="gray", label="±1σ")
        ax3.set_xlabel("Frequency (MHz)")
        ax3.set_ylabel("Power (dB)")
        ax3.set_title(f"Repeat Consistency @ {labels[mid]}")
        ax3.legend(fontsize=7)
        ax3.grid(True, alpha=0.3)

        ax4 = fig.add_subplot(2, 3, 6)
        peaks = np.asarray([row.max() for row in means], dtype=float)
        peak_stds = np.asarray([stds[i][int(np.argmax(means[i]))] for i in range(count)], dtype=float)
        ax4.errorbar(np.arange(count), peaks, yerr=peak_stds, fmt="o-", capsize=5, linewidth=1.5, markersize=6)
        ax4.set_xticks(np.arange(count))
        ax4.set_xticklabels(labels, rotation=45, fontsize=7)
        ax4.set_ylabel("Max Power (dB)")
        ax4.set_title("Peak Power per Center Frequency")
        ax4.grid(True, alpha=0.3)

        sample_rate = float(config.get("sample_rate") or config.get("sample_rate_hz") or 0)
        bandwidth = float(config.get("bandwidth") or config.get("bandwidth_hz") or 0)
        if abs(sample_rate) > 100000:
            sample_rate = sample_rate / 1e6
        if abs(bandwidth) > 100000:
            bandwidth = bandwidth / 1e6
        dwell = config.get("dwell_ms", config.get("dwell_time_sec", "?"))
        dwell_text = f"{dwell} ms" if "dwell_ms" in config else (f"{dwell} s" if dwell != "?" else "?")
        summary = [
            f"Device: {device_label}  |  Created: {_display_text(data['created_at'], '?')}",
            f"Range: {freqs[0]:.6g} – {freqs[-1]:.6g} MHz  |  Center frequencies: {count}",
            f"Sample rate: {sample_rate:.6g} MHz  |  Bandwidth: {bandwidth:.6g} MHz  |  Gain: {config.get('gain', '?')} dB",
            f"Repeats: {repeat_count}  |  Aggregation: {_display_text(config.get('aggregation', '?'), '?')}  |  Dwell: {dwell_text}",
            f"Task: {_display_text(data['task_id'], '?')}  |  Plan: {_display_text(data['plan_id'], '?')}  |  Dry-run: {data['dry_run']}",
        ]
        fig.text(0.05, 0.012, "\n".join(summary), fontsize=8, va="bottom", ha="left")
        fig.tight_layout(rect=[0, 0.07, 1, 0.95])
        cache.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(cache, dpi=dpi, bbox_inches="tight", facecolor="white")
        fig.clear()
        return cache
