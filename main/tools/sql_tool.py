from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from deepem.protocol import ToolResult
from deepem.tools.base import ToolContext, ToolDefinition, ToolExecutionResult

_DEFAULT_MAX_ROWS = 200
_DEFAULT_SCHEMA_SAMPLE_ROWS = 3
_DEFAULT_SQL_RETRY_TIMES = 1
_PREFERRED_DATABASE_KEYWORDS = ("6号楼模拟会议室", "六号楼模拟会议室", "6号楼", "六号楼")

_SCHEMA_CACHE_LOCK = RLock()
_SCHEMA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

_FORBIDDEN_SQL_PATTERN = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|create|replace|truncate|vacuum|pragma|reindex|analyze|grant|revoke)\b",
    re.IGNORECASE,
)

# =========================
# SQL 包裹标签
# =========================
_SQL_WRAPPER_START = "<FINAL_SQL>"
_SQL_WRAPPER_END = "</FINAL_SQL>"


@dataclass(slots=True)
class ColumnProfile:
    name: str
    declared_type: str
    nullable: bool
    is_pk: bool
    samples: list[str]


@dataclass(slots=True)
class TableProfile:
    name: str
    columns: list[ColumnProfile]
    row_count: int
    foreign_keys: list[dict[str, str]]


@dataclass(slots=True)
class SQLAttemptRecord:
    stage: str
    sql: str
    validated_sql: str | None
    executed_sql: str | None
    error: str | None
    success: bool


class SQLToolError(RuntimeError):
    pass


class SchemaIntrospector:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def list_table_names(self) -> list[str]:
        payload = self.inspect()
        return [str(item["name"]) for item in payload.get("tables", [])]

    def inspect(self, include_tables: list[str] | None = None) -> dict[str, Any]:
        full_payload = self._inspect_full()
        if not include_tables:
            return full_payload

        ordered_names: list[str] = []
        seen: set[str] = set()
        for name in include_tables:
            text = str(name or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            ordered_names.append(text)

        filtered_tables = [table for table in full_payload.get("tables", []) if table.get("name") in seen]
        if not filtered_tables:
            return full_payload

        filtered_tables.sort(
            key=lambda item: ordered_names.index(str(item.get("name"))) if str(item.get("name")) in ordered_names else 10**9
        )
        return {
            "db_path": str(self.db_path),
            "tables": filtered_tables,
            "summary_text": render_schema_summary_from_payload(filtered_tables),
        }

    def _inspect_full(self) -> dict[str, Any]:
        cache_key = str(self.db_path)
        db_mtime = self.db_path.stat().st_mtime
        with _SCHEMA_CACHE_LOCK:
            cached = _SCHEMA_CACHE.get(cache_key)
            if cached and cached[0] == db_mtime:
                return cached[1]

        payload = self._load_schema()
        with _SCHEMA_CACHE_LOCK:
            _SCHEMA_CACHE[cache_key] = (db_mtime, payload)
        return payload

    def _load_schema(self) -> dict[str, Any]:
        tables: list[TableProfile] = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            table_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()

            for item in table_rows:
                table_name = str(item[0])
                columns: list[ColumnProfile] = []

                table_info = conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
                for col in table_info:
                    column_name = str(col[1])
                    samples = [
                        stringify_cell(row[0])
                        for row in conn.execute(
                            f"SELECT {quote_identifier(column_name)} FROM {quote_identifier(table_name)} "
                            f"WHERE {quote_identifier(column_name)} IS NOT NULL LIMIT ?",
                            (_DEFAULT_SCHEMA_SAMPLE_ROWS,),
                        ).fetchall()
                    ]
                    columns.append(
                        ColumnProfile(
                            name=column_name,
                            declared_type=str(col[2] or "TEXT"),
                            nullable=not bool(col[3]),
                            is_pk=bool(col[5]),
                            samples=samples,
                        )
                    )

                foreign_keys = [
                    {
                        "from": str(fk[3]),
                        "to_table": str(fk[2]),
                        "to": str(fk[4]),
                    }
                    for fk in conn.execute(f"PRAGMA foreign_key_list({quote_identifier(table_name)})").fetchall()
                ]

                row_count = int(conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(table_name)}").fetchone()[0])

                tables.append(
                    TableProfile(
                        name=table_name,
                        columns=columns,
                        row_count=row_count,
                        foreign_keys=foreign_keys,
                    )
                )

        return {
            "db_path": str(self.db_path),
            "tables": [serialize_table_profile(table) for table in tables],
            "summary_text": render_schema_summary(tables),
        }


class TableSelector:
    """
    返回 {table_name: [column1, column2, ...]} 的表字段映射。
    """

    def __init__(self, *, llm_client: Any) -> None:
        self.llm_client = llm_client

    def select(
        self,
        *,
        question: str,
        schema_payload: dict[str, Any],
        stream_handler: Any | None = None,
        cancel_checker: Any | None = None,
    ) -> dict[str, list[str] | None]:
        tables = schema_payload.get("tables", [])
        if not tables:
            return {}

        if self.llm_client is None or self.llm_client.__class__.__name__ == "LocalWorkflowLLMClient":
            return {}

        mschema_for_linking = build_mschema_from_schema_payload(
            schema_payload=schema_payload,
            table_field_mapping=None,
            include_examples=False,
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个专门从事 Text2SQL 中 schema linking 的助手。\n"
                    "请根据用户问题，从给定数据库模式中选出最相关的表和字段。\n\n"
                    "严格要求：\n"
                    "1. 输出必须是 Python dict / JSON object 格式，例如："
                    '{"orders": ["order_id", "customer_id"], "customers": ["customer_name"]}。\n'
                    "2. key 必须是 schema 中真实存在的表名；value 必须是该表中真实存在的字段名列表。\n"
                    "3. 高召回优先，但不要输出明显无关字段。\n"
                    "4. 如某表整体都相关，可返回该表下若干关键字段，不要输出解释。\n"
                    "5. 不要输出 Markdown，不要输出思维链，不要输出额外文字。\n"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户问题：{question}\n\n"
                    "数据库模式（M-Schema）：\n"
                    f"{mschema_for_linking}\n\n"
                    "请直接返回表字段映射。"
                ),
            },
        ]

        emit_nl2sql_reasoning_section(stream_handler=stream_handler, title="表选择")
        response = self.llm_client.complete(messages=messages, tools=[], temperature=0.0, stream_handler=stream_handler, cancel_checker=cancel_checker)
        result = parse_table_field_mapping(response.content or "", schema_payload)
        print("schema link结果：", result)
        return result


