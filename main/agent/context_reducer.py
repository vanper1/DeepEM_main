from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextReductionPolicy:
    tool_compaction_ratio: float = 0.65
    summary_ratio: float = 0.80
    aggressive_ratio: float = 0.90
    normal_tail_message_target: int = 8
    aggressive_tail_message_target: int = 4

    def stage_for_ratio(self, ratio: float) -> str:
        if ratio < self.tool_compaction_ratio:
            return "full"
        if ratio < self.summary_ratio:
            return "tool_compaction"
        if ratio < self.aggressive_ratio:
            return "normal_summary"
        return "aggressive"
