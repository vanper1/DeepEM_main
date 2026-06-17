from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

import pandas as pd

from deepem.protocol import utc_now
from deepem.tools.sql_tool import SchemaIntrospector


class StructuredDatabaseError(RuntimeError):
    pass


@dataclass(slots=True)
class StructuredDatabaseRecord:
    database_id: str
    file_name: str
    display_name: str
    db_path: str
    source_kind: str
    source_suffix: str
    created_at: str
    table_names: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class StructuredDatabaseCatalog:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def list_databases(self) -> list[StructuredDatabaseRecord]:
        items: list[StructuredDatabaseRecord] = []
        for meta_path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            items.append(self._deserialize(data))
        items.sort(key=lambda item: (item.created_at, item.database_id), reverse=True)
        return items

    def get(self, database_id: str) -> StructuredDatabaseRecord:
        meta_path = self._meta_path(database_id)
        if not meta_path.exists():
            raise KeyError(database_id)
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        record = self._deserialize(data)
        if not Path(record.db_path).exists():
            raise FileNotFoundError(record.db_path)
        return record

    def resolve_path(self, database_id: str) -> Path:
        return Path(self.get(database_id).db_path).expanduser().resolve()

    def rename(self, database_id: str, display_name: str) -> StructuredDatabaseRecord:
        name = normalize_database_display_name(display_name)
        if not name:
            raise StructuredDatabaseError("数据库名称不能为空")
        with self._lock:
            record = self.get(database_id)
            record.display_name = name
            self._meta_path(database_id).write_text(json.dumps(self._serialize(record), ensure_ascii=False, indent=2), encoding="utf-8")
            return record

    def import_file(self, *, database_id: str, file_name: str, content: bytes) -> StructuredDatabaseRecord:
        suffix = Path(file_name).suffix.lower()
        with self._lock:
            if suffix in {".db", ".sqlite", ".sqlite3"}:
                record = self._import_sqlite(database_id=database_id, file_name=file_name, content=content)
            elif suffix in {".xlsx", ".xls"}:
                record = self._import_excel(database_id=database_id, file_name=file_name, content=content)
            elif suffix in {".csv", ".tsv"}:
                record = self._import_delimited(database_id=database_id, file_name=file_name, content=content, delimiter="\t" if suffix == ".tsv" else ",")
            else:
                raise StructuredDatabaseError("仅支持 .db / .sqlite / .sqlite3 / .xls / .xlsx / .csv / .tsv 文件")
            self._meta_path(database_id).write_text(json.dumps(self._serialize(record), ensure_ascii=False, indent=2), encoding="utf-8")
            return record

    def _import_sqlite(self, *, database_id: str, file_name: str, content: bytes) -> StructuredDatabaseRecord:
        display_name = semantic_database_display_name(file_name)
        db_path = self._allocate_sqlite_path(display_name)
        db_path.write_bytes(content)
        try:
            table_names = SchemaIntrospector(db_path).list_table_names()
        except Exception as exc:
            db_path.unlink(missing_ok=True)
            raise StructuredDatabaseError(f"无法读取 SQLite 数据库：{exc}") from exc
        return StructuredDatabaseRecord(
            database_id=database_id,
            file_name=Path(file_name).name,
            display_name=display_name,
            db_path=str(db_path.resolve()),
            source_kind="sqlite_upload",
            source_suffix=Path(file_name).suffix.lower(),
            created_at=utc_now().isoformat(),
            table_names=table_names,
        )

    def _import_excel(self, *, database_id: str, file_name: str, content: bytes) -> StructuredDatabaseRecord:
        display_name = semantic_database_display_name(file_name)
        db_path = self._allocate_sqlite_path(display_name)
        if db_path.exists():
            db_path.unlink()
        excel_path = self.root / f"{database_id}{Path(file_name).suffix.lower() or '.xlsx'}"
        excel_path.write_bytes(content)
        try:
            workbook = pd.ExcelFile(excel_path)
            with sqlite3.connect(db_path) as conn:
                for sheet_name in workbook.sheet_names:
                    frame = workbook.parse(sheet_name=sheet_name)
                    table_name = sanitize_table_name(sheet_name, fallback="sheet")
                    frame.columns = [sanitize_column_name(str(item)) for item in frame.columns]
                    frame.to_sql(table_name, conn, if_exists="replace", index=False)
        except Exception as exc:
            db_path.unlink(missing_ok=True)
            raise StructuredDatabaseError(f"Excel 导入失败：{exc}") from exc
        finally:
            excel_path.unlink(missing_ok=True)
        table_names = SchemaIntrospector(db_path).list_table_names()
        return StructuredDatabaseRecord(
            database_id=database_id,
            file_name=Path(file_name).name,
            display_name=display_name,
            db_path=str(db_path.resolve()),
            source_kind="excel_import",
            source_suffix=Path(file_name).suffix.lower(),
            created_at=utc_now().isoformat(),
            table_names=table_names,
        )

    def _import_delimited(self, *, database_id: str, file_name: str, content: bytes, delimiter: str) -> StructuredDatabaseRecord:
        display_name = semantic_database_display_name(file_name)
        db_path = self._allocate_sqlite_path(display_name)
        if db_path.exists():
            db_path.unlink()
        tmp_path = self.root / f"{database_id}{Path(file_name).suffix.lower()}"
        tmp_path.write_bytes(content)
        try:
            frame = pd.read_csv(tmp_path, sep=delimiter)
            table_name = sanitize_table_name(Path(file_name).stem, fallback="sheet")
            frame.columns = [sanitize_column_name(str(item)) for item in frame.columns]
            with sqlite3.connect(db_path) as conn:
                frame.to_sql(table_name, conn, if_exists="replace", index=False)
        except Exception as exc:
            db_path.unlink(missing_ok=True)
            raise StructuredDatabaseError(f"表格导入失败：{exc}") from exc
        finally:
            tmp_path.unlink(missing_ok=True)
        table_names = SchemaIntrospector(db_path).list_table_names()
        return StructuredDatabaseRecord(
            database_id=database_id,
            file_name=Path(file_name).name,
            display_name=display_name,
            db_path=str(db_path.resolve()),
            source_kind="table_import",
            source_suffix=Path(file_name).suffix.lower(),
            created_at=utc_now().isoformat(),
            table_names=table_names,
        )

    def _meta_path(self, database_id: str) -> Path:
        return self.root / f"{database_id}.json"

    def _allocate_sqlite_path(self, display_name: str) -> Path:
        base_name = sqlite_file_stem(display_name)
        candidate = self.root / f"{base_name}.sqlite3"
        index = 2
        while candidate.exists():
            candidate = self.root / f"{base_name}_{index}.sqlite3"
            index += 1
        return candidate

    @staticmethod
    def _serialize(record: StructuredDatabaseRecord) -> dict[str, Any]:
        return {
            "database_id": record.database_id,
            "file_name": record.file_name,
            "display_name": record.display_name,
            "db_path": record.db_path,
            "source_kind": record.source_kind,
            "source_suffix": record.source_suffix,
            "created_at": record.created_at,
            "table_names": list(record.table_names),
            "metadata": dict(record.metadata),
        }

    @staticmethod
    def _deserialize(data: dict[str, Any]) -> StructuredDatabaseRecord:
        return StructuredDatabaseRecord(
            database_id=str(data["database_id"]),
            file_name=str(data.get("file_name", "")),
            display_name=str(data.get("display_name") or data.get("file_name") or data["database_id"]),
            db_path=str(data["db_path"]),
            source_kind=str(data.get("source_kind", "sqlite_upload")),
            source_suffix=str(data.get("source_suffix", ".sqlite3")),
            created_at=str(data.get("created_at", "")),
            table_names=[str(item) for item in data.get("table_names", [])],
            metadata=dict(data.get("metadata") or {}),
        )


def sanitize_table_name(value: str, *, fallback: str = "sheet") -> str:
    text = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(value).strip())
    text = text.strip("_") or fallback
    if text[0].isdigit():
        text = f"t_{text}"
    return text[:63]


def sanitize_column_name(value: str, *, fallback: str = "column") -> str:
    text = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(value).strip())
    text = text.strip("_") or fallback
    if text[0].isdigit():
        text = f"c_{text}"
    return text[:63]


def semantic_database_display_name(file_name: str) -> str:
    stem = Path(file_name or "database").stem
    return normalize_database_display_name(stem) or "database"


def normalize_database_display_name(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" ._-/\\")
    return text[:80]


def sqlite_file_stem(value: str) -> str:
    normalized = normalize_database_display_name(value)
    text = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in normalized)
    text = re.sub(r"_+", "_", text).strip("._-")
    if not text:
        text = "database"
    if text[0].isdigit():
        text = f"db_{text}"
    return text[:80]
