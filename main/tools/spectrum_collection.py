"""
标准化 USRP 频谱采集流程工具 —— Agent-side sweep orchestration。

=== 重要提示（实现者必读）===

当前 USRP 后端 (10.112.210.4:8100) 仅支持单频点 POST /api/usrp/start。
本模块中的 execute_spectrum_collection 在 Agent 侧逐频点、逐 repeat 循环编排采集，
是 Agent-side sweep fallback。

未来如果 USRP 后端新增 POST /api/usrp/sweep（接收频率列表、原生执行全频段扫描），
应将 execute_spectrum_collection 替换为调用后端原生 sweep task，移除客户端 for 循环。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from deepem.protocol import ToolResult, new_id
from deepem.tools.autonomous_usrp import (
    AUTONOMOUS_OUTPUT_ROOT,
    AutonomousUsrpError,
    SafeExecutionLogger,
    SafeUsrpRuntime,
)
from deepem.tools.base import EventDraft, ToolContext, ToolDefinition, ToolExecutionResult

# ------ 输出目录 ------------------------------------------------------------

_SPECTRUM_COLLECTION_ROOT = Path(__file__).resolve().parent.parent / "data" / "spectrum_collections"

# ------ 参数策略 ------------------------------------------------------------

_VALID_AGGREGATIONS = {"mean", "median"}

_EXPLICIT_PLAN_FIELDS = (
    "room_name",
    "mode",
    "freq_start_mhz",
    "freq_stop_mhz",
    "freq_step_mhz",
    "dwell_ms",
    "repeat_count",
    "aggregation",
    "sample_rate",
    "bandwidth",
    "gain",
    "antenna",
)


# ------ 参数合并 & 校验 ------------------------------------------------------

def _build_explicit_plan(args: dict[str, Any]) -> dict[str, Any]:
    """Build a plan only from resolved task parameters; never inject defaults."""
    missing = [
        key for key in _EXPLICIT_PLAN_FIELDS
        if key not in args or args.get(key) is None or args.get(key) == ""
    ]
    if missing:
        raise ValueError("缺少显式采集参数，禁止使用默认值补齐：" + "、".join(missing))
    plan = {key: args[key] for key in _EXPLICIT_PLAN_FIELDS}
    for key in ("device_id", "dry_run", "parameter_sources"):
        if key in args and args.get(key) is not None:
            plan[key] = args[key]
    return plan


def _validate_plan(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    freq_start = plan.get("freq_start_mhz")
    freq_stop = plan.get("freq_stop_mhz")
    freq_step = plan.get("freq_step_mhz")
    dwell_ms = plan.get("dwell_ms")
    repeat_count = plan.get("repeat_count")
    aggregation = plan.get("aggregation")
    sample_rate = plan.get("sample_rate")
    bandwidth = plan.get("bandwidth")
    gain = plan.get("gain")

    if not isinstance(freq_start, (int, float)) or freq_start <= 0:
        errors.append("freq_start_mhz 必须为正数")
    if not isinstance(freq_stop, (int, float)) or freq_stop <= 0:
        errors.append("freq_stop_mhz 必须为正数")
    if not isinstance(freq_step, (int, float)) or freq_step <= 0:
        errors.append("freq_step_mhz 必须为正数")
    if freq_start is not None and freq_stop is not None and isinstance(freq_start, (int, float)) and isinstance(freq_stop, (int, float)):
        if freq_stop < freq_start:
            errors.append(f"freq_stop_mhz ({freq_stop}) 必须大于等于 freq_start_mhz ({freq_start})")

    if not isinstance(dwell_ms, (int, float)) or dwell_ms <= 0:
        errors.append("dwell_ms 必须为正数")
    if not isinstance(repeat_count, int) or repeat_count <= 0:
        errors.append("repeat_count 必须为正整数")
    if aggregation not in _VALID_AGGREGATIONS:
        errors.append(f"aggregation 必须是 {' / '.join(sorted(_VALID_AGGREGATIONS))}，当前值: {aggregation}")

    if not isinstance(sample_rate, (int, float)) or sample_rate <= 0:
        errors.append("sample_rate 必须为正数")
    if not isinstance(bandwidth, (int, float)) or bandwidth <= 0:
        errors.append("bandwidth 必须为正数")
    if isinstance(sample_rate, (int, float)) and isinstance(bandwidth, (int, float)) and bandwidth > sample_rate:
        errors.append(f"bandwidth ({bandwidth}) 不能大于 sample_rate ({sample_rate})")
    if gain is not None and (not isinstance(gain, (int, float))):
        errors.append("gain 必须为数值")

    return errors


def _estimate_summary(plan: dict[str, Any]) -> dict[str, Any]:
    freq_start = float(plan["freq_start_mhz"])
    freq_stop = float(plan["freq_stop_mhz"])
    freq_step = float(plan["freq_step_mhz"])
    dwell_ms = float(plan["dwell_ms"])
    repeat_count = int(plan["repeat_count"])
    freq_count = max(1, int((freq_stop - freq_start) / freq_step) + 1)
    captured_sec_per_freq = dwell_ms / 1000.0 * repeat_count
    estimated_total_sec = freq_count * captured_sec_per_freq
    return {
        "freq_count": freq_count,
        "captured_sec_per_freq": round(captured_sec_per_freq, 3),
        "estimated_total_sec": round(estimated_total_sec, 1),
    }


def _query_idle_device(context: ToolContext) -> dict[str, Any] | None:
    """查询当前 IDLE 状态的 USRP 设备，不触发 scan。"""
    adapter = context.device_registry.find_by_capability("list_usrp_devices")
    if adapter is None:
        return None
    try:
        from deepem.devices.abstract import DeviceCommand

        result = adapter.execute(DeviceCommand(device_id=adapter.device_id, capability="list_usrp_devices", args={}))
        devices = result.data.get("devices", []) if result.data else []
        idle_devices = [d for d in devices if str(d.get("status") or "").upper() == "IDLE"]
        if idle_devices:
            return dict(idle_devices[0])
        return {"devices": devices, "note": "当前无 IDLE 设备"}
    except Exception:
        return None


# ------ 状态持久化 ----------------------------------------------------------

SPECTRUM_META_KEY = "spectrum_collection"


def _load_pending_plan(context: ToolContext, plan_id: str) -> dict[str, Any] | None:
    state = context.state_repo.get(context.task.id)
    meta = state.metadata.get(SPECTRUM_META_KEY)
    if not isinstance(meta, dict):
        return None
    return meta.get("pending_plans", {}).get(plan_id)


def _is_plan_completed(context: ToolContext, plan_id: str) -> bool:
    state = context.state_repo.get(context.task.id)
    meta = state.metadata.get(SPECTRUM_META_KEY)
    if not isinstance(meta, dict):
        return False
    return plan_id in (meta.get("completed_plan_ids") or [])


def _save_pending_plan(context: ToolContext, plan: dict[str, Any]) -> None:
    state = context.state_repo.get(context.task.id)
    meta = state.metadata.get(SPECTRUM_META_KEY)
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("pending_plans", {})[plan["plan_id"]] = plan
    meta.setdefault("completed_plan_ids", [])
    state.metadata[SPECTRUM_META_KEY] = meta
    context.state_repo.save(state)


def _mark_plan_completed(context: ToolContext, plan_id: str) -> None:
    state = context.state_repo.get(context.task.id)
    meta = state.metadata.get(SPECTRUM_META_KEY)
    if not isinstance(meta, dict):
        return
    meta.setdefault("completed_plan_ids", [])
    if plan_id not in meta["completed_plan_ids"]:
        meta["completed_plan_ids"].append(plan_id)
    meta.get("pending_plans", {}).pop(plan_id, None)
    state.metadata[SPECTRUM_META_KEY] = meta
    context.state_repo.save(state)


# ------ NPZ 保存 ------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]+", "_", name).strip()


def _save_collection_npz(*, room_name: str, plan: dict[str, Any], freqs_mhz: list[float],
                         all_power_repeats: list[list[np.ndarray]],
                         all_power_mean: list[np.ndarray],
                         all_power_std: list[np.ndarray],
                         all_freq_axis: list[np.ndarray] | None,
                         device_id: str,
                         task_id: str,
                         dry_run: bool) -> str:
    """将全频段采集结果保存为 .npz 文件。

    如果各频点 FFT bin 数一致，power_repeats 保存为 3D 数组 (freq_count × repeat_count × fft_bins)；
    如果不一致，保存为 object array 并在 metadata 中记录。
    """
    _SPECTRUM_COLLECTION_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_room = _sanitize_filename(str(room_name))
    safe_mode = _sanitize_filename(str(plan["mode"]))
    filename = f"{safe_room}_{safe_mode}_频谱_{timestamp}.npz"
    filepath = _SPECTRUM_COLLECTION_ROOT / filename

    config = {
        "room_name": room_name,
        "mode": plan["mode"],
        "device_id": device_id,
        "freq_start_mhz": plan["freq_start_mhz"],
        "freq_stop_mhz": plan["freq_stop_mhz"],
        "freq_step_mhz": plan["freq_step_mhz"],
        "dwell_ms": plan["dwell_ms"],
        "repeat_count": plan["repeat_count"],
        "aggregation": plan["aggregation"],
        "sample_rate": plan["sample_rate"],
        "bandwidth": plan["bandwidth"],
        "gain": plan["gain"],
        "antenna": plan.get("antenna"),
    }

    payload: dict[str, Any] = {
        "freqs_mhz": np.asarray(freqs_mhz, dtype=float),
        "power_mean": np.asarray(all_power_mean, dtype=float),
        "power_std": np.asarray(all_power_std, dtype=float),
        "config_json": np.array(json.dumps(config, ensure_ascii=False)),
        "room_name": np.array(room_name),
        "mode": np.array(plan["mode"]),
        "device_id": np.array(device_id),
        "created_at": np.array(datetime.now(timezone.utc).isoformat()),
        "task_id": np.array(task_id),
        "plan_id": np.array(plan.get("plan_id", "")),
        "operator_confirmed": np.array(True),
        "dry_run": np.array(bool(dry_run)),
    }

    # frequency_axis_mhz
    if all_freq_axis:
        unique_axes = set(arr.shape[0] if isinstance(arr, np.ndarray) else len(arr) for arr in all_freq_axis)
        if len(unique_axes) == 1:
            payload["frequency_axis_mhz"] = np.asarray(all_freq_axis, dtype=float)
        else:
            payload["frequency_axis_mhz"] = np.array(all_freq_axis, dtype=object)

    # power_repeats: (freq_count, repeat_count, fft_bins) 或 object array
    fft_sizes: set[int] = set()
    for repeats in all_power_repeats:
        for arr in repeats:
            fft_sizes.add(arr.shape[0] if isinstance(arr, np.ndarray) else len(arr))

    if len(fft_sizes) <= 1:
        # 所有频点 FFT bin 数一致 → 3D 数组
        stacked = []
        for repeats in all_power_repeats:
            stacked.append(np.asarray(repeats, dtype=float))
        payload["power_repeats"] = np.array(stacked, dtype=float)
    else:
        # FFT bin 数不一致 → object array
        payload["power_repeats"] = np.array(all_power_repeats, dtype=object)
        payload["power_repeats_note"] = np.array(
            f"不同频点 FFT bin 数不一致，已保存为 object array。各频点 repeat 数组形状: "
            + "; ".join(f"freq[{i}]: {[r.shape for r in repeats]}" for i, repeats in enumerate(all_power_repeats))
        )

    np.savez_compressed(filepath, **payload)
    return str(filepath)


# ------ Handler: prepare_spectrum_collection ---------------------------------

def _prepare_spectrum_collection(args: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
    room_name = str(args.get("room_name", "")).strip()
    if not room_name:
        return ToolExecutionResult(result=ToolResult(status="error", error="room_name 是必填参数"))

    try:
        plan = _build_explicit_plan(args)
    except ValueError as exc:
        return ToolExecutionResult(result=ToolResult(status="error", error=str(exc)))
    errors = _validate_plan(plan)
    if errors:
        return ToolExecutionResult(result=ToolResult(status="error", error="参数校验失败：" + "；".join(errors)))

    # 频点数预估（使用 SafeUsrpRuntime.generate_frequency_list 的校验逻辑）
    try:
        freq_start = float(plan["freq_start_mhz"])
        freq_stop = float(plan["freq_stop_mhz"])
        freq_step = float(plan["freq_step_mhz"])
        freq_count = max(1, int((freq_stop - freq_start) / freq_step) + 1)
        max_freqs = 800000
        if freq_count > max_freqs:
            return ToolExecutionResult(result=ToolResult(
                status="error",
                error=f"频点数量 {freq_count} 超过上限 {max_freqs}，请增大步长或缩小频率范围",
            ))
    except ValueError as exc:
        return ToolExecutionResult(result=ToolResult(status="error", error=str(exc)))

    summary = _estimate_summary(plan)
    plan.update(summary)

    # 查询设备状态（只读，不 scan）
    idle_info = _query_idle_device(context)
    plan["device_status"] = idle_info
    plan["device_id"] = plan.get("device_id") or (idle_info.get("dev_id") if isinstance(idle_info, dict) else None)

    # 生成 plan_id 并持久化
    plan_id = new_id("scplan")
    plan["plan_id"] = plan_id
    plan["dry_run"] = bool(args.get("dry_run", False))  # dry_run 来自 prepare args，记录到 plan
    _save_pending_plan(context, plan)

    prepare_result = {
        "plan_id": plan_id,
        "room_name": room_name,
        "mode": plan["mode"],
        "freq_start_mhz": plan["freq_start_mhz"],
        "freq_stop_mhz": plan["freq_stop_mhz"],
        "freq_step_mhz": plan["freq_step_mhz"],
        "dwell_ms": plan["dwell_ms"],
        "repeat_count": plan["repeat_count"],
        "aggregation": plan["aggregation"],
        "sample_rate": plan["sample_rate"],
        "bandwidth": plan["bandwidth"],
        "gain": plan["gain"],
        "antenna": plan.get("antenna"),
        "freq_count": plan["freq_count"],
        "estimated_total_sec": plan["estimated_total_sec"],
        "device_id": plan.get("device_id"),
        "device_status": idle_info,
        "dry_run": plan["dry_run"],
        "confirmation_prompt": "请开启会议室所有批准使用的无线设备，使其处于正常工作状态；完成后确认执行。",
    }
    return ToolExecutionResult(result=ToolResult(status="success", data=prepare_result))


# ------ Handler: execute_spectrum_collection ---------------------------------

def _execute_spectrum_collection(args: dict[str, Any], context: ToolContext) -> ToolExecutionResult:
    plan_id = str(args.get("plan_id", "")).strip()
    if not plan_id:
        return ToolExecutionResult(result=ToolResult(status="error", error="plan_id 是必填参数"))

    if args.get("operator_confirmed") is not True:
        return ToolExecutionResult(result=ToolResult(status="error", error="操作员未确认执行。请设置 operator_confirmed=true 后重试。"))

    plan = _load_pending_plan(context, plan_id)
    if plan is None:
        return ToolExecutionResult(result=ToolResult(status="error", error=f"未找到待执行的采集计划: {plan_id}。请先调用 prepare_spectrum_collection。"))

    if _is_plan_completed(context, plan_id):
        return ToolExecutionResult(result=ToolResult(status="error", error=f"采集计划 {plan_id} 已执行完成，不能重复执行。"))

    # ---- 执行阶段参数从 plan 读取，不允许覆盖 ----
    freq_start = float(plan["freq_start_mhz"])
    freq_stop = float(plan["freq_stop_mhz"])
    freq_step = float(plan["freq_step_mhz"])
    dwell_ms = float(plan["dwell_ms"])
    repeat_count = int(plan["repeat_count"])
    aggregation = str(plan["aggregation"])
    sample_rate = float(plan["sample_rate"])
    bandwidth = float(plan["bandwidth"])
    gain = float(plan["gain"])
    antenna = plan.get("antenna")
    dry_run = bool(plan.get("dry_run", False))
    room_name = str(plan["room_name"])

    # ---- 创建 SafeUsrpRuntime ----
    logger = SafeExecutionLogger(stream_handler=context.stream_handler, run_id=context.run.id)
    usrp = SafeUsrpRuntime(logger=logger, cancel_checker=context.cancel_checker)

    # ---- 生成频率列表 ----
    try:
        freqs = usrp.generate_frequency_list(freq_start, freq_stop, freq_step)
    except (ValueError, AutonomousUsrpError) as exc:
        return ToolExecutionResult(result=ToolResult(status="error", error=str(exc)))
    freq_count = len(freqs)

    # ---- 扫描设备 ----
    try:
        plan_dev_id = plan.get("device_id")
        device = usrp.scan_and_get_idle_device(plan_dev_id if plan_dev_id else None)
        dev_id = device["dev_id"]
    except Exception as exc:
        return ToolExecutionResult(result=ToolResult(status="error", error=f"无法获取 IDLE USRP 设备: {exc}"))

    dwell_sec = dwell_ms / 1000.0

    all_power_repeats: list[list[np.ndarray]] = []
    all_power_mean: list[np.ndarray] = []
    all_power_std: list[np.ndarray] = []
    all_freq_axis: list[np.ndarray] = []
    failed_freqs: list[dict[str, Any]] = []  # 完全失败的频点
    partial_freqs: list[dict[str, Any]] = []  # 部分 repeat 失败的频点

    # ---- 逐频点、逐 repeat 循环采集 ----
    for fi, freq in enumerate(freqs):
        # 每频点前检查取消
        try:
            usrp.check_cancelled()
        except AutonomousUsrpError:
            _mark_plan_completed(context, plan_id)
            return ToolExecutionResult(result=ToolResult(status="error", error="用户取消采集任务"))

        logger.emit("progress", f"采集频点 {fi + 1}/{freq_count}: {freq:.6g} MHz", {"freq_mhz": freq, "freq_index": fi})

        repeat_powers: list[np.ndarray] = []
        freq_axis: np.ndarray | None = None
        freq_failures: list[str] = []  # 该频点内失败的 repeat 描述

        for ri in range(repeat_count):
            try:
                usrp.check_cancelled()
            except AutonomousUsrpError:
                _mark_plan_completed(context, plan_id)
                return ToolExecutionResult(result=ToolResult(status="error", error="用户取消采集任务"))

            task_id = usrp.make_task_id("sweep", f=fi, r=ri)
            try:
                capture = usrp.capture_fft_once(
                    task_id=task_id,
                    dev_id=dev_id,
                    freq=freq * 1e6,
                    sample_rate=sample_rate * 1e6,
                    bandwidth=bandwidth * 1e6,
                    gain=gain,
                    slice_duration=dwell_sec,
                    duration=dwell_sec,
                    antenna=antenna,
                    device=device,
                )
            except Exception as exc:
                err_msg = f"repeat {ri + 1}/{repeat_count}: {exc}"
                freq_failures.append(err_msg)
                logger.emit("error",
                            f"⚠️ 频点 {fi + 1}/{freq_count} ({freq:.6g} MHz) repeat {ri + 1}/{repeat_count} 失败: {exc}",
                            {"freq_mhz": freq, "repeat": ri + 1, "error": str(exc)})
                continue

            repeat_powers.append(capture["power_db_mean"])
            if freq_axis is None:
                freq_axis = capture.get("frequency_axis_mhz")
                if freq_axis is None:
                    raw_axis = capture.get("frequency_axis_hz")
                    if raw_axis is not None:
                        try:
                            freq_axis = np.asarray(raw_axis, dtype=float) / 1e6
                        except Exception:
                            freq_axis = raw_axis

            logger.emit("progress",
                        f"  频点 {fi + 1} repeat {ri + 1}/{repeat_count} 完成",
                        {"freq_mhz": freq, "repeat": ri + 1, "frame_count": capture.get("frame_count")})

        # ---- 频点聚合（至少有一个 repeat 成功才纳入结果）----
        if not repeat_powers:
            err_detail = "; ".join(freq_failures) if freq_failures else "所有 repeat 均失败"
            logger.emit("error",
                        f"❌ 频点 {fi + 1}/{freq_count} ({freq:.6g} MHz) 所有 {repeat_count} 次 repeat 均失败，已跳过。原因: {err_detail}",
                        {"freq_mhz": freq, "freq_index": fi, "failures": freq_failures})
            failed_freqs.append({"freq_mhz": freq, "freq_index": fi, "failures": freq_failures})
            continue

        # 该频点有部分 repeat 成功；如果也有失败的，发出部分成功警告
        if freq_failures:
            logger.emit("progress",
                        f"⚠️ 频点 {fi + 1}/{freq_count} ({freq:.6g} MHz) {len(repeat_powers)}/{repeat_count} 次 repeat 成功（{len(freq_failures)} 次失败: {'; '.join(freq_failures)}）",
                        {"freq_mhz": freq, "success_repeats": len(repeat_powers), "failed_repeats": len(freq_failures)})
            partial_freqs.append({
                "freq_mhz": round(freq, 6),
                "success_repeats": len(repeat_powers),
                "failed_repeats": len(freq_failures),
                "failures": freq_failures,
            })

        repeats_array = np.array(repeat_powers)
        if aggregation == "median":
            mean_power = np.median(repeats_array, axis=0)
        else:
            mean_power = np.mean(repeats_array, axis=0)
        std_power = np.std(repeats_array, axis=0)

        all_power_repeats.append(repeat_powers)
        all_power_mean.append(mean_power)
        all_power_std.append(std_power)
        if freq_axis is not None:
            all_freq_axis.append(freq_axis)

    # ---- 检查是否有成功采集的数据 ----
    if not all_power_mean:
        _mark_plan_completed(context, plan_id)
        return ToolExecutionResult(result=ToolResult(
            status="error",
            error=f"所有 {freq_count} 个频点均采集失败，未生成 NPZ。失败原因: {failed_freqs[:3]}",
        ))

    # ---- 保存 NPZ（仅保存成功采集的频点）----
    success_freqs = [freqs[i] for i in range(len(freqs))
                     if i not in {f["freq_index"] for f in failed_freqs}]
    try:
        output_file = _save_collection_npz(
            room_name=room_name,
            plan=plan,
            freqs_mhz=success_freqs,
            all_power_repeats=all_power_repeats,
            all_power_mean=all_power_mean,
            all_power_std=all_power_std,
            all_freq_axis=all_freq_axis if all_freq_axis else None,
            device_id=dev_id,
            task_id=context.task.id,
            dry_run=dry_run,
        )
    except Exception as exc:
        _mark_plan_completed(context, plan_id)
        return ToolExecutionResult(result=ToolResult(status="error", error=f"NPZ 保存失败: {exc}"))

    # ---- 标记完成 ----
    _mark_plan_completed(context, plan_id)

    success_count = len(all_power_mean)
    failed_count = len(failed_freqs)
    partial_count = len(partial_freqs)
    has_issues = failed_count > 0 or partial_count > 0

    summary = {
        "plan_id": plan_id,
        "room_name": room_name,
        "mode": plan["mode"],
        "output_file": output_file,
        "freq_count": freq_count,
        "success_count": success_count,
        "failed_count": failed_count,
        "partial_count": partial_count,
        "repeat_count": repeat_count,
        "aggregation": aggregation,
        "captured_total_sec": round(freq_count * repeat_count * dwell_sec, 1),
        "device_id": dev_id,
        "dry_run": dry_run,
        "status": "completed" if not has_issues else "partial",
    }
    if failed_freqs:
        summary["failed_freqs"] = [
            {"freq_mhz": round(f["freq_mhz"], 6), "failures": f.get("failures", [])}
            for f in failed_freqs
        ]
    if partial_freqs:
        summary["partial_freqs"] = partial_freqs  # 已含 freq_mhz 和 failures

    # 有失败时在 ToolResult 中也带 error 字段，确保前端醒目提示
    result_error = None
    if has_issues:
        lines = ["以下频点采集异常:"]
        for f in failed_freqs:
            lines.append(f"  ❌ {f['freq_mhz']:.6g} MHz — 完全失败 ({'; '.join(f.get('failures', []))})")
        for f in partial_freqs:
            lines.append(f"  ⚠️ {f['freq_mhz']} MHz — {f['success_repeats']}/{repeat_count} 次成功 ({'; '.join(f['failures'])})")
        result_error = "\n".join(lines)

    event_draft = EventDraft(
        event_type="collection.completed",
        source="tool:execute_spectrum_collection",
        payload=summary,
        idempotency_key=f"spectrum_collection:{context.task.id}:{plan_id}",
    )

    return ToolExecutionResult(
        result=ToolResult(status="success", data=summary, error=result_error, metadata={"display_in_chat": True}),
        event_drafts=[event_draft],
    )


# ------ Factory -------------------------------------------------------------

def build_prepare_spectrum_collection_tool() -> ToolDefinition:
    return ToolDefinition(
        name="prepare_spectrum_collection",
        description=(
            "【标准化频谱采集 - 第一步】生成并校验采集计划，不启动真实采集。"
            "所有采集参数必须已经由用户指令、模板或采集智能体的意图设计明确解析；"
            "本工具不提供预设、不注入默认参数，也不静默补齐缺失字段。"
            "返回 plan_id、频点数量、预计耗时和操作员确认提示。"
            "用户确认后必须调用 execute_spectrum_collection 执行采集。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "room_name": {"type": "string", "description": "会议室名称，必填。"},
                "mode": {"type": "string", "description": "本次采集模式或任务类型，必须由用户、模板或意图设计明确给出。"},
                "freq_start_mhz": {"type": "number", "description": "起始频率，MHz。"},
                "freq_stop_mhz": {"type": "number", "description": "终止频率，MHz。"},
                "freq_step_mhz": {"type": "number", "description": "频率步长，MHz。"},
                "dwell_ms": {"type": "number", "description": "每频点扫描时间，毫秒。"},
                "repeat_count": {"type": "integer", "description": "每频点扫描次数。"},
                "aggregation": {"type": "string", "description": "聚合方式: mean 或 median。"},
                "device_id": {"type": "string", "description": "指定 USRP 设备 ID，不填则自动选择 IDLE 设备。"},
                "sample_rate": {"type": "number", "description": "采样率，MHz。"},
                "bandwidth": {"type": "number", "description": "接收带宽，MHz。"},
                "gain": {"type": "number", "description": "接收增益，dB。"},
                "antenna": {"type": "string", "description": "天线端口: TX/RX 或 RX2。"},
                "dry_run": {"type": "boolean", "description": "是否使用模拟数据执行后续采集。"},
            },
            "required": [
                "room_name",
                "mode",
                "freq_start_mhz",
                "freq_stop_mhz",
                "freq_step_mhz",
                "dwell_ms",
                "repeat_count",
                "aggregation",
                "sample_rate",
                "bandwidth",
                "gain",
                "antenna",
            ],
        },
        handler=_prepare_spectrum_collection,
    )


def build_execute_spectrum_collection_tool() -> ToolDefinition:
    return ToolDefinition(
        name="execute_spectrum_collection",
        description=(
            "【标准化全频段频谱采集 - 第二步】执行由 prepare_spectrum_collection 生成的采集计划。"
            "逐频点、逐 repeat 完成多频点采集、多轮平均，生成 .npz 结果文件。"
            "仅在 operator_confirmed=true 且用户已明确确认后执行。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "prepare_spectrum_collection 返回的 plan_id。"},
                "operator_confirmed": {"type": "boolean", "description": "操作员是否已确认物理环境就绪，必须为 true。"},
            },
            "required": ["plan_id", "operator_confirmed"],
        },
        handler=_execute_spectrum_collection,
    )
