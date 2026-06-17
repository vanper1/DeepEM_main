from __future__ import annotations

from typing import Any

from deepem.protocol import ToolResult
from deepem.tools.base import ToolContext, ToolDefinition, ToolExecutionResult


def build_query_uploaded_documents_tool() -> ToolDefinition:
    return ToolDefinition(
        name="query_uploaded_documents",
        description=(
            "查询已上传到共享文档库中的文件内容或文件摘要。"
            "如果需要阅读 PDF、Markdown、TXT、DOCX、二进制信号摘要或历史上传资料，调用此工具。"
            "当前轮非图片附件以及跨轮文件访问都应优先使用本工具。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "file_id": {"type": "string"},
                "top_k": {"type": "integer"},
                "max_chars": {"type": "integer"},
            },
            "additionalProperties": False,
        },
        handler=_query_uploaded_documents,
    )


def _query_uploaded_documents(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    document_index = getattr(context, "document_index", None)
    if document_index is None:
        raise RuntimeError("文档索引服务未初始化。")

    query = str(args.get("query", "") or "").strip()
    file_id = str(args.get("file_id", "") or "").strip() or None
    top_k = max(1, min(int(args.get("top_k", 5) or 5), 10))
    max_chars = max(200, min(int(args.get("max_chars", 1200) or 1200), 5000))

    if not query and not file_id:
        raise RuntimeError("query 与 file_id 至少需要提供一个。")

    file_record = document_index.get_file_record(file_id) if file_id else None
    hits = document_index.search(query=query or None, file_id=file_id, top_k=top_k + 1, max_chars=max_chars)
    items: list[dict[str, Any]] = []
    for hit in hits:
        if file_record and hit.get("asset_id") == file_record.get("asset_id") and hit.get("record_type") == "file":
            continue
        items.append(
            {
                "asset_id": hit.get("asset_id"),
                "file_name": hit.get("file_name"),
                "record_type": hit.get("record_type"),
                "chunk_text": hit.get("text"),
                "summary": hit.get("summary"),
                "page": hit.get("page"),
                "sheet": hit.get("sheet"),
                "chunk_index": hit.get("chunk_index"),
                "score": hit.get("score"),
                "preview_url": hit.get("preview_url"),
                "preview_markdown": _preview_markdown(hit),
            }
        )
        if len(items) >= top_k:
            break

    summary_parts = []
    if file_record:
        summary_parts.append(f"文件：{file_record.get('file_name')}")
        if file_record.get("summary"):
            summary_parts.append(str(file_record.get("summary")))
    if query:
        summary_parts.append(f"已检索到 {len(items)} 条相关片段。")
    elif items:
        summary_parts.append(f"已返回文件摘要和 {len(items)} 条内容片段。")
    summary = " ".join(part for part in summary_parts if part).strip() or "已返回文件检索结果。"

    return ToolExecutionResult(
        result=ToolResult(
            status="success",
            data={
                "query": query,
                "file_id": file_id,
                "file": {
                    "asset_id": file_record.get("asset_id"),
                    "file_name": file_record.get("file_name"),
                    "summary": file_record.get("summary"),
                    "preview_url": file_record.get("preview_url"),
                    "preview_markdown": _preview_markdown(file_record),
                    "upload_kind": file_record.get("upload_kind"),
                }
                if file_record
                else None,
                "results": items,
                "result_count": len(items),
                "summary": summary,
            },
        )
    )


def _preview_markdown(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    preview_url = payload.get("preview_url")
    if not preview_url:
        return None
    file_name = str(payload.get("file_name") or "preview")
    return f"![{file_name}]({preview_url})"