class SQLGenerator:
    def __init__(self, *, llm_client: Any) -> None:
        self.llm_client = llm_client

    def generate(self, *, question: str, mschema: str, stream_handler: Any | None = None, cancel_checker: Any | None = None) -> str:
        if self.llm_client is None or self.llm_client.__class__.__name__ == "LocalWorkflowLLMClient":
            raise SQLToolError("nl2sql 需要可用的在线 LLM 配置，当前未启用。")

        messages = [
            {
                "role": "system",
                "content": (
                    # "你是一名SQL专家，擅长将自然语言问题转换为正确的SQL查询。分析问题意图、数据库结构及提供的额外知识，分步骤构造精确的SQL语句，并将最终答案以标签完整包裹格式输出。\n"
                    # "请按照以下步骤生成SQL语句：\n"
                    # "1. **语言检查**：确保用户问题与数据库信息一致，必要时调整语言或结构。\n"
                    # "2. **解析问题**：提取关键需求、筛选条件、输出字段和计算逻辑，结合额外知识进行分析。\n"
                    # "3. **映射到数据库结构**：确定涉及的表、字段及其关系（主键/外键、连接条件等）。\n"
                    # "4. **构建SQL**：生成包含必要子句（SELECT、FROM、JOIN、WHERE、GROUP BY、HAVING、ORDER BY、LIMIT等）的SQL语句，不要包含多余的字段。\n"
                    # "5. **验证与检查**：确认字段名称、数据类型、筛选条件和逻辑正确，避免逻辑和语义偏差，实现原始SQL的再生成。\n"
                    # "6. **输出SQL**：以如下标签完整包裹的形式呈现SQL。\n"
                    # "**注意**：SQL语句必须与用户问题需求完全一致，避免包含不必要的字段或条件。"
                    # "输出格式必须严格如下：\n"
                    # "第一部分：输出你的推理过程，标题必须是【思维链】。\n"
                    # "第二部分：输出最终 SQL，且必须被如下标签完整包裹：\n"
                    # f"{_SQL_WRAPPER_START}\n"
                    # "SELECT ...\n"
                    # f"{_SQL_WRAPPER_END}\n\n"
                    "你是一个严格的 SQLite 只读 SQL 生成器。\n"
                    "你的唯一输出必须是**一条完整、可直接执行的 SELECT SQL 语句**，没有任何解释或其他文字。\n\n"
                    "必须严格遵守以下规则：\n"
                    "1. **只能使用用户本次消息中提供的 schema** 中精确存在的表名和字段名（区分大小写、下划线）。禁止虚构任何不存在的表、列、视图。\n"
                    "2. 生成的 SQL 必须是纯只读，禁止出现 INSERT、UPDATE、DELETE等任何修改操作。\n"
                    "3. 在生成 SQL 之前，需要：\n"
                    "   - 完整理解用户查询的真实意图（包括隐含条件、业务逻辑、代指的字段）。\n"
                    "   - 精确映射用户提到的概念到 schema 中的表、字段和关系（如果用户用自然语言描述，需推断对应列）。\n"
                    "   - 规划完整的 SQL 结构（SELECT 哪些字段、FROM 哪些表、WHERE 条件、是否需要 JOIN、GROUP BY、HAVING、ORDER BY、LIMIT、日期处理等）。\n"
                    "   - 检查是否需要 TopN、趋势分析、聚合、日期范围等，并自行补全最合理、高效的子句。\n"
                    "   - 确保 SQL 语法标准、可直接在 SQLite 中运行，优先选择性能最佳的写法（如必要时使用子查询；不得滥用%LIKE% ）。\n"
                    "4. 如果用户查询与 schema 无法完全匹配，仍需输出最接近、可执行的合理 SQL，而非拒绝或解释。\n\n"
                    "输出格式要求：只返回 SQL 语句本身，首尾不得有任何多余字符。必须被如下标签完整包裹："    
                    f"{_SQL_WRAPPER_START}\n"
                    "SELECT ...\n"
                    f"{_SQL_WRAPPER_END}\n\n"     
                ),
            },
            {
                "role": "user",
                "content": f"用户问题：{question}\n\n数据库 M-Schema：\n{mschema}",
            },
        ]
        print("mschema:", mschema)
        emit_nl2sql_reasoning_section(stream_handler=stream_handler, title="SQL 生成")
        response = self.llm_client.complete(messages=messages, tools=[], temperature=0.0, stream_handler=stream_handler, cancel_checker=cancel_checker)
        raw_output = response.content or ""
        print(raw_output)

        sql = extract_wrapped_sql(raw_output)
        if not sql:
            raise SQLToolError("模型没有生成被特殊标签包裹的有效 SQL。")
        return sql


