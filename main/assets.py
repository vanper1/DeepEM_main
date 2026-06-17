from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from deepem.protocol import utc_now


def guess_mime_type(file_name: str, *, default: str = "application/octet-stream") -> str:
    mime_type, _ = mimetypes.guess_type(file_name)
    return mime_type or default


@dataclass(slots=True)
class AssetRecord:
    asset_id: str
    file_name: str
    stored_name: str
    suffix: str
    mime_type: str
    size_bytes: int
    upload_kind: str
    created_at: str
    conversation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def path_suffix(self) -> str:
        return self.suffix if self.suffix.startswith(".") or not self.suffix else f".{self.suffix}"


class AssetManager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.files_dir = self.root / "files"
        self.meta_dir = self.root / "meta"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def save_bytes(
        self,
        *,
        asset_id: str,
        file_name: str,
        content: bytes,
        upload_kind: str,
        conversation_id: str | None = None,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AssetRecord:
        suffix = Path(file_name).suffix.lower()
        stored_name = f"{asset_id}{suffix}"
        record = AssetRecord(
            asset_id=asset_id,
            file_name=Path(file_name).name,
            stored_name=stored_name,
            suffix=suffix,
            mime_type=mime_type or guess_mime_type(file_name),
            size_bytes=len(content),
            upload_kind=upload_kind,
            created_at=utc_now().isoformat(),
            conversation_id=conversation_id,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._path_for(record).write_bytes(content)
            self._meta_path(asset_id).write_text(json.dumps(self._serialize(record), ensure_ascii=False, indent=2), encoding="utf-8")
        return record

    def get(self, asset_id: str) -> AssetRecord:
        with self._lock:
            meta_path = self._meta_path(asset_id)
            if not meta_path.exists():
                raise KeyError(asset_id)
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        return self._deserialize(data)

    def update_metadata(self, asset_id: str, patch: dict[str, Any]) -> AssetRecord:
        with self._lock:
            record = self.get(asset_id)
            record.metadata.update(dict(patch))
            self._meta_path(asset_id).write_text(json.dumps(self._serialize(record), ensure_ascii=False, indent=2), encoding="utf-8")
            return record

    def resolve_path(self, asset_id: str) -> Path:
        record = self.get(asset_id)
        path = self._path_for(record)
        if not path.exists():
            raise FileNotFoundError(asset_id)
        return path

    def read_bytes(self, asset_id: str) -> bytes:
        return self.resolve_path(asset_id).read_bytes()

    @staticmethod
    def public_url(asset_id: str) -> str:
        return f"/api/assets/{asset_id}"

    def to_client_payload(self, record: AssetRecord) -> dict[str, Any]:
        payload = {
            "asset_id": record.asset_id,
            "file_name": record.file_name,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "upload_kind": record.upload_kind,
            "created_at": record.created_at,
            "url": self.public_url(record.asset_id),
            "metadata": dict(record.metadata),
        }
        preview_asset_id = record.metadata.get("preview_asset_id")
        if preview_asset_id:
            payload["preview_url"] = self.public_url(str(preview_asset_id))
        return payload

    def _path_for(self, record: AssetRecord) -> Path:
        return self.files_dir / record.stored_name

    def _meta_path(self, asset_id: str) -> Path:
        return self.meta_dir / f"{asset_id}.json"

    @staticmethod
    def _serialize(record: AssetRecord) -> dict[str, Any]:
        return {
            "asset_id": record.asset_id,
            "file_name": record.file_name,
            "stored_name": record.stored_name,
            "suffix": record.suffix,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "upload_kind": record.upload_kind,
            "created_at": record.created_at,
            "conversation_id": record.conversation_id,
            "metadata": dict(record.metadata),
        }

    @staticmethod
    def _deserialize(data: dict[str, Any]) -> AssetRecord:
        return AssetRecord(
            asset_id=str(data["asset_id"]),
            file_name=str(data["file_name"]),
            stored_name=str(data["stored_name"]),
            suffix=str(data.get("suffix", "")),
            mime_type=str(data.get("mime_type") or guess_mime_type(str(data.get("file_name", "")))),
            size_bytes=int(data.get("size_bytes", 0)),
            upload_kind=str(data.get("upload_kind", "file")),
            created_at=str(data.get("created_at", "")),
            conversation_id=data.get("conversation_id"),
            metadata=dict(data.get("metadata") or {}),
        )
