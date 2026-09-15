from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .probe_evidence_compaction import build_probe_evidence_pack


PROBE_EVIDENCE_ROOT = Path(__file__).resolve().parent / "data" / "probe_evidence"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_component(value: str, fallback: str = "task") -> str:
    text = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", str(value or "")).strip("._")
    return text or fallback


def _display_name(row: dict[str, Any]) -> str:
    for key in ("name", "ssid", "connected_ssid", "device_name", "alias", "mac", "bssid"):
        value = str(row.get(key) or "").strip()
        if value and value != "--":
            return value
    return "未命名设备"


@dataclass(slots=True)
class ProbeEvidenceStore:
    """SQLite-backed, retrieval-friendly store for large probe evidence sets.

    Raw JSON is retained as an immutable artifact while every device/target/observation
    row is indexed into SQLite. The LLM receives only the bounded evidence pack and can
    retrieve matching rows on demand via ``query_probe_evidence``.
    """

    root: Path = PROBE_EVIDENCE_ROOT
    db_path: Path = field(init=False)

    def __post_init__(self) -> None:
        configured = os.getenv("DEEPEM_PROBE_EVIDENCE_ROOT")
        if configured:
            self.root = Path(configured).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "probe_evidence.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=20.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS evidence_sets (
                    id TEXT PRIMARY KEY,
                    capture_task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    source TEXT,
                    raw_json_path TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL,
                    counts_json TEXT NOT NULL,
                    overview_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_probe_sets_task ON evidence_sets(capture_task_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS evidence_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_set_id TEXT NOT NULL,
                    section TEXT NOT NULL,
                    kind TEXT,
                    probe_id TEXT,
                    mac TEXT,
                    name TEXT,
                    ssid TEXT,
                    bssid TEXT,
                    vendor TEXT,
                    channel TEXT,
                    rssi REAL,
                    distance REAL,
                    last_seen TEXT,
                    search_text TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(evidence_set_id) REFERENCES evidence_sets(id)
                );
                CREATE INDEX IF NOT EXISTS idx_probe_records_set ON evidence_records(evidence_set_id, section, kind);
                CREATE INDEX IF NOT EXISTS idx_probe_records_mac ON evidence_records(evidence_set_id, mac);
                CREATE INDEX IF NOT EXISTS idx_probe_records_name ON evidence_records(evidence_set_id, name);
                CREATE INDEX IF NOT EXISTS idx_probe_records_last_seen ON evidence_records(evidence_set_id, last_seen DESC);
                """
            )

    def ingest(self, result: dict[str, Any], *, capture_task_id: str) -> dict[str, Any]:
        capture_task_id = _safe_component(capture_task_id or "capture-task")
        evidence_set_id = f"probeev_{uuid4().hex[:16]}"
        target_dir = self.root / capture_task_id
        target_dir.mkdir(parents=True, exist_ok=True)
        raw_path = target_dir / f"{evidence_set_id}.json"
        raw_bytes = json.dumps(result, ensure_ascii=False, indent=2, default=str).encode("utf-8")
        raw_path.write_bytes(raw_bytes)
        raw_sha256 = _sha256_bytes(raw_bytes)
        evidence_pack = build_probe_evidence_pack(result)
        created_at = utc_now_iso()

        rows: list[tuple[Any, ...]] = []
        for section, items in (
            ("devices", list(result.get("devices") or [])),
            ("targets", list(result.get("targets") or [])),
            ("observations", list(result.get("observations") or [])),
        ):
            for item in items:
                row = dict(item or {})
                search_values = [
                    section,
                    row.get("kind"), row.get("probe_id"), row.get("mac"), row.get("name"), row.get("ssid"),
                    row.get("bssid"), row.get("connected_ssid"), row.get("connected_bssid"), row.get("vendor"),
                    row.get("device_type"), row.get("channel"), row.get("encryption"), _display_name(row),
                ]
                search_text = " ".join(str(value) for value in search_values if value not in (None, "")).lower()
                try:
                    rssi = float(row.get("rssi_latest")) if row.get("rssi_latest") not in (None, "") else None
                except (TypeError, ValueError):
                    rssi = None
                try:
                    distance = float(row.get("distance")) if row.get("distance") not in (None, "") else None
                except (TypeError, ValueError):
                    distance = None
                rows.append(
                    (
                        evidence_set_id,
                        section,
                        str(row.get("kind") or ""),
                        str(row.get("probe_id") or ""),
                        str(row.get("mac") or ""),
                        _display_name(row),
                        str(row.get("ssid") or ""),
                        str(row.get("bssid") or ""),
                        str(row.get("vendor") or ""),
                        str(row.get("channel") or ""),
                        rssi,
                        distance,
                        str(row.get("last_seen") or ""),
                        search_text,
                        _json(row),
                    )
                )

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO evidence_sets(id, capture_task_id, created_at, source, raw_json_path, raw_sha256, counts_json, overview_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_set_id,
                    capture_task_id,
                    created_at,
                    str(result.get("source") or ""),
                    str(raw_path.resolve()),
                    raw_sha256,
                    _json(result.get("counts") or {}),
                    _json(evidence_pack.get("overview") or {}),
                ),
            )
            if rows:
                connection.executemany(
                    """
                    INSERT INTO evidence_records(
                        evidence_set_id, section, kind, probe_id, mac, name, ssid, bssid, vendor,
                        channel, rssi, distance, last_seen, search_text, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

        return {
            "evidence_set_id": evidence_set_id,
            "capture_task_id": capture_task_id,
            "created_at": created_at,
            "raw_json_path": str(raw_path.resolve()),
            "raw_sha256": raw_sha256,
            "counts": dict(result.get("counts") or {}),
            "overview": evidence_pack.get("overview") or {},
            "llm_evidence": evidence_pack.get("llm_evidence") or {},
            "manifest": evidence_pack.get("manifest") or {},
            "indexed_records": len(rows),
        }

    def latest_for_task(self, capture_task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM evidence_sets WHERE capture_task_id=? ORDER BY created_at DESC LIMIT 1",
                (_safe_component(capture_task_id),),
            ).fetchone()
        return self._set_row(row) if row else None

    def get_set(self, evidence_set_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM evidence_sets WHERE id=?", (str(evidence_set_id),)).fetchone()
        return self._set_row(row) if row else None

    @staticmethod
    def _set_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "evidence_set_id": row["id"],
            "capture_task_id": row["capture_task_id"],
            "created_at": row["created_at"],
            "source": row["source"],
            "raw_json_path": row["raw_json_path"],
            "raw_sha256": row["raw_sha256"],
            "counts": json.loads(row["counts_json"] or "{}"),
            "overview": json.loads(row["overview_json"] or "{}"),
        }

    def load_raw_result(self, evidence_set_id: str) -> dict[str, Any]:
        item = self.get_set(evidence_set_id)
        if item is None:
            raise KeyError(evidence_set_id)
        path = Path(str(item["raw_json_path"])).resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict(payload or {})

    def query(
        self,
        *,
        evidence_set_id: str,
        query: str = "",
        kinds: list[str] | None = None,
        sections: list[str] | None = None,
        limit: int = 40,
    ) -> dict[str, Any]:
        evidence_set_id = str(evidence_set_id or "").strip()
        if not evidence_set_id:
            raise ValueError("evidence_set_id 不能为空")
        limit = max(1, min(100, int(limit or 40)))
        kinds = [str(item) for item in (kinds or []) if str(item)]
        sections = [str(item) for item in (sections or []) if str(item)]
        tokens = [token.lower() for token in re.split(r"\s+", str(query or "").strip()) if token]

        clauses = ["evidence_set_id=?"]
        params: list[Any] = [evidence_set_id]
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" for _ in kinds))
            params.extend(kinds)
        if sections:
            clauses.append("section IN (%s)" % ",".join("?" for _ in sections))
            params.extend(sections)
        for token in tokens[:8]:
            clauses.append("search_text LIKE ?")
            params.append(f"%{token}%")
        where_sql = " AND ".join(clauses)

        with self._connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM evidence_records WHERE {where_sql}", params).fetchone()[0])
            rows = connection.execute(
                f"""
                SELECT section, kind, probe_id, mac, name, ssid, bssid, vendor, channel,
                       rssi, distance, last_seen, payload_json
                FROM evidence_records
                WHERE {where_sql}
                ORDER BY CASE WHEN rssi IS NULL THEN 1 ELSE 0 END, rssi DESC, last_seen DESC, id DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        items = []
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            items.append({"section": row["section"], **payload})
        return {
            "evidence_set_id": evidence_set_id,
            "query": query,
            "kinds": kinds,
            "sections": sections,
            "total_matches": total,
            "returned": len(items),
            "items": items,
            "retrieval_note": "结果来自已落库的探针原始证据索引；可继续缩小 kind/section 或关键词按需检索。",
        }


_DEFAULT_STORE: ProbeEvidenceStore | None = None


def get_probe_evidence_store() -> ProbeEvidenceStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = ProbeEvidenceStore()
    return _DEFAULT_STORE
