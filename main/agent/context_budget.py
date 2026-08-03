from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable


DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"


@lru_cache(maxsize=8)
def _load_default_tokenizer_cached(
    model_name: str,
    hf_endpoint: str,
    hf_offline: str,
    transformers_offline: str,
) -> Any:
    try:
        from transformers import AutoTokenizer
    except Exception as exc:  # pragma: no cover - depends on optional dependency
        raise RuntimeError("transformers is not installed") from exc
    return AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)


@dataclass(slots=True)
class ContextBudgetOptions:
    char_limit: int = 50000
    token_limit: int | None = 30000
    reserved_output_tokens: int = 4096
    safety_margin_tokens: int = 512
    tokenizer_model: str | None = "Qwen/Qwen3-30B-A3B"
    enable_token_estimation: bool = True
    enable_thinking: bool = True
    warning_ratio: float = 0.70
    danger_ratio: float = 0.90


class ContextBudgetEstimator:
    def __init__(
        self,
        options: ContextBudgetOptions | None = None,
        *,
        tokenizer_loader: Callable[[str], Any] | None = None,
    ) -> None:
        self.options = options or ContextBudgetOptions()
        self._tokenizer_loader = tokenizer_loader or self._load_tokenizer
        self._tokenizer: Any | None = None
        self._tokenizer_error: str | None = None

    def estimate(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self.options.char_limit <= 0:
            raise ValueError("char_limit must be greater than 0")
        estimated_chars = len(json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")))
        char_usage_ratio = estimated_chars / self.options.char_limit
        token_metrics, token_error = self._estimate_tokens(messages, tools=tools)
        usage_ratio = token_metrics["token_usage_ratio"] if token_metrics else char_usage_ratio
        if usage_ratio >= self.options.danger_ratio:
            status = "danger"
        elif usage_ratio >= self.options.warning_ratio:
            status = "warning"
        else:
            status = "ok"

        metrics = {
            "estimated_chars": estimated_chars,
            "char_limit": self.options.char_limit,
            "usage_ratio": char_usage_ratio,
            "warning_ratio": self.options.warning_ratio,
            "danger_ratio": self.options.danger_ratio,
            "status": status,
            "estimated_tokens": token_metrics["estimated_tokens"] if token_metrics else None,
            "token_limit": token_metrics["token_limit"] if token_metrics else None,
            "usable_input_tokens": token_metrics["usable_input_tokens"] if token_metrics else None,
            "reserved_output_tokens": token_metrics["reserved_output_tokens"] if token_metrics else None,
            "safety_margin_tokens": token_metrics["safety_margin_tokens"] if token_metrics else None,
            "token_usage_ratio": token_metrics["token_usage_ratio"] if token_metrics else None,
            "method": "qwen3_tokenizer_v1" if token_metrics else "json_chars_v1",
        }
        if token_error:
            metrics["token_estimation_error"] = token_error
        return metrics

    def _estimate_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not self.options.enable_token_estimation:
            return None, None
        if not self.options.tokenizer_model:
            return None, None
        if self.options.token_limit is None:
            return None, None
        if self._tokenizer_error is not None:
            return None, self._tokenizer_error

        usable_input_tokens = (
            self.options.token_limit - self.options.reserved_output_tokens - self.options.safety_margin_tokens
        )
        if usable_input_tokens <= 0:
            return None, "usable_input_tokens must be greater than 0"

        try:
            tokenizer = self._get_tokenizer()
            chat_text = tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=self.options.enable_thinking,
                **({"tools": self._tool_payloads(tools)} if tools else {}),
            )
            estimated_tokens = len(tokenizer.encode(chat_text, add_special_tokens=False))
        except Exception as exc:
            self._tokenizer_error = self._compact_error(exc)
            return None, self._tokenizer_error

        return (
            {
                "estimated_tokens": estimated_tokens,
                "token_limit": self.options.token_limit,
                "usable_input_tokens": usable_input_tokens,
                "reserved_output_tokens": self.options.reserved_output_tokens,
                "safety_margin_tokens": self.options.safety_margin_tokens,
                "token_usage_ratio": estimated_tokens / usable_input_tokens,
            },
            None,
        )

    @staticmethod
    def _tool_payloads(tools: list[dict[str, Any]] | list[Any] | None) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for tool in tools or []:
            if isinstance(tool, dict):
                payloads.append(tool)
                continue
            payloads.append(
                {
                    "type": "function",
                    "function": {
                        "name": getattr(tool, "name", ""),
                        "description": getattr(tool, "description", ""),
                        "parameters": getattr(tool, "input_schema", {}),
                    },
                }
            )
        return payloads

    def _get_tokenizer(self) -> Any:
        if self._tokenizer is None:
            assert self.options.tokenizer_model is not None
            self._tokenizer = self._tokenizer_loader(self.options.tokenizer_model)
        return self._tokenizer

    @staticmethod
    def _load_tokenizer(model_name: str) -> Any:
        os.environ.setdefault("HF_ENDPOINT", DEFAULT_HF_ENDPOINT)
        return _load_default_tokenizer_cached(
            model_name,
            os.environ.get("HF_ENDPOINT", ""),
            os.environ.get("HF_HUB_OFFLINE", ""),
            os.environ.get("TRANSFORMERS_OFFLINE", ""),
        )

    @staticmethod
    def clear_tokenizer_cache() -> None:
        _load_default_tokenizer_cached.cache_clear()

    @staticmethod
    def _compact_error(exc: Exception) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        return text[:240]


class PromptCompositionEstimator:
    COMPONENT_NAMES = (
        "system",
        "context_summary",
        "base_non_system",
        "transcript_tool_messages",
        "transcript_assistant_messages",
        "transcript_other_messages",
    )
    NOTE = (
        "component shares are diagnostic estimates and may not sum to 1.0 because chat template overhead "
        "differs between full prompt and component subsets."
    )

    def __init__(
        self,
        options: ContextBudgetOptions | None = None,
        *,
        tokenizer_loader: Callable[[str], Any] | None = None,
        budget_estimator: ContextBudgetEstimator | None = None,
    ) -> None:
        self.options = options or getattr(budget_estimator, "options", None) or ContextBudgetOptions()
        self._tokenizer_loader = tokenizer_loader
        self._budget_estimator = budget_estimator or ContextBudgetEstimator(
            self.options,
            tokenizer_loader=self._tokenizer_loader,
        )

    def estimate(
        self,
        *,
        base_messages: list[dict[str, Any]],
        transcript_messages: list[dict[str, Any]],
        assembled_messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        total_metrics = self._budget(assembled_messages, tools=tools)
        groups = self._group_messages(base_messages=base_messages, transcript_messages=transcript_messages)
        components = [
            self._component(name, messages, total_metrics=total_metrics, tools=tools)
            for name, messages in groups.items()
        ]
        largest = self._largest_component(components)
        result = {
            "method": total_metrics.get("method"),
            "_note": self.NOTE,
            "total": {
                "estimated_tokens": total_metrics.get("estimated_tokens"),
                "estimated_chars": total_metrics.get("estimated_chars"),
            },
            "components": components,
            "largest_component": largest,
        }
        if total_metrics.get("token_estimation_error"):
            result["token_estimation_error"] = total_metrics.get("token_estimation_error")
        return result

    def _budget(self, messages: list[dict[str, Any]], *, tools: list[Any] | None = None) -> dict[str, Any]:
        return self._budget_estimator.estimate(messages, tools=tools)

    def _component(
        self,
        name: str,
        messages: list[dict[str, Any]],
        *,
        total_metrics: dict[str, Any],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        if not messages:
            return {
                "name": name,
                "estimated_tokens": 0 if total_metrics.get("estimated_tokens") is not None else None,
                "estimated_chars": 0,
                "share": 0,
            }
        metrics = self._budget(messages, tools=tools)
        use_tokens = total_metrics.get("estimated_tokens") is not None and metrics.get("estimated_tokens") is not None
        if use_tokens:
            denominator = int(total_metrics.get("estimated_tokens") or 0)
            numerator = int(metrics.get("estimated_tokens") or 0)
        else:
            denominator = int(total_metrics.get("estimated_chars") or 0)
            numerator = int(metrics.get("estimated_chars") or 0)
        share = round(numerator / denominator, 6) if denominator > 0 and numerator > 0 else 0
        return {
            "name": name,
            "estimated_tokens": metrics.get("estimated_tokens"),
            "estimated_chars": metrics.get("estimated_chars"),
            "share": share,
        }

    def _group_messages(
        self,
        *,
        base_messages: list[dict[str, Any]],
        transcript_messages: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        groups = {name: [] for name in self.COMPONENT_NAMES}
        for message in base_messages:
            if self._is_context_summary(message):
                groups["context_summary"].append(message)
            elif message.get("role") == "system":
                groups["system"].append(message)
            else:
                groups["base_non_system"].append(message)
        for message in transcript_messages:
            role = message.get("role")
            if role == "tool":
                groups["transcript_tool_messages"].append(message)
            elif role == "assistant":
                groups["transcript_assistant_messages"].append(message)
            else:
                groups["transcript_other_messages"].append(message)
        return groups

    @staticmethod
    def _is_context_summary(message: dict[str, Any]) -> bool:
        content = message.get("content", "")
        if not isinstance(content, str):
            return False
        return content.startswith("较早历史上下文摘要") or "conversation_context_summary" in content[:500]

    @staticmethod
    def _largest_component(components: list[dict[str, Any]]) -> str | None:
        nonzero = [item for item in components if int(item.get("estimated_chars") or 0) > 0]
        if not nonzero:
            return None
        return max(nonzero, key=lambda item: (item.get("estimated_tokens") or item.get("estimated_chars") or 0))["name"]
