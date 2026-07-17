# Tokenizer 国内镜像设计

## 目标

运行上下文管理评测时，Qwen tokenizer 默认通过 `https://hf-mirror.com` 获取，避免访问 Hugging Face 官方端点超时，同时保留完全离线运行能力。

## 行为

- `run_runtime_eval.py` 加载项目 `.env` 后，使用 `setdefault` 将 `HF_ENDPOINT` 设为 `https://hf-mirror.com`。
- 用户在进程环境或 `.env` 中设置的 `HF_ENDPOINT` 优先，不被默认值覆盖。
- 默认允许 tokenizer 网络访问和自动下载。
- 新增 `--tokenizer-offline`。启用后设置 `HF_HUB_OFFLINE=1` 和 `TRANSFORMERS_OFFLINE=1`，只使用本地缓存。
- 移除 `--allow-tokenizer-network`，因为联网成为默认行为。

## 可观测性

`manifest.json` 记录：

- `tokenizer_endpoint`：本次运行实际使用的 endpoint。
- `tokenizer_offline`：是否启用离线模式。

不记录任何凭据。

## 测试

- 未配置 endpoint 时使用国内镜像。
- 已配置 endpoint 时保留用户值。
- 离线参数设置两个 Transformers/Hugging Face 离线变量。
- manifest 正确记录 endpoint 和离线状态。

## 文档

更新评测 README，说明默认镜像、环境变量覆盖方式和 `--tokenizer-offline` 用法。