class SQLFixer:
    """
    执行报错后的二次纠错 SQL 生成器
    """

    def __init__(self, *, llm_client: Any) -> None:
        self.llm_client = llm_client

    def fix(
        self,
        *,
        question: str,
        mschema: str,
        failed_sql: str,
        error_message: str,
        history: list[SQLAttemptRecord],
        stream_handler: Any | None = None,
        cancel_checker: Any | None = None,
    ) -> str:
        if self.llm_client is None or self.llm_client.__class__.__name__ == "LocalWorkflowLLMClient":
            raise SQLToolError("sql 修复需要可用的在线 LLM 配置，当前未启用。")

        history_text = render_attempt_history(history)

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个严格的 SQLite SQL 修复器。\n"
                    "你的任务不是重新自由发挥，而是基于：用户问题、数据库 M-Schema、失败 SQL、执行错误信息，"
                    "修复出一条可以执行的、语义尽量保持一致的只读 SQL。\n\n"
                    "必须严格遵守：\n"
                    "1. 只能输出 SQLite 只读 SQL（仅 SELECT / WITH）。\n"
                    "2. 禁止使用 M-Schema 中不存在的表名、字段名。\n"
                    "3. 必须认真利用报错信息修复问题，例如：\n"
                    "   - no such column\n"
                    "   - no such table\n"
                    "   - ambiguous column name\n"
                    "   - misuse of aggregate\n"
                    "   - syntax error\n"
                    "   - wrong join path\n"
                    "4. 若原 SQL 意图基本正确，优先做最小改动。\n"
                    "5. 若原 SQL 与 schema 不匹配，允许重写，但必须尽量贴合用户原始问题。\n"
                    "6. 最终只能输出一条 SQL，且必须被特殊标签完整包裹。\n\n"
                    "输出格式必须严格如下：\n"
                    "第一部分：输出你的推理过程，标题必须是【纠错思维链】。\n"
                    "第二部分：输出修复后的最终 SQL，且必须被如下标签完整包裹：\n"
                    f"{_SQL_WRAPPER_START}\n"
                    "SELECT ...\n"
                    f"{_SQL_WRAPPER_END}\n\n"
                    "额外要求：\n"
                    "A. 标签中的内容必须是一条完整、可直接执行的 SQL。\n"
                    "B. 不要输出 Markdown 代码块。\n"
                    "C. 不要在标签外再输出第二条 SQL。\n"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户问题：{question}\n\n"
                    f"数据库 M-Schema：\n{mschema}\n\n"
                    f"失败 SQL：\n{failed_sql}\n\n"
                    f"执行错误：\n{error_message}\n\n"
                    f"此前尝试轨迹：\n{history_text}\n"
                ),
            },
        ]

        emit_nl2sql_reasoning_section(stream_handler=stream_handler, title="SQL 修复")
        response = self.llm_client.complete(messages=messages, tools=[], temperature=0.0, stream_handler=stream_handler, cancel_checker=cancel_checker)
        raw_output = response.content or ""
        print("纠错结果：")
        print(raw_output)

        sql = extract_wrapped_sql(raw_output)
        if not sql:
            raise SQLToolError("纠错模型没有生成被特殊标签包裹的有效 SQL。")
        return sql


class SQLValidator:
    def validate(self, sql: str) -> str:
        normalized = sql.strip()
        if not normalized:
            raise SQLToolError("生成的 SQL 为空。")

        normalized = normalized.strip().rstrip(";").strip()
        if not normalized:
            raise SQLToolError("生成的 SQL 为空。")

        if ";" in normalized:
            raise SQLToolError("仅允许执行单条 SQL 查询。")

        lowered = remove_sql_strings(normalized).lower()
        if _FORBIDDEN_SQL_PATTERN.search(lowered):
            raise SQLToolError("仅允许只读 SQL 查询。")

        if not (lowered.startswith("select") or lowered.startswith("with")):
            raise SQLToolError("仅允许 SELECT 或 WITH 查询。")

        return normalized

    def apply_limit(self, sql: str, max_rows: int = _DEFAULT_MAX_ROWS) -> str:
        lowered = remove_sql_strings(sql).lower()
        if re.search(r"\blimit\s+\d+\b", lowered):
            return sql
        return f"SELECT * FROM ({sql}) AS _nl2sql_result LIMIT {max_rows}"


