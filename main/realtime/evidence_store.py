from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from elasticsearch import Elasticsearch
except Exception:  # pragma: no cover - optional dependency during partial installs
    Elasticsearch = None


@dataclass(slots=True)
class EvidenceStoreResult:
    status: str
    target: str
    error: str | None = None


class SignalEvidenceStore:
    def __init__(
        self,
        *,
        root: Path,
        es_url: str | None = None,
        index_name: str | None = None,
        username: str | None = None,
        password: str | None = None,
        ca_certs: str | None = None,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.root / "signal_evidence.jsonl"
        self.es_url = (es_url or os.getenv("DEEPEM_SIGNAL_ES_URL") or os.getenv("DEEPEM_ES_URL") or "https://localhost:9200").strip()
        self.index_name = (index_name or os.getenv("DEEPEM_SIGNAL_ES_INDEX") or "deepem_signal_evidence").strip()
        self.username = (username or os.getenv("DEEPEM_SIGNAL_ES_USERNAME") or os.getenv("DEEPEM_ES_USERNAME") or "elastic").strip()
        self.password = (password or os.getenv("DEEPEM_SIGNAL_ES_PASSWORD") or os.getenv("DEEPEM_ES_PASSWORD") or "work4deepem").strip()
        default_ca = "/home/deepem/http_ca.crt" if Path("/home/deepem/http_ca.crt").exists() else ""
        self.ca_certs = (ca_certs or os.getenv("DEEPEM_SIGNAL_ES_CA_CERTS") or os.getenv("DEEPEM_ES_CA_CERTS") or default_ca).strip()
        self._client: Any | None = None
        self._index_ready = False

    def index_signal_evidence(self, document_id: str, document: dict[str, Any]) -> EvidenceStoreResult:
        payload = dict(document)
        payload.setdefault("document_id", document_id)
        if self.es_url and Elasticsearch is not None:
            try:
                client = self._es_client()
                self._ensure_es_index(client)
                payload["evidence_store"] = {"status": "indexed", "target": f"es:{self.index_name}"}
                client.index(index=self.index_name, id=document_id, document=payload, refresh="wait_for")
                return EvidenceStoreResult(status="indexed", target=f"es:{self.index_name}")
            except Exception as exc:
                self._append_jsonl(
                    payload
                    | {
                        "evidence_store": {
                            "status": "fallback_jsonl",
                            "target": str(self.jsonl_path),
                            "error": str(exc),
                        }
                    }
                )
                return EvidenceStoreResult(status="fallback_jsonl", target=str(self.jsonl_path), error=str(exc))
        self._append_jsonl(payload | {"evidence_store": {"status": "jsonl", "target": str(self.jsonl_path)}})
        return EvidenceStoreResult(status="jsonl", target=str(self.jsonl_path))

    def _append_jsonl(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _es_client(self) -> Any:
        if self._client is not None:
            return self._client
        kwargs: dict[str, Any] = {}
        if self.username:
            kwargs["basic_auth"] = (self.username, self.password)
        if self.ca_certs:
            kwargs["ca_certs"] = self.ca_certs
        self._client = Elasticsearch(self.es_url, **kwargs)
        return self._client

    def _ensure_es_index(self, client: Any) -> None:
        if self._index_ready:
            return
        if not client.indices.exists(index=self.index_name):
            client.indices.create(
                index=self.index_name,
                mappings={
                    "properties": {
                        "document_id": {"type": "keyword"},
                        "event_id": {"type": "keyword"},
                        "signal_id": {"type": "keyword"},
                        "batch_id": {"type": "keyword"},
                        "session_id": {"type": "keyword"},
                        "usrp_task_id": {"type": "keyword"},
                        "collector_id": {"type": "keyword"},
                        "device_id": {"type": "keyword"},
                        "file_name": {"type": "keyword"},
                        "file_path": {"type": "keyword"},
                        "classification": {"type": "keyword"},
                        "fingerprint": {"type": "keyword"},
                        "channel": {"type": "integer"},
                        "center_freq_hz": {"type": "double"},
                        "sample_rate_hz": {"type": "double"},
                        "bandwidth_hz": {"type": "double"},
                        "gain": {"type": "double"},
                        "raw_sample_count": {"type": "long"},
                        "score": {"type": "double"},
                        "created_at": {"type": "date"},
                    }
                },
            )
        self._index_ready = True
