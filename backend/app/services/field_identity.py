"""字段身份与别名有效期领域服务：把"按字符串猜字段"升级为可核实的身份检索。

核心约束：
- 字段身份由「表 + 列 + 生效期」三元组确定，同名同列也不能跨表混用；
- 旧别名按表限定和有效期登记，同一别名可在不同有效期指向不同来源；
- 查询历史日期时只返回当时能确认的生产表和列，遇到同名候选不猜来源；
- 别名修订必须晚于原生效日期，只关闭旧记录、不改写旧历史；
- Schema 变更的写入任务评估条目必须带齐表与日期证据，确认后才能进入候选列演练。

上游：lineage_graph 提供字段级传递路径
消费：schema_evolution 用字段影响数据生成写入任务评估条目
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

OPEN_END = date(9999, 12, 31)

_SPLIT_TOKENS = re.compile(r"[,;|\s]+")


class FieldIdentityError(ValueError):
    """字段身份领域校验失败，路由层转成 400。"""


# ---------------------------------------------------------------- 公共工具

def normalize(value: Any) -> str:
    return str(value or "").strip().casefold()


def field_tokens(value: Any) -> set[str]:
    """把「传递字段」拆成精确字段集合。

    血缘里的传递字段是逗号分隔的字符串，旧的子串检索会让 order_code
    误命中 order_code_v2。这里按分隔符切分后做精确比较，并兼容
    source->target 形式的字段映射。
    """
    tokens: set[str] = set()
    for part in _SPLIT_TOKENS.split(str(value or "")):
        part = part.strip().strip("[](){}'\"`")
        if not part:
            continue
        if "->" in part:
            for side in part.split("->", 1):
                side = side.strip().strip("'\"`")
                if side:
                    tokens.add(side.casefold())
        else:
            tokens.add(part.casefold())
    return tokens


def mentions_field(text: Any, field_name: str) -> bool:
    """判断一段文本是否精确提到某个字段，避免前缀误命中。"""
    field_name = str(field_name or "").strip()
    if not field_name:
        return False
    text = str(text or "")
    if re.fullmatch(r"[A-Za-z0-9_]+", field_name):
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(field_name)}(?![A-Za-z0-9_])"
        return re.search(pattern, text, re.IGNORECASE) is not None
    return field_name.casefold() in text.casefold()


def mentions_table(text: Any, table: str) -> bool:
    """判断 SQL/处理逻辑是否引用了某张表，表名按完整标识符匹配。"""
    table = str(table or "").strip()
    if not table:
        return False
    text = str(text or "")
    pattern = rf"(?<![A-Za-z0-9_.]){re.escape(table)}(?![A-Za-z0-9_.])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _is_write(logic: Any) -> bool:
    """判断节点处理逻辑是否为写入动作，避免把读取源表的抽取节点当写入任务。"""
    text = str(logic or "").upper()
    return any(
        keyword in text for keyword in (
            "INSERT", "OVERWRITE", "MERGE", "UPSERT", "CREATE TABLE",
            "REPLACE INTO", "LOAD DATA", "写入",
        )
    )


def _parse_date(value: Any, field_name: str = "生效日期") -> date:
    text = str(value or "").strip()
    if not text:
        raise FieldIdentityError(f"缺少{field_name}")
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise FieldIdentityError(f"{field_name}格式应为 YYYY-MM-DD：{value}") from exc


def _row_start(row: dict[str, Any]) -> date:
    return _parse_date(row.get("effective_from"), "登记生效日期")


def _row_end(row: dict[str, Any]) -> date:
    if not row.get("effective_to"):
        return OPEN_END
    return _parse_date(row.get("effective_to"), "登记失效日期")


def _active_at(row: dict[str, Any], at: date) -> bool:
    return _row_start(row) <= at <= _row_end(row)


def _overlap(start_a: date, end_a: date, start_b: date, end_b: date) -> bool:
    return start_a <= end_b and start_b <= end_a


def _period_text(row: dict[str, Any]) -> str:
    end = row.get("effective_to") or "至今"
    return f"{row.get('effective_from')}~{end}"


def _now() -> str:
    return datetime.now().isoformat()


class FieldIdentityService:
    """字段身份、别名有效期、影响单与候选列演练的内存服务。"""

    def __init__(self) -> None:
        self._fields: list[dict[str, Any]] = []
        self._aliases: list[dict[str, Any]] = []
        self._entries: list[dict[str, Any]] = []
        self._revisions: list[dict[str, Any]] = []
        self._recalculations: list[dict[str, Any]] = []
        self._seq = 0

    # ------------------------------------------------------------ 内部

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def _field_rows(self, table: str | None = None) -> list[dict[str, Any]]:
        if table is None:
            return list(self._fields)
        key = normalize(table)
        return [row for row in self._fields if normalize(row["table"]) == key]

    def _alias_rows(self, table: str | None = None) -> list[dict[str, Any]]:
        if table is None:
            return list(self._aliases)
        key = normalize(table)
        return [row for row in self._aliases if normalize(row["table"]) == key]

    def _find_field(self, table: str, column: str, at: date) -> dict[str, Any] | None:
        for row in self._field_rows(table):
            if normalize(row["column"]) == normalize(column) and _active_at(row, at):
                return row
        return None

    def _covering_field(
        self, table: str, column: str, start: date, end: date,
    ) -> dict[str, Any] | None:
        for row in self._field_rows(table):
            if normalize(row["column"]) != normalize(column):
                continue
            if _row_start(row) <= start and end <= _row_end(row):
                return row
        return None

    # ------------------------------------------------------------ 登记

    def register_field(
        self,
        *,
        table: str,
        column: str,
        effective_from: str,
        effective_to: str | None = None,
        data_type: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """登记一个「表 + 列 + 生效期」的字段身份。"""
        table = str(table or "").strip()
        column = str(column or "").strip()
        if not table or not column:
            raise FieldIdentityError("登记字段身份必须提供表名和列名")
        start = _parse_date(effective_from)
        end = _parse_date(effective_to, "失效日期") if effective_to else OPEN_END
        if end < start:
            raise FieldIdentityError("失效日期不能早于生效日期")

        for row in self._field_rows(table):
            if normalize(row["column"]) != normalize(column):
                continue
            if _overlap(start, end, _row_start(row), _row_end(row)):
                raise FieldIdentityError(
                    f"字段身份 {table}.{column} 与已登记的 {_period_text(row)} 有效期重叠，"
                    "不能覆盖已生效的历史"
                )

        record = {
            "id": self._next_id(),
            "table": table,
            "column": column,
            "effective_from": start.isoformat(),
            "effective_to": end.isoformat() if end != OPEN_END else None,
            "data_type": str(data_type or ""),
            "note": str(note or ""),
            "created_at": _now(),
        }
        self._fields.append(record)
        return dict(record)

    def register_alias(
        self,
        *,
        table: str,
        alias: str,
        column: str,
        effective_from: str,
        effective_to: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """登记旧别名及其有效期，别名必须落在同表目标列的有效期内。"""
        table = str(table or "").strip()
        alias = str(alias or "").strip()
        column = str(column or "").strip()
        if not table or not alias or not column:
            raise FieldIdentityError("登记别名必须提供表名、别名和目标列")
        start = _parse_date(effective_from)
        end = _parse_date(effective_to, "失效日期") if effective_to else OPEN_END
        if end < start:
            raise FieldIdentityError("失效日期不能早于生效日期")

        # 表限定和别名有效期必须与目标列的身份匹配。
        if self._covering_field(table, column, start, end) is None:
            raise FieldIdentityError(
                f"别名 {table}.{alias} 的生效期 {start}~{end.isoformat() if end != OPEN_END else '至今'} "
                f"无法匹配到字段身份 {table}.{column}，请先登记覆盖该区间的列身份"
            )

        for row in self._alias_rows(table):
            if normalize(row["alias"]) != normalize(alias):
                continue
            if _overlap(start, end, _row_start(row), _row_end(row)):
                raise FieldIdentityError(
                    f"别名 {table}.{alias} 在 {_period_text(row)} 已生效，"
                    "不能用新列名覆盖旧别名历史；如需改口径请使用修订并给出更晚的生效日期"
                )

        record = {
            "id": self._next_id(),
            "table": table,
            "alias": alias,
            "column": column,
            "effective_from": start.isoformat(),
            "effective_to": end.isoformat() if end != OPEN_END else None,
            "revision": 1,
            "revision_of": None,
            "superseded_by": None,
            "note": str(note or ""),
            "created_at": _now(),
        }
        self._aliases.append(record)
        return dict(record)

    def revise_alias(
        self,
        alias_id: int,
        *,
        effective_from: str,
        column: str | None = None,
        alias: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """修订别名口径：关闭旧记录、追加新记录，并重算受影响血缘与写入结果。

        修订只允许从原生效日期之后的某一天起切换口径：
        - 原记录保留原生效日期与原列，只在切换日之前截止，历史日期照样可回查；
        - 新记录从切换日开始，可以指向同表的另一个列，形成"同一别名、不同有效期、
          不同来源"的两段历史；
        - 切换日晚于原记录失效日时，两段之间保持空档，不强行续接。
        """
        old = self._alias_by_id(alias_id)
        start = _parse_date(effective_from)
        old_start = _row_start(old)
        if start <= old_start:
            raise FieldIdentityError(
                f"修订生效日期必须晚于原生效日期 {old_start}，不能改写已生效的旧别名历史"
            )
        old_end = _row_end(old)
        if old_end != OPEN_END and old_end < start:
            new_end = OPEN_END
        else:
            new_end = old_end

        new_column = str(column or old["column"]).strip()
        new_alias = str(alias or old["alias"]).strip()
        table = old["table"]
        if not new_column or not new_alias:
            raise FieldIdentityError("修订必须保留别名和目标列")

        if self._covering_field(table, new_column, start, new_end) is None:
            raise FieldIdentityError(
                f"修订后的目标列 {table}.{new_column} 在 {start} 之后没有匹配的字段身份"
            )

        for row in self._alias_rows(table):
            if row["id"] == alias_id:
                continue
            if normalize(row["alias"]) != normalize(new_alias):
                continue
            if _overlap(start, new_end, _row_start(row), _row_end(row)):
                raise FieldIdentityError(
                    f"修订后的别名 {table}.{new_alias} 与已有 {_period_text(row)} 有效期重叠"
                )

        # 只关闭旧记录，原生效日期和原列都保留，历史日期仍可回查。
        if old_end == OPEN_END or old_end >= start:
            old["effective_to"] = (start - timedelta(days=1)).isoformat()
        old["revised_at"] = _now()

        record = {
            "id": self._next_id(),
            "table": table,
            "alias": new_alias,
            "column": new_column,
            "effective_from": start.isoformat(),
            "effective_to": new_end.isoformat() if new_end != OPEN_END else None,
            "revision": int(old.get("revision", 1)) + 1,
            "revision_of": old["id"],
            "superseded_by": None,
            "note": str(note or ""),
            "created_at": _now(),
        }
        old["superseded_by"] = record["id"]
        self._aliases.append(record)

        recalculation = self._recalculate_alias_change(
            table=table,
            alias=new_alias,
            old_column=old["column"],
            new_column=new_column,
            old_start=old_start,
            effective_from=start,
            note=str(note or ""),
        )
        revision = {
            "id": self._next_id(),
            "alias_id": record["id"],
            "previous_alias_id": old["id"],
            "table": table,
            "alias": new_alias,
            "old_column": old["column"],
            "new_column": new_column,
            "effective_from": start.isoformat(),
            "created_at": _now(),
            "note": str(note or ""),
            "recalculation_id": recalculation["id"],
        }
        self._revisions.append(revision)
        return {
            "ok": True,
            "alias": dict(record),
            "previous_alias": dict(old),
            "revision": revision,
            "recalculation": recalculation,
            "message": (
                f"别名 {table}.{new_alias} 已从 {start} 起改为 {new_column}，"
                f"旧日期 {old_start} 仍可回查 {old['column']}"
            ),
        }

    def _alias_by_id(self, alias_id: int) -> dict[str, Any]:
        for row in self._aliases:
            if int(row["id"]) == int(alias_id):
                return row
        raise FieldIdentityError(f"别名记录 {alias_id} 不存在")

    # ------------------------------------------------------------ 查询

    def query_identity(
        self,
        *,
        table: str | None = None,
        effective_date: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """按表、生效日期和字段名/别名查询字段身份。

        给表名时只在该表内核实；不给表名时汇总所有候选。
        出现多个同名字段来源时 resolved=False 且不填生产表，不猜来源。
        """
        at = _parse_date(effective_date)
        name_key = normalize(name) if name else ""

        fields = [row for row in self._field_rows(table) if _active_at(row, at)]
        aliases = [row for row in self._alias_rows(table) if _active_at(row, at)]

        if not name_key:
            identities = [dict(row) for row in fields]
            resolved = len(identities) == 1
            production_table = identities[0]["table"] if resolved else None
            production_column = identities[0]["column"] if resolved else None
            result = {
                "query": {
                    "table": str(table or ""),
                    "effective_date": at.isoformat(),
                    "name": "",
                },
                "resolved": resolved,
                "ambiguous": False,
                "production_table": production_table,
                "production_column": production_column,
                "identities": identities,
                "aliases": [dict(row) for row in aliases],
                "candidates": identities,
                "read_paths": [],
                "lineage_chain": [],
                "impact": _empty_impact(),
                "message": (
                    "按表与生效日期返回当时有效的字段身份"
                    if table else "未限定表，列出该日期所有字段身份"
                ),
            }
            if resolved:
                self._attach_lineage(result, production_table, production_column)
            return result

        candidates: dict[tuple[str, str], dict[str, Any]] = {}

        def _add_candidate(
            row: dict[str, Any],
            *,
            matched_by: str,
            alias_name: str | None = None,
        ) -> None:
            key = (normalize(row["table"]), normalize(row["column"]))
            candidate = candidates.get(key)
            if candidate is None:
                candidate = {
                    "table": row["table"],
                    "column": row["column"],
                    "matched_by": [],
                    "aliases": [],
                    "effective_from": row["effective_from"],
                    "effective_to": row.get("effective_to"),
                }
                candidates[key] = candidate
            if matched_by not in candidate["matched_by"]:
                candidate["matched_by"].append(matched_by)
            if alias_name and alias_name not in candidate["aliases"]:
                candidate["aliases"].append(alias_name)

        for row in fields:
            if normalize(row["column"]) == name_key:
                _add_candidate(row, matched_by="column")
        for row in aliases:
            if normalize(row["alias"]) == name_key:
                field_row = self._find_field(row["table"], row["column"], at)
                if field_row is None:
                    continue
                _add_candidate(field_row, matched_by="alias", alias_name=row["alias"])
                key = (normalize(row["table"]), normalize(row["column"]))
                candidates[key]["effective_from"] = row["effective_from"]
                candidates[key]["effective_to"] = row.get("effective_to")

        candidate_list = sorted(
            candidates.values(), key=lambda item: (item["table"], item["column"])
        )
        targets = {(normalize(c["table"]), normalize(c["column"])) for c in candidate_list}
        resolved = len(targets) == 1
        ambiguous = len(targets) > 1

        production_table = candidate_list[0]["table"] if resolved else None
        production_column = candidate_list[0]["column"] if resolved else None

        result = {
            "query": {
                "table": str(table or ""),
                "effective_date": at.isoformat(),
                "name": str(name or ""),
            },
            "resolved": resolved,
            "ambiguous": ambiguous,
            "production_table": production_table,
            "production_column": production_column,
            "identities": [dict(row) for row in fields],
            "aliases": [dict(row) for row in aliases],
            "candidates": candidate_list,
            "read_paths": [],
            "lineage_chain": [],
            "impact": _empty_impact(),
            "message": "",
        }

        if resolved:
            result["message"] = (
                f"已核实 {at} 的生产字段为 {production_table}.{production_column}"
            )
            self._attach_lineage(result, production_table, production_column)
        elif ambiguous:
            result["message"] = (
                f"在 {at} 找到 {len(candidate_list)} 个同名候选，未限定表时不能猜来源，"
                "请补充表名后重查"
            )
        else:
            hint = ""
            if table:
                periods = [
                    _period_text(row) for row in self._field_rows(table)
                    if normalize(row["column"]) == name_key
                ]
                if periods:
                    hint = f"；该字段已登记的有效期为 {', '.join(periods)}"
            result["message"] = (
                f"在 {at} 未找到可确认的字段身份 {str(name)}{hint}"
            )
        return result

    def _attach_lineage(
        self, result: dict[str, Any], table: str, column: str,
    ) -> None:
        read_paths = self._read_paths(table, column)
        chain, downstream_targets = self._field_downstream(table, column)
        basis = "field_lineage"

        # 字段级血缘缺失时退回表级拓扑，但仍标明证据基础，不能当成字段级结论。
        if not read_paths and not chain:
            read_paths = self._table_read_paths(table)
            downstream_targets = self._pipeline_downstream(table) or [
                path["target"] for path in read_paths if path["direction"] == "out"
            ]
            basis = "table_pipeline"

        target_tables = list(dict.fromkeys(downstream_targets)) or [table]
        # 写入任务既包括写本表的任务，也包括写下游表的任务：Schema 变更两边都要评估。
        # 本表排最前，下游按血缘深度由近到远，符合"先评估直接写入、再看下游"的阅读顺序。
        write_scope: list[str] = [table]
        for target in target_tables:
            if normalize(target) not in {normalize(item) for item in write_scope}:
                write_scope.append(target)
        write_tasks = self._write_tasks(write_scope)
        rules = self._quality_rules(write_scope, column)
        pipelines = self._pipelines(table, target_tables)

        result["read_paths"] = read_paths
        result["lineage_chain"] = chain
        result["impact"] = {
            "basis": basis,
            "downstream_targets": target_tables,
            "read_paths": read_paths,
            "write_tasks": write_tasks,
            "affected_quality_rules": rules,
            "affected_pipelines": pipelines,
            "total_impact_items": len(target_tables) + len(rules) + len(write_tasks),
        }

    def _table_read_paths(self, table: str) -> list[dict[str, Any]]:
        """表级读取路径，用于字段级血缘尚未登记时提供定位线索。"""
        table_key = normalize(table)
        paths: list[dict[str, Any]] = []
        for edge in store.rows("lineage_graph"):
            source = str(edge.get("源节点", ""))
            target = str(edge.get("目标节点", ""))
            if table_key not in (normalize(source), normalize(target)):
                continue
            paths.append({
                "edge_id": edge.get("id"),
                "source": source,
                "target": target,
                "field": "",
                "direction": "out" if normalize(source) == table_key else "in",
                "transform": edge.get("转换类型"),
                "data_volume": edge.get("数据量级"),
                "status": edge.get("status"),
                "active": edge.get("status") == "活跃",
                "basis": "table_pipeline",
            })
        return paths

    def _pipeline_downstream(self, table: str) -> list[str]:
        key = normalize(table)
        targets: list[str] = []

        def _add(target: str) -> None:
            if target and normalize(target) not in {normalize(t) for t in targets}:
                targets.append(target)

        for pipeline in store.rows("pipeline_def"):
            if normalize(pipeline.get("输入源表")) == key:
                _add(str(pipeline.get("输出目标表", "")))
        for edge in store.rows("lineage_graph"):
            if normalize(edge.get("源节点")) == key:
                _add(str(edge.get("目标节点", "")))
        return targets

    def _read_paths(self, table: str, column: str) -> list[dict[str, Any]]:
        token = normalize(column)
        table_key = normalize(table)
        paths: list[dict[str, Any]] = []
        for edge in store.rows("lineage_graph"):
            if token not in field_tokens(edge.get("传递字段")):
                continue
            source = str(edge.get("源节点", ""))
            target = str(edge.get("目标节点", ""))
            if normalize(source) == table_key:
                direction = "out"
            elif normalize(target) == table_key:
                direction = "in"
            else:
                continue
            paths.append({
                "edge_id": edge.get("id"),
                "source": source,
                "target": target,
                "field": column,
                "direction": direction,
                "transform": edge.get("转换类型"),
                "data_volume": edge.get("数据量级"),
                "status": edge.get("status"),
                "active": edge.get("status") == "活跃",
            })
        return paths

    def _field_downstream(
        self, table: str, column: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        token = normalize(column)
        index: dict[str, list[dict[str, Any]]] = {}
        for edge in store.rows("lineage_graph"):
            if token not in field_tokens(edge.get("传递字段")):
                continue
            index.setdefault(normalize(edge.get("源节点", "")), []).append(edge)

        chains: list[dict[str, Any]] = []
        targets: list[str] = []
        visited = {normalize(table)}
        queue: list[tuple[str, int, list[dict[str, Any]]]] = [(table, 0, [])]
        while queue:
            current, depth, path = queue.pop(0)
            if depth >= 5:
                continue
            for edge in index.get(normalize(current), []):
                target = str(edge.get("目标节点", ""))
                if normalize(target) in visited:
                    continue
                visited.add(normalize(target))
                hop = {
                    "source": current,
                    "target": target,
                    "field": column,
                    "transform": edge.get("转换类型"),
                    "edge_id": edge.get("id"),
                }
                next_path = [*path, hop]
                chains.append({
                    "target_table": target,
                    "depth": depth + 1,
                    "path": next_path,
                })
                targets.append(target)
                queue.append((target, depth + 1, next_path))
        return chains, targets

    def _write_tasks(self, targets: list[str]) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        nodes = store.rows("task_node")
        pipelines = store.rows("pipeline_def")

        def _add(
            *, task_name: str, pipeline: str, target: str, source: str,
        ) -> None:
            key = (normalize(task_name), normalize(pipeline), normalize(target))
            if key in seen:
                return
            seen.add(key)
            tasks.append({
                "task_name": task_name,
                "pipeline": pipeline,
                "target_table": target,
                "source": source,
            })

        for target in targets:
            for pipeline in pipelines:
                if normalize(pipeline.get("输出目标表")) != normalize(target):
                    continue
                pipeline_name = str(pipeline.get("流水线名称", ""))
                matched = False
                # 只认真正写入目标表的节点；读取源表的抽取节点不算写入任务。
                for node in nodes:
                    if normalize(node.get("所属管线")) != normalize(pipeline_name):
                        continue
                    logic = str(node.get("处理逻辑", ""))
                    if not mentions_table(logic, target):
                        continue
                    if not _is_write(logic):
                        continue
                    _add(
                        task_name=str(node.get("节点名称", "")),
                        pipeline=pipeline_name,
                        target=target,
                        source="task_node",
                    )
                    matched = True
                if not matched:
                    _add(
                        task_name=f"写入 {target}",
                        pipeline=pipeline_name,
                        target=target,
                        source="pipeline_def",
                    )
            direct = [
                node for node in nodes
                if mentions_table(node.get("处理逻辑"), target)
                and _is_write(node.get("处理逻辑"))
            ]
            for node in direct:
                _add(
                    task_name=str(node.get("节点名称", "")),
                    pipeline=str(node.get("所属管线", "")),
                    target=target,
                    source="task_node",
                )
        return tasks

    def _quality_rules(self, targets: list[str], column: str) -> list[dict[str, Any]]:
        token = normalize(column)
        target_keys = {normalize(target) for target in targets}
        rules: list[dict[str, Any]] = []
        for rule in store.rows("quality_rule"):
            if normalize(rule.get("目标表")) not in target_keys:
                continue
            if normalize(rule.get("校验字段")) != token:
                continue
            rules.append({
                "rule_name": rule.get("规则名称"),
                "target_table": rule.get("目标表"),
                "field": rule.get("校验字段"),
                "status": rule.get("status"),
            })
        return rules

    def _pipelines(self, source: str, targets: list[str]) -> list[str]:
        source_key = normalize(source)
        target_keys = {normalize(target) for target in targets}
        names: list[str] = []
        for pipeline in store.rows("pipeline_def"):
            if (
                normalize(pipeline.get("输入源表")) == source_key
                or normalize(pipeline.get("输出目标表")) in target_keys
            ):
                name = str(pipeline.get("流水线名称", ""))
                if name and name not in names:
                    names.append(name)
        return names

    # ------------------------------------------------------------ 影响单

    def generate_impact_entries(
        self,
        *,
        table: str | None = None,
        column: str | None = None,
        effective_date: str | None = None,
        change_type: str = "MODIFY_TYPE",
        note: str = "",
    ) -> dict[str, Any]:
        """用字段影响数据生成写入任务的评估条目。

        能核实的条目直接标记可提交；暂时辨不清来源的条目保留候选，
        状态为待补充证据，不允许确认，也不进入候选列演练。
        """
        date_provided = bool(str(effective_date or "").strip())
        query = self.query_identity(
            table=table,
            effective_date=effective_date or date.today().isoformat(),
            name=column,
        )
        query_date = query["query"]["effective_date"] if date_provided else ""
        has_table = bool(str(table or "").strip())
        verified = query["resolved"] and has_table and date_provided
        entries: list[dict[str, Any]] = []

        if verified:
            source_table = query["production_table"]
            source_column = query["production_column"]
            impact = query["impact"]
            tasks = impact["write_tasks"] or [{
                "task_name": f"写入 {source_table}",
                "pipeline": "",
                "target_table": source_table,
                "source": "derived",
            }]
            chain_by_target = {
                normalize(item["target_table"]): item["path"]
                for item in query["lineage_chain"]
            }
            for task in tasks:
                target = task["target_table"]
                entries.append(self._new_entry(
                    task=task,
                    source_table=source_table,
                    source_column=source_column,
                    target_table=target,
                    effective_date=query_date,
                    change_type=change_type,
                    note=note,
                    status="待确认",
                    submittable=True,
                    missing=[],
                    lineage_path=chain_by_target.get(normalize(target), []),
                    read_paths=query["read_paths"],
                ))
        else:
            missing: list[str] = []
            if not has_table or query["ambiguous"]:
                missing.append("表")
            if not date_provided:
                missing.append("生效日期")
            candidates = query["candidates"]
            if not candidates:
                missing.append("列")
            for candidate in candidates:
                entries.append(self._new_entry(
                    task={
                        "task_name": f"待核实写入任务（{candidate['table']}.{candidate['column']}）",
                        "pipeline": "",
                        "target_table": "",
                        "source": "candidate",
                    },
                    source_table="",
                    source_column=candidate["column"],
                    target_table="",
                    effective_date=query_date,
                    change_type=change_type,
                    note=note,
                    status="待补充证据",
                    submittable=False,
                    missing=sorted(set(missing)),
                    lineage_path=[],
                    read_paths=[],
                    candidate=candidate,
                ))
            if not candidates:
                entries.append(self._new_entry(
                    task={
                        "task_name": "待核实写入任务",
                        "pipeline": "",
                        "target_table": "",
                        "source": "unresolved",
                    },
                    source_table=str(table or ""),
                    source_column=str(column or ""),
                    target_table="",
                    effective_date=query_date,
                    change_type=change_type,
                    note=note,
                    status="待补充证据",
                    submittable=False,
                    missing=sorted(set(missing)),
                    lineage_path=[],
                    read_paths=[],
                    candidate=None,
                ))

        self._entries.extend(entries)
        return {
            "query": query["query"],
            "resolved": query["resolved"],
            "ambiguous": query["ambiguous"],
            "production_table": query["production_table"],
            "production_column": query["production_column"],
            "impact": query["impact"],
            "message": query["message"],
            "entries": [dict(entry) for entry in entries],
            "submittable_count": sum(1 for entry in entries if entry["可提交"]),
            "blocked_count": sum(1 for entry in entries if not entry["可提交"]),
        }

    def _new_entry(
        self,
        *,
        task: dict[str, Any],
        source_table: str,
        source_column: str,
        target_table: str,
        effective_date: str,
        change_type: str,
        note: str,
        status: str,
        submittable: bool,
        missing: list[str],
        lineage_path: list[dict[str, Any]],
        read_paths: list[dict[str, Any]],
        candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": self._next_id(),
            "task_name": task["task_name"],
            "pipeline": task["pipeline"],
            "production_table": source_table,
            "field": source_column,
            "target_table": target_table,
            "change_type": change_type,
            "effective_date": effective_date,
            "status": status,
            "可提交": submittable,
            "缺失证据": missing,
            "lineage_path": lineage_path,
            "read_paths": read_paths,
            "source": task["source"],
            "note": str(note or ""),
            "created_at": _now(),
            "confirmed_at": None,
        }
        if candidate is not None:
            entry["candidate"] = candidate
        return entry

    def generate_from_change(self, change_id: int, **overrides: Any) -> dict[str, Any]:
        """从 schema_evolution 的变更记录生成影响单条目。

        支持一次评估多个字段：可核实的字段生成可提交条目，暂时辨不清来源的
        字段保留为待补充证据条目，影响单里同时标出两类，便于分头补证据。
        """
        change = store.find("schema_evolution", change_id)
        if change is None:
            raise FieldIdentityError(f"变更记录 {change_id} 不存在")
        table = str(change.get("目标表", ""))
        detail = str(change.get("变更详情", ""))

        requested = overrides.get("columns") or overrides.get("column") or overrides.get("field")
        if isinstance(requested, str):
            columns = [requested.strip()] if requested.strip() else []
        elif requested:
            columns = [str(item).strip() for item in requested if str(item).strip()]
        else:
            columns = []
        if not columns:
            columns = [
                row["column"] for row in self._field_rows(table)
                if mentions_field(detail, row["column"])
            ]
        if not columns:
            raise FieldIdentityError(
                f"无法从变更记录 {change_id} 的详情识别字段，请显式指定要评估的列"
            )

        effective_date = (
            overrides.get("effective_date")
            or str(change.get("变更时间", ""))[:10]
        )
        change_type = str(change.get("变更类型", "MODIFY_TYPE"))
        note = str(overrides.get("note") or f"来自变更记录 {change_id}")

        batches = [
            self.generate_impact_entries(
                table=table,
                column=column,
                effective_date=effective_date,
                change_type=change_type,
                note=note,
            )
            for column in columns
        ]
        entries = [entry for batch in batches for entry in batch["entries"]]
        return {
            "query": {
                "table": table,
                "effective_date": batches[0]["query"]["effective_date"],
                "name": ",".join(columns),
            },
            "resolved": all(batch["resolved"] for batch in batches),
            "ambiguous": any(batch["ambiguous"] for batch in batches),
            "production_table": (
                batches[0]["production_table"] if len(batches) == 1 else None
            ),
            "production_column": (
                batches[0]["production_column"] if len(batches) == 1 else None
            ),
            "impact": batches[0]["impact"] if len(batches) == 1 else _empty_impact(),
            "message": "；".join(batch["message"] for batch in batches),
            "entries": entries,
            "submittable_count": sum(1 for entry in entries if entry["可提交"]),
            "blocked_count": sum(1 for entry in entries if not entry["可提交"]),
            "change": {
                "change_id": change_id,
                "table": table,
                "change_type": change.get("变更类型"),
                "detail": change.get("变更详情"),
                "affected_pipelines": change.get("影响管线"),
            },
        }

    def list_entries(
        self, *, status: str | None = None, table: str | None = None,
    ) -> dict[str, Any]:
        rows = list(self._entries)
        if status:
            rows = [row for row in rows if row["status"] == status]
        if table:
            key = normalize(table)
            rows = [
                row for row in rows
                if normalize(row.get("production_table")) == key
                or normalize(row.get("target_table")) == key
            ]
        return {
            "entries": [dict(row) for row in rows],
            "submittable": [row["id"] for row in rows if row["可提交"]],
            "blocked": [
                {
                    "entry_id": row["id"],
                    "status": row["status"],
                    "missing_evidence": list(row["缺失证据"]),
                }
                for row in rows if not row["可提交"]
            ],
        }

    def confirm_entry(self, entry_id: int) -> dict[str, Any]:
        entry = self._entry_by_id(entry_id)
        if entry["status"] == "已确认":
            return {"ok": True, "message": "条目已确认", "entry": dict(entry)}
        if entry["缺失证据"] or not entry["可提交"]:
            missing = "、".join(entry["缺失证据"]) or "写入任务"
            raise FieldIdentityError(
                f"条目 {entry_id} 缺少{missing}证据，不能确认，也不进入候选列演练"
            )
        entry["status"] = "已确认"
        entry["confirmed_at"] = _now()
        return {"ok": True, "message": "评估条目已确认", "entry": dict(entry)}

    def _entry_by_id(self, entry_id: int) -> dict[str, Any]:
        for entry in self._entries:
            if int(entry["id"]) == int(entry_id):
                return entry
        raise FieldIdentityError(f"评估条目 {entry_id} 不存在")

    def candidate_columns(
        self, *, table: str | None = None, column: str | None = None,
    ) -> dict[str, Any]:
        """候选列演练输入：只允许已确认的评估条目。"""
        confirmed = [
            entry for entry in self._entries if entry["status"] == "已确认"
        ]
        if table:
            key = normalize(table)
            confirmed = [
                entry for entry in confirmed
                if normalize(entry.get("production_table")) == key
                or normalize(entry.get("target_table")) == key
            ]
        if column:
            key = normalize(column)
            confirmed = [
                entry for entry in confirmed if normalize(entry.get("field")) == key
            ]
        return {
            "ok": True,
            "candidates": [self._candidate_view(entry) for entry in confirmed],
            "count": len(confirmed),
            "message": (
                "仅已确认条目可作为候选列演练输入"
                if confirmed else "暂无已确认条目，请先补齐证据并确认"
            ),
        }

    def run_drill(self, entry_ids: list[int]) -> dict[str, Any]:
        if not entry_ids:
            raise FieldIdentityError("候选列演练至少需要一条已确认的评估条目")
        selected: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for entry_id in entry_ids:
            entry = self._entry_by_id(int(entry_id))
            if entry["status"] != "已确认":
                rejected.append({
                    "entry_id": entry["id"],
                    "status": entry["status"],
                    "missing_evidence": entry["缺失证据"],
                })
            else:
                selected.append(entry)
        if rejected:
            detail = "、".join(
                f"{item['entry_id']}({item['status']})" for item in rejected
            )
            raise FieldIdentityError(
                f"以下条目未确认，不能作为候选列演练输入：{detail}"
            )
        write_results = [
            {
                "task_name": entry["task_name"],
                "pipeline": entry["pipeline"],
                "target_table": entry["target_table"],
                "field": entry["field"],
                "read_table": entry["production_table"],
                "result": f"{entry['target_table'] or entry['production_table']}.{entry['field']}",
            }
            for entry in selected
        ]
        return {
            "ok": True,
            "candidate_columns": [self._candidate_view(entry) for entry in selected],
            "write_results": write_results,
            "count": len(selected),
            "message": f"已用 {len(selected)} 条已确认条目执行候选列演练",
        }

    def _candidate_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "entry_id": entry["id"],
            "field": entry["field"],
            "production_table": entry["production_table"],
            "target_table": entry["target_table"],
            "pipeline": entry["pipeline"],
            "task_name": entry["task_name"],
            "effective_date": entry["effective_date"],
        }

    # ------------------------------------------------------------ 历史

    def list_fields(self, *, table: str | None = None) -> list[dict[str, Any]]:
        rows = self._field_rows(table)
        rows.sort(key=lambda row: (normalize(row["table"]), normalize(row["column"]),
                                   row["effective_from"]))
        return [dict(row) for row in rows]

    def alias_history(
        self, *, table: str | None = None, alias: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._alias_rows(table)
        if alias:
            key = normalize(alias)
            rows = [row for row in rows if normalize(row["alias"]) == key]
        rows.sort(key=lambda row: (normalize(row["table"]), normalize(row["alias"]),
                                   row["effective_from"]))
        return [dict(row) for row in rows]

    def history_query(
        self,
        *,
        table: str | None = None,
        name: str | None = None,
        effective_date: str | None = None,
    ) -> dict[str, Any]:
        """历史日期回查：返回当时能确认的生产表与列。"""
        return self.query_identity(
            table=table, effective_date=effective_date, name=name,
        )

    # ------------------------------------------------------------ 重算

    def _recalculate_alias_change(
        self,
        *,
        table: str,
        alias: str,
        old_column: str,
        new_column: str,
        old_start: date,
        effective_from: date,
        note: str,
    ) -> dict[str, Any]:
        old_date = old_start.isoformat()
        before = self.query_identity(
            table=table, effective_date=old_date, name=old_column,
        )
        after = self.query_identity(
            table=table,
            effective_date=effective_from.isoformat(),
            name=new_column,
        )
        before_targets = set(
            before["impact"]["downstream_targets"] if before["resolved"] else []
        )
        after_targets = set(
            after["impact"]["downstream_targets"] if after["resolved"] else []
        )
        affected_targets = sorted(before_targets | after_targets)
        write_tasks = self._write_tasks(affected_targets or [table])

        def _annotate(
            paths: list[dict[str, Any]], column: str, basis: str,
        ) -> list[dict[str, Any]]:
            # 字段级血缘尚未登记到新列时，路径会退回表级；把依据标出来，
            # 避免把表级线索误读成字段级结论。
            return [
                {
                    **path,
                    "field": path.get("field") or "",
                    "matched_column": column,
                    "basis": path.get("basis") or basis,
                }
                for path in paths
            ]

        recalc = {
            "id": self._next_id(),
            "table": table,
            "alias": alias,
            "old_column": old_column,
            "new_column": new_column,
            "effective_from": effective_from.isoformat(),
            "recalculated_paths": {
                "before": _annotate(
                    before["read_paths"], old_column, before["impact"]["basis"],
                ),
                "after": _annotate(
                    after["read_paths"], new_column, after["impact"]["basis"],
                ),
            },
            "lineage_chain": after["lineage_chain"],
            "affected_write_targets": affected_targets,
            "write_results": [
                {
                    "task_name": task["task_name"],
                    "pipeline": task["pipeline"],
                    "target_table": task["target_table"],
                    "field": new_column,
                }
                for task in write_tasks
            ],
            "history_snapshot": {
                "query_date": old_date,
                "table": table,
                "column": old_column,
                "preserved": True,
                "note": "旧别名记录仅关闭有效期，原生效日期与列名保留可回查",
            },
            "note": note,
            "computed_at": _now(),
        }
        self._recalculations.append(recalc)
        return recalc

    def recalculations(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._recalculations]

    # ------------------------------------------------------------ 快照

    def snapshot(self) -> dict[str, Any]:
        return {
            "fields": len(self._fields),
            "aliases": len(self._aliases),
            "impact_entries": len(self._entries),
            "confirmed_entries": sum(
                1 for entry in self._entries if entry["status"] == "已确认"
            ),
            "revisions": len(self._revisions),
            "recalculations": len(self._recalculations),
        }


def _empty_impact() -> dict[str, Any]:
    return {
        "basis": "",
        "downstream_targets": [],
        "read_paths": [],
        "write_tasks": [],
        "affected_quality_rules": [],
        "affected_pipelines": [],
        "total_impact_items": 0,
    }


field_identity_service = FieldIdentityService()