class SQLiteExecutor:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def execute(self, sql: str) -> tuple[list[str], list[list[Any]], bool]:
        deadline = time.monotonic() + 3.0
        with sqlite3.connect(self.db_path, timeout=2.0) as conn:
            conn.row_factory = sqlite3.Row

            def progress_handler() -> int:
                return 1 if time.monotonic() > deadline else 0

            conn.set_progress_handler(progress_handler, 10000)
            try:
                cursor = conn.execute(sql)
                rows = cursor.fetchall()
                columns = [item[0] for item in (cursor.description or [])]
            except sqlite3.OperationalError as exc:
                raise SQLToolError(f"SQL 执行失败：{exc}") from exc
            except sqlite3.DatabaseError as exc:
                raise SQLToolError(f"SQL 执行失败：{exc}") from exc
            finally:
                conn.set_progress_handler(None, 0)

        safe_rows = [[coerce_cell(cell) for cell in row] for row in rows]
        truncated = len(safe_rows) >= _DEFAULT_MAX_ROWS
        return columns, safe_rows, truncated


class ChartPlanner:
    def suggest(self, *, question: str, columns: list[str], rows: list[list[Any]]) -> dict[str, Any]:
        if not columns:
            return {"type": "table", "title": "查询结果"}
        if len(columns) < 2 or len(rows) < 2:
            return {"type": "table", "title": "查询结果"}

        first = [row[0] for row in rows]
        second = [row[1] for row in rows]

        second_is_numeric = all(is_number(item) for item in second if item is not None)
        first_looks_temporal = all(is_temporal_like(item) for item in first if item is not None)
        first_looks_categorical = any(isinstance(item, str) for item in first if item is not None)
        normalized_question = question.lower()

        if first_looks_temporal and second_is_numeric:
            return {"type": "line", "title": "趋势图", "x": columns[0], "y": columns[1]}

        if second_is_numeric and len(rows) <= 8 and any(token in normalized_question for token in ("占比", "构成", "比例", "pie")):
            return {"type": "pie", "title": "占比分布", "label": columns[0], "value": columns[1]}

        if second_is_numeric and first_looks_categorical:
            return {"type": "bar", "title": "统计图", "x": columns[0], "y": columns[1]}

        return {"type": "table", "title": "查询结果"}


def build_query_local_database_tool() -> ToolDefinition:
    return ToolDefinition(
        name="query_local_database",
        description=(
            "根据用户的自然语言问题查询本地 SQLite 数据库。"
            "返回最终 SQL、表格结果、图表建议和结果解读。"
            "当用户问题有查表意图时，调用 query_local_database。"
            "当用户问题有信号的合法分析意图时，或需要判断该信号是否出现过时,需要调用query_local_database,调用前若用户问题无明显查表细节，则改写并形成查询语句;例如将'查询物联设备信号特征分布情况'作为参数调用 query_local_database。若有查表细节，则不改写，按照用户原始查表意图即可。"
            "当问题涉及查询、统计、筛选、分组、趋势、排行，或提到表、字段等数据对象时，也调用 query_local_database。"
            "只有普通闲聊时，不要调用 query_local_database。"
        ),
        input_schema={
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
            "additionalProperties": False,
        },
        handler=_query_local_database,
    )


