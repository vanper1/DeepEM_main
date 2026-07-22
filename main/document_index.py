from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from elasticsearch import Elasticsearch
except Exception:  # pragma: no cover - optional dependency during partial installs
    Elasticsearch = None


class DocumentIndexUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class DocumentHit:
    score: float
    payload: dict[str, Any]


class InMemoryDocumentIndex:
    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []

    def index_documents(self, documents: list[dict[str, Any]]) -> None:
        self._docs.extend(dict(item) for item in documents)

    def get_file_record(self, asset_id: str) -> dict[str, Any] | None:
        for item in self._docs:
            if item.get("asset_id") == asset_id and item.get("record_type") == "file":
                return dict(item)
        return None

    def search(
        self,
        *,
        query: str | None = None,
        file_id: str | None = None,
        top_k: int = 5,
        max_chars: int = 1600,
    ) -> list[dict[str, Any]]:
        query_text = (query or "").strip().lower()
        items = []
        for doc in self._docs:
            if file_id and doc.get("asset_id") != file_id:
                continue
            if not query_text:
                items.append((float(1.0), doc))
                continue
            haystack = " ".join(
                [
                    str(doc.get("file_name", "")),
                    str(doc.get("summary", "")),
                    str(doc.get("text", "")),
                ]
            ).lower()
            count = haystack.count(query_text)
            if count:
                items.append((float(count), doc))
        items.sort(key=lambda item: (-item[0], str(item[1].get("record_type")), int(item[1].get("chunk_index") or 0)))
        results: list[dict[str, Any]] = []
        for score, payload in items[:top_k]:
            doc = dict(payload)
            if doc.get("text"):
                doc["text"] = str(doc["text"])[:max_chars]
            doc["score"] = score
            results.append(doc)
        return results


class ElasticsearchDocumentIndex:
    def __init__(
        self,
        *,
        url: str,
        index_name: str,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.url = url.strip()
        self.index_name = index_name.strip() or "deepem_knowledge"
        self.username = username
        self.password = password
        if Elasticsearch is None:
            self.client = None
        else:
            auth = None
            if username:
                auth = (username, password or "")
            self.client = Elasticsearch(self.url, basic_auth=auth,verify_certs=False,ssl_show_warn=False)
        self._index_ready = False

    def index_documents(self, documents: list[dict[str, Any]]) -> None:
        self._ensure_index()
        assert self.client is not None
        operations: list[dict[str, Any]] = []
        for item in documents:
            document = dict(item)
            doc_id = str(document.pop("document_id"))
            operations.append({"index": {"_index": self.index_name, "_id": doc_id}})
            operations.append(document)
        if operations:
            self.client.bulk(operations=operations, refresh="wait_for")

    def get_file_record(self, asset_id: str) -> dict[str, Any] | None:
        self._ensure_index()
        assert self.client is not None
        response = self.client.search(
            index=self.index_name,
            size=1,
            query={
                "bool": {
                    "filter": [
                        {"term": {"asset_id": asset_id}},
                        {"term": {"record_type": "file"}},
                    ]
                }
            },
        )
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            return None
        payload = dict(hits[0].get("_source") or {})
        payload["score"] = float(hits[0].get("_score") or 0.0)
        return payload

    def search(
        self,
        *,
        query: str | None = None,
        file_id: str | None = None,
        top_k: int = 5,
        max_chars: int = 1600,
    ) -> list[dict[str, Any]]:
        self._ensure_index()
        assert self.client is not None

        filters = []
        if file_id:
            filters.append({"term": {"asset_id": file_id}})

        query_text = (query or "").strip()
        if query_text:
            es_query: dict[str, Any] = {
                "bool": {
                    "filter": filters,
                    "must": [
                        {
                            "multi_match": {
                                "query": query_text,
                                "fields": ["text^3", "summary^2", "file_name^2"],
                                "type": "best_fields",
                            }
                        }
                    ],
                }
            }
            sort = None
        else:
            es_query = {
                "bool": {
                    "filter": filters,
                }
            }
            sort = [
                {"record_type": {"order": "asc"}},
                {"chunk_index": {"order": "asc"}},
                {"created_at": {"order": "desc"}},
            ]

        response = self.client.search(index=self.index_name, size=max(1, top_k), query=es_query, sort=sort)
        hits = response.get("hits", {}).get("hits", [])
        results: list[dict[str, Any]] = []
        for hit in hits:
            payload = dict(hit.get("_source") or {})
            if payload.get("text"):
                payload["text"] = str(payload["text"])[:max_chars]
            payload["score"] = float(hit.get("_score") or 0.0)
            results.append(payload)
        return results

    def _ensure_index(self) -> None:
        if self._index_ready:
            return
        if self.client is None:
            raise DocumentIndexUnavailable("Elasticsearch Python 客户端不可用。")
        try:
            if not self.client.ping():
                raise DocumentIndexUnavailable(f"无法连接 Elasticsearch：{self.url}")
            exists = self.client.indices.exists(index=self.index_name)
            if not exists:
                self.client.indices.create(
                    index=self.index_name,
                    mappings={
                        "properties": {
                            "asset_id": {"type": "keyword"},
                            "record_type": {"type": "keyword"},
                            "file_name": {
                                "type": "text",
                                "fields": {
                                    "keyword": {"type": "keyword", "ignore_above": 256},
                                },
                            },
                            "suffix": {"type": "keyword"},
                            "conversation_id": {"type": "keyword"},
                            "created_at": {"type": "date"},
                            "chunk_index": {"type": "integer"},
                            "page": {"type": "keyword"},
                            "sheet": {"type": "keyword"},
                            "summary": {"type": "text"},
                            "text": {"type": "text"},
                            "preview_asset_id": {"type": "keyword"},
                            "preview_url": {"type": "keyword"},
                            "mime_type": {"type": "keyword"},
                            "upload_kind": {"type": "keyword"},
                        }
                    },
                )
        except DocumentIndexUnavailable:
            raise
        except Exception as exc:
            raise DocumentIndexUnavailable(f"初始化 Elasticsearch 索引失败：{exc}") from exc
        self._index_ready = True
