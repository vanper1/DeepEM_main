from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class NL2SQLSessionConfig:
    force_enabled: bool = False
    auto_select_tables: bool = True
    manual_selected_tables: list[str] = field(default_factory=list)
    database_id: str = ""
    db_path: str = ""

    @classmethod
    def from_payload(cls, payload: Any | None) -> "NL2SQLSessionConfig":
        if payload is None:
            return cls()
        if isinstance(payload, cls):
            return cls(
                force_enabled=bool(payload.force_enabled),
                auto_select_tables=bool(payload.auto_select_tables),
                manual_selected_tables=_normalize_table_names(payload.manual_selected_tables),
                database_id=_normalize_database_id(getattr(payload, "database_id", "")),
                db_path=_normalize_db_path(getattr(payload, "db_path", "")),
            )
        if not isinstance(payload, dict):
            return cls()
        return cls(
            force_enabled=bool(payload.get("force_enabled", False)),
            auto_select_tables=bool(payload.get("auto_select_tables", True)),
            manual_selected_tables=_normalize_table_names(payload.get("manual_selected_tables") or []),
            database_id=_normalize_database_id(payload.get("database_id", "")),
            db_path=_normalize_db_path(payload.get("db_path", "")),
        )

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "force_enabled": self.force_enabled,
            "auto_select_tables": self.auto_select_tables,
            "manual_selected_tables": list(self.manual_selected_tables),
            "database_id": self.database_id,
            "db_path": self.db_path,
        }


def _normalize_table_names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        text = str(item or '').strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result



def _normalize_db_path(value: Any) -> str:
    text = str(value or '').strip()
    return text


def _normalize_database_id(value: Any) -> str:
    return str(value or "").strip()