def _query_local_database(args: dict[str, object], context: ToolContext) -> ToolExecutionResult:
    question = str(args.get("question", "")).strip()
    if not question:
        raise SQLToolError("question 不能为空。")

    options = context.nl2sql_options
    db_path = resolve_nl2sql_db_path(options=options, database_catalog=getattr(context, "database_catalog", None))
    if not db_path.exists():
        raise SQLToolError(f"未找到 SQLite 数据库文件：{db_path}")
    database_id, database_name, database_file_name = resolve_database_labels(
        options=options,
        context=context,
        db_path=db_path,
    )

    introspector = SchemaIntrospector(db_path)
    full_schema_payload = introspector.inspect()
    all_table_names = [str(item["name"]) for item in full_schema_payload.get("tables", [])]

    table_field_mapping, selection_mode = resolve_schema_scope(
        question=question,
        full_schema_payload=full_schema_payload,
        llm_client=context.llm_client,
        options=options,
        stream_handler=context.stream_handler,
        cancel_checker=context.cancel_checker,
    )

    selected_table_names = list(table_field_mapping.keys()) if table_field_mapping else all_table_names
    schema_payload = introspector.inspect(include_tables=selected_table_names)
    mschema = build_mschema_from_schema_payload(
        schema_payload=schema_payload,
        table_field_mapping=table_field_mapping,
        include_examples=True,
    )

    main_llm_client = context.llm_client
    fix_llm_client = getattr(context, "sql_fix_llm_client", None) or main_llm_client
    retry_times = int(getattr(options, "sql_retry_times", _DEFAULT_SQL_RETRY_TIMES) or 0)

    validator = SQLValidator()
    executor = SQLiteExecutor(db_path)

    attempts: list[SQLAttemptRecord] = []

    def run_attempt(*, stage: str, raw_sql: str) -> tuple[str, str, list[str], list[list[Any]], bool]:
        validated_sql = validator.validate(raw_sql)
        executed_sql = validator.apply_limit(validated_sql)
        columns, rows, truncated = executor.execute(executed_sql)
        attempts.append(
            SQLAttemptRecord(
                stage=stage,
                sql=raw_sql,
                validated_sql=validated_sql,
                executed_sql=executed_sql,
                error=None,
                success=True,
            )
        )
        return validated_sql, executed_sql, columns, rows, truncated

    def record_failed_attempt(
        *,
        stage: str,
        raw_sql: str,
        validated_sql: str | None,
        executed_sql: str | None,
        error: str,
    ) -> None:
        attempts.append(
            SQLAttemptRecord(
                stage=stage,
                sql=raw_sql,
                validated_sql=validated_sql,
                executed_sql=executed_sql,
                error=error,
                success=False,
            )
        )

    def safe_record_failed_attempt(
        *,
        stage: str,
        raw_sql: str,
        validated_sql: str | None,
        executed_sql: str | None,
        error: str,
    ) -> None:
        try:
            record_failed_attempt(
                stage=stage,
                raw_sql=raw_sql,
                validated_sql=validated_sql,
                executed_sql=executed_sql,
                error=error,
            )
        except Exception as record_exc:
            print(f"[WARN] record_failed_attempt failed: {record_exc}")

    raw_sql = SQLGenerator(llm_client=main_llm_client).generate(
        question=question,
        mschema=mschema,
        stream_handler=context.stream_handler,
        cancel_checker=context.cancel_checker,
    )

    final_validated_sql = ""
    final_executed_sql = ""
    final_columns: list[str] = []
    final_rows: list[list[Any]] = []
    final_truncated = False
    final_stage = "initial_generation"

    try:
        final_validated_sql, final_executed_sql, final_columns, final_rows, final_truncated = run_attempt(
            stage="initial_generation",
            raw_sql=raw_sql,
        )
    except SQLToolError as exc:
        error_message = str(exc)
        validated_sql_for_record: str | None = None
        executed_sql_for_record: str | None = None

        try:
            validated_sql_for_record = validator.validate(raw_sql)
            executed_sql_for_record = validator.apply_limit(validated_sql_for_record)
        except Exception:
            pass

        safe_record_failed_attempt(
            stage="initial_generation",
            raw_sql=raw_sql,
            validated_sql=validated_sql_for_record,
            executed_sql=executed_sql_for_record,
            error=error_message,
        )

        current_error = error_message
        repaired = False

        for retry_index in range(retry_times):
            last_failed_sql = attempts[-1].sql if attempts else raw_sql

            fixed_sql = SQLFixer(llm_client=fix_llm_client).fix(
                question=question,
                mschema=mschema,
                failed_sql=last_failed_sql,
                error_message=current_error,
                history=attempts,
                stream_handler=context.stream_handler,
                cancel_checker=context.cancel_checker,
            )

            stage = f"fix_retry_{retry_index + 1}"
            try:
                final_validated_sql, final_executed_sql, final_columns, final_rows, final_truncated = run_attempt(
                    stage=stage,
                    raw_sql=fixed_sql,
                )
                final_stage = stage
                repaired = True
                break
            except SQLToolError as retry_exc:
                retry_error_message = str(retry_exc)
                validated_sql_for_record = None
                executed_sql_for_record = None

                try:
                    validated_sql_for_record = validator.validate(fixed_sql)
                    executed_sql_for_record = validator.apply_limit(validated_sql_for_record)
                except Exception:
                    pass

                safe_record_failed_attempt(
                    stage=stage,
                    raw_sql=fixed_sql,
                    validated_sql=validated_sql_for_record,
                    executed_sql=executed_sql_for_record,
                    error=retry_error_message,
                )
                current_error = retry_error_message

        if not repaired:
            raise SQLToolError(build_retry_failure_message(attempts))

    chart = ChartPlanner().suggest(question=question, columns=final_columns, rows=final_rows)

    result = ToolResult(
        status="success",
        data={
            "question": question,
            "database_id": database_id,
            "database_name": database_name,
            "database_file_name": database_file_name,
            "db_path": str(db_path),
            "sql": final_validated_sql,
            "executed_sql": final_executed_sql,
            "columns": final_columns,
            "rows": final_rows,
            "row_count": len(final_rows),
            "truncated": final_truncated,
            "chart": chart,
            "schema_overview": {
                "table_count": len(schema_payload.get("tables", [])),
                "tables": [table["name"] for table in schema_payload.get("tables", [])],
                "table_field_mapping": table_field_mapping,
                "mschema": mschema,
            },
            "nl2sql_config": {
                "force_enabled": options.force_enabled,
                "auto_select_tables": options.auto_select_tables,
                "manual_selected_tables": list(options.manual_selected_tables),
                "database_id": database_id,
                "database_name": database_name,
                "selected_tables": [table["name"] for table in schema_payload.get("tables", [])],
                "selection_mode": selection_mode,
                "sql_retry_times": retry_times,
            },
            "attempt_count": len(attempts),
            "attempts": [
                {
                    "stage": item.stage,
                    "sql": item.sql,
                    "validated_sql": item.validated_sql,
                    "executed_sql": item.executed_sql,
                    "error": item.error,
                    "success": item.success,
                }
                for item in attempts
            ],
            "final_generation_stage": final_stage,
            "summary": summarize_result(question=question, columns=final_columns, rows=final_rows, chart=chart),
        },
    )
    return ToolExecutionResult(result=result)


def _build_placeholder_result(*, question: str, db_path: Path, all_table_names: list[str], note: str, options) -> ToolExecutionResult:
    sql = f"SELECT {json.dumps(note, ensure_ascii=False)} AS note"
    database_id = str(getattr(options, "database_id", "") or "default").strip() or "default"
    database_name = "默认数据库" if database_id == "default" else db_path.stem
    result = ToolResult(
        status="success",
        data={
            "question": question,
            "database_id": database_id,
            "database_name": database_name,
            "database_file_name": db_path.name,
            "db_path": str(db_path),
            "sql": sql,
            "executed_sql": sql,
            "columns": ["note"],
            "rows": [[note]],
            "row_count": 1,
            "truncated": False,
            "chart": {"type": "table", "title": "查询结果"},
            "schema_overview": {"table_count": len(all_table_names), "tables": all_table_names},
            "nl2sql_config": {
                "force_enabled": options.force_enabled,
                "auto_select_tables": options.auto_select_tables,
                "manual_selected_tables": list(options.manual_selected_tables),
                "database_id": database_id,
                "database_name": database_name,
                "selected_tables": all_table_names,
                "selection_mode": "forced_placeholder",
            },
            "summary": note,
        },
    )
    return ToolExecutionResult(result=result)


def resolve_database_labels(*, options: Any, context: ToolContext, db_path: Path) -> tuple[str, str, str]:
    database_id = str(getattr(options, "database_id", "") or "").strip()
    database_record = None
    if database_id and database_id != "default" and getattr(context, "database_catalog", None) is not None:
        try:
            database_record = context.database_catalog.get(database_id)
        except Exception:
            database_record = None
    if database_record is None and getattr(context, "database_catalog", None) is not None:
        database_record = find_database_record_by_path(context.database_catalog, db_path)
        if database_record is not None:
            database_id = database_record.database_id

    if database_record is not None:
        display_name = str(getattr(database_record, "display_name", "") or "").strip()
        file_name = Path(str(getattr(database_record, "db_path", "") or db_path)).name
        return database_id, display_name or file_name or database_id, file_name or db_path.name

    if not database_id or database_id == "default":
        return "default", "默认数据库", db_path.name

    return database_id, db_path.stem or db_path.name, db_path.name


def resolve_schema_scope(
    *,
    question: str,
    full_schema_payload: dict[str, Any],
    llm_client: Any,
    options,
    stream_handler: Any | None = None,
    cancel_checker: Any | None = None,
) -> tuple[dict[str, list[str] | None], str]:
    all_table_names = [str(item["name"]) for item in full_schema_payload.get("tables", [])]
    all_table_set = set(all_table_names)

    if options.auto_select_tables:
        selected = TableSelector(llm_client=llm_client).select(
            question=question,
            schema_payload=full_schema_payload,
            stream_handler=stream_handler,
            cancel_checker=cancel_checker,
        )
        if selected:
            return selected, "auto"
        return {name: None for name in all_table_names}, "auto_fallback_all"

    manual = [name for name in options.manual_selected_tables if name in all_table_set]
    if manual and len(manual) < len(all_table_names):
        return {name: None for name in manual}, "manual"
    return {name: None for name in all_table_names}, "manual_all"


def resolve_nl2sql_db_path(
    *,
    options: Any | None = None,
    db_path: str | None = None,
    database_id: str | None = None,
    database_catalog: Any | None = None,
) -> Path:
    requested = db_path
    if requested is None and options is not None:
        requested = getattr(options, "db_path", None)

    requested_database_id = database_id
    if requested_database_id is None and options is not None:
        requested_database_id = getattr(options, "database_id", None)

    requested_database_id = str(requested_database_id or "").strip()
    if requested_database_id and requested_database_id != "default":
        if database_catalog is None:
            raise SQLToolError("database_id 已提供，但数据库目录服务不可用。")
        return database_catalog.resolve_path(requested_database_id)

    requested_text = str(requested or "").strip()
    if requested_text:
        return Path(requested_text).expanduser().resolve()

    configured = os.getenv("NL2SQL_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()

    if not requested_database_id and database_catalog is not None:
        preferred = find_preferred_nl2sql_database(database_catalog)
        if preferred is not None:
            return Path(preferred.db_path).expanduser().resolve()

    # Source-tree layout keeps the demo DB at <project>/data/nl2sql_demo.db.
    # Wheel/package layout includes a copy at <site-packages>/main/data/nl2sql_demo.db.
    current_file = Path(__file__).resolve()
    candidates = [
        current_file.parents[2] / "data" / "nl2sql_demo.db",
        current_file.parents[1] / "data" / "nl2sql_demo.db",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def find_preferred_nl2sql_database(database_catalog: Any | None) -> Any | None:
    if database_catalog is None:
        return None
    try:
        records = list(database_catalog.list_databases())
    except Exception:
        return None
    for record in records:
        searchable = " ".join(
            str(value or "")
            for value in (
                getattr(record, "display_name", ""),
                getattr(record, "file_name", ""),
                Path(str(getattr(record, "db_path", ""))).stem,
            )
        )
        if any(keyword in searchable for keyword in _PREFERRED_DATABASE_KEYWORDS):
            return record
    return None


def find_database_record_by_path(database_catalog: Any | None, db_path: Path) -> Any | None:
    if database_catalog is None:
        return None
    try:
        target = Path(db_path).expanduser().resolve()
        records = list(database_catalog.list_databases())
    except Exception:
        return None
    for record in records:
        try:
            if Path(str(getattr(record, "db_path", ""))).expanduser().resolve() == target:
                return record
        except Exception:
            continue
    return None


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def emit_nl2sql_reasoning_section(*, stream_handler: Any | None, title: str) -> None:
    # NL2SQL 内部阶段标题不再通过 reasoning_delta 注入前端。
    # 否则在模型没有真实思考内容时，也会因为这些占位标题而固定展示“思考过程”区域。
    return


def serialize_table_profile(table: TableProfile) -> dict[str, Any]:
    return {
        "name": table.name,
        "row_count": table.row_count,
        "columns": [
            {
                "name": column.name,
                "type": column.declared_type,
                "nullable": column.nullable,
                "pk": column.is_pk,
                "samples": column.samples,
            }
            for column in table.columns
        ],
        "foreign_keys": table.foreign_keys,
    }


def render_schema_summary(tables: list[TableProfile]) -> str:
    return render_schema_summary_from_payload([serialize_table_profile(table) for table in tables])


def render_schema_summary_from_payload(tables: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for table in tables:
        lines.append(f"表 {table['name']}（约 {table['row_count']} 行）")
        for column in table.get("columns", []):
            samples = f" 示例值={column.get('samples')}" if column.get("samples") else ""
            pk = " PK" if column.get("pk") else ""
            nullable = " 可空" if column.get("nullable") else " 非空"
            lines.append(f"  - {column.get('name')}: {column.get('type')}{pk}{nullable}{samples}")
        for fk in table.get("foreign_keys", []):
            lines.append(f"  - 外键: {fk['from']} -> {fk['to_table']}.{fk['to']}")
    return "\n".join(lines)


def remove_sql_strings(sql: str) -> str:
    sql = re.sub(r"'([^']|'')*'", "''", sql)
    sql = re.sub(r'"([^"]|"")*"', '""', sql)
    return sql


def extract_sql(text: str) -> str:
    candidate = text.strip()
    if candidate.startswith("```"):
        match = re.search(r"```(?:sql)?\s*(.*?)```", candidate, re.IGNORECASE | re.DOTALL)
        if match:
            candidate = match.group(1).strip()
    return candidate.strip()


def extract_wrapped_sql(text: str) -> str:
    candidate = str(text or "").strip()
    if not candidate:
        return ""

    pattern = re.compile(
        rf"{re.escape(_SQL_WRAPPER_START)}\s*(.*?)\s*{re.escape(_SQL_WRAPPER_END)}",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(candidate)
    if match:
        return match.group(1).strip()

    fenced = re.search(r"```(?:sql)?\s*(.*?)```", candidate, re.IGNORECASE | re.DOTALL)
    if fenced:
        return fenced.group(1).strip()

    stripped = candidate.strip()
    lowered = remove_sql_strings(stripped).lower()
    if lowered.startswith("select") or lowered.startswith("with"):
        return stripped

    return ""


def parse_table_field_mapping(text: str, schema_payload: dict[str, Any]) -> dict[str, list[str] | None]:
    candidate = extract_python_object_text(text)
    if not candidate:
        return {}

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(candidate)
        except Exception:
            parsed = None

    if not isinstance(parsed, dict):
        return {}

    valid_table_columns: dict[str, set[str]] = {
        str(table.get("name")): {str(col.get("name")) for col in table.get("columns", [])}
        for table in schema_payload.get("tables", [])
    }

    normalized: dict[str, list[str] | None] = {}
    for raw_table, raw_columns in parsed.items():
        table_name = str(raw_table or "").strip()
        if table_name not in valid_table_columns:
            continue

        if raw_columns is None:
            normalized[table_name] = None
            continue

        if isinstance(raw_columns, str):
            candidate_columns = [raw_columns]
        elif isinstance(raw_columns, (list, tuple, set)):
            candidate_columns = [str(item).strip() for item in raw_columns if str(item).strip()]
        else:
            continue

        valid_columns = []
        seen_columns: set[str] = set()
        for col in candidate_columns:
            if col in valid_table_columns[table_name] and col not in seen_columns:
                valid_columns.append(col)
                seen_columns.add(col)

        if valid_columns:
            normalized[table_name] = valid_columns

    return normalized


def extract_python_object_text(text: str) -> str:
    candidate = str(text or "").strip()
    if not candidate:
        return ""

    fenced = re.search(r"```(?:python|json)?\s*(.*?)```", candidate, re.IGNORECASE | re.DOTALL)
    if fenced:
        return fenced.group(1).strip()

    # 优先抓最外层 dict
    brace_match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if brace_match:
        return brace_match.group(0).strip()

    return candidate


def build_mschema_from_schema_payload(
    *,
    schema_payload: dict[str, Any],
    table_field_mapping: dict[str, list[str] | None] | None,
    include_examples: bool = True,
    language: str = "chinese",
) -> str:
    """
   生成 M-Schema。
    """
    labels = {
        "DB_ID": "DB_ID" if language.lower() == "english" else "数据库ID",
        "Schema": "Schema" if language.lower() == "english" else "表结构",
        "Table": "Table" if language.lower() == "english" else "表",
        "Foreign keys": "Foreign keys" if language.lower() == "english" else "外键",
        "Primary Key": "Primary Key" if language.lower() == "english" else "主键",
        "Examples": "Examples" if language.lower() == "english" else "示例",
    }

    db_path = str(schema_payload.get("db_path", ""))
    db_name = Path(db_path).name if db_path else "unknown"

    table_lookup = {str(table.get("name")): table for table in schema_payload.get("tables", [])}
    if table_field_mapping:
        ordered_table_names = [name for name in table_field_mapping.keys() if name in table_lookup]
    else:
        ordered_table_names = list(table_lookup.keys())

    lines: list[str] = []
    lines.append(f"【{labels['DB_ID']}】 {db_name}")
    lines.append(f"【{labels['Schema']}】")

    foreign_key_lines: list[str] = []

    for table_name in ordered_table_names:
        table = table_lookup[table_name]
        selected_columns = table.get("columns", [])

        desired_columns = None
        if table_field_mapping and table_name in table_field_mapping:
            desired_columns = table_field_mapping[table_name]

        if desired_columns:
            desired_set = set(desired_columns)
            selected_columns = [col for col in selected_columns if str(col.get("name")) in desired_set]

        lines.append(f"# {labels['Table']}: {table_name}")
        lines.append("[")

        col_lines: list[str] = []
        for col in selected_columns:
            col_name = str(col.get("name"))
            col_type = str(col.get("type") or "TEXT").split("(")[0].upper()
            field_desc = f"({col_name}:{col_type}"

            if bool(col.get("pk")):
                field_desc += f", {labels['Primary Key']}"
            if not bool(col.get("nullable", True)):
                field_desc += ", NOT NULL"

            if include_examples:
                samples = [str(item) for item in col.get("samples", [])]
                field_desc += f", {labels['Examples']}: [{', '.join(samples)}]"

            field_desc += ")"
            col_lines.append(field_desc)

        lines.append(",\n".join(col_lines))
        lines.append("]")

        for fk in table.get("foreign_keys", []):
            local_col = str(fk.get("from"))
            ref_table = str(fk.get("to_table"))
            ref_col = str(fk.get("to"))
            foreign_key_lines.append(f"{table_name}.{local_col}={ref_table}.{ref_col}")

    if foreign_key_lines:
        deduped_fk = list(dict.fromkeys(foreign_key_lines))
        lines.append(f"【{labels['Foreign keys']}】")
        lines.extend(deduped_fk)

    return "\n".join(lines)


def coerce_cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def stringify_cell(value: Any) -> str:
    text = str(coerce_cell(value))
    return text if len(text) <= 32 else text[:29] + "..."


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_temporal_like(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return bool(re.match(r"^\d{4}-\d{2}(-\d{2})?", value))


def summarize_result(*, question: str, columns: list[str], rows: list[list[Any]], chart: dict[str, Any]) -> str:
    if not rows:
        return "未查到符合条件的数据。"

    prefix = f"已根据问题“{question}”查询到 {len(rows)} 行结果。"
    if chart.get("type") and chart.get("type") != "table":
        return prefix + f" 建议使用 {chart['type']} 图展示。"

    if columns and rows:
        preview = "，".join(f"{name}={stringify_cell(value)}" for name, value in zip(columns[:3], rows[0][:3]))
        return prefix + f" 首行示例：{preview}。"

    return prefix


def render_attempt_history(history: list[SQLAttemptRecord]) -> str:
    if not history:
        return "暂无历史尝试。"

    parts: list[str] = []
    for idx, item in enumerate(history, start=1):
        parts.append(f"第 {idx} 次尝试")
        parts.append(f"阶段：{item.stage}")
        parts.append(f"是否成功：{item.success}")
        parts.append(f"原始 SQL：\n{item.sql}")
        if item.validated_sql:
            parts.append(f"校验后 SQL：\n{item.validated_sql}")
        if item.executed_sql:
            parts.append(f"实际执行 SQL：\n{item.executed_sql}")
        if item.error:
            parts.append(f"错误：{item.error}")
        parts.append("-" * 40)
    return "\n".join(parts)


def build_retry_failure_message(attempts: list[SQLAttemptRecord]) -> str:
    if not attempts:
        return "SQL 生成与执行失败，且没有可用的尝试记录。"

    lines = ["SQL 多轮生成/纠错后仍执行失败。错误轨迹如下："]
    for idx, item in enumerate(attempts, start=1):
        lines.append(f"{idx}. stage={item.stage}, success={item.success}, error={item.error or '无'}")
    return "\n".join(lines)
