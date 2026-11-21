"""数据血缘领域服务：管理表间字段级血缘图、影响分析和追溯查询。

上游：pipeline_def + task_node 产出数据流转 → lineage_graph 建立血缘
消费：schema_evolution 消费血缘信息评估变更影响
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.services.field_identity import field_tokens
from app.store import store


class LineageGraphService(BaseModuleService):
    MODULE = "lineage_graph"
    ENTITY = "血缘边"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["源节点", "目标节点", "传递字段", "转换类型", "数据量级", "血缘深度", "是否活跃"]
    REQUIRED_FIELDS = ["源节点", "目标节点", "传递字段"]
    STATUS_ORDER = ["活跃", "已失效", "已归档"]
    ACTION_RULES = {"标记失效": "活跃", "归档边": "已失效"}
    NEGATIVE_ACTIONS = []

    def query_upstream(self, target_node: str) -> list[dict[str, Any]]:
        """查询指定节点的上游血缘。

        遍历 store 中的血缘边记录，查找以该节点为目标的边，
        返回上游源节点信息供影响分析消费。
        """
        result: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if target_node.casefold() in str(row.get("目标节点", "")).casefold():
                result.append(dict(row))
        return result

    def query_downstream(self, source_node: str) -> list[dict[str, Any]]:
        """查询指定节点的下游血缘。

        遍历 store 中的血缘边记录，查找以该节点为源的边，
        返回下游目标节点信息供变更影响范围评估。
        """
        result: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if source_node.casefold() in str(row.get("源节点", "")).casefold():
                result.append(dict(row))
        return result

    def trace_field_lineage(self, field_name: str) -> dict[str, Any]:
        """追溯字段的端到端血缘链路。

        从目标表出发，沿血缘边反向追溯字段的来源表和处理节点，
        构建字段级数据流向图。字段按分隔符精确匹配，避免 order_code
        误命中 order_code_v2 这类前缀相同的列。
        """
        edges = store.rows(self.MODULE)
        token = str(field_name).strip().casefold()
        field_edges: list[dict[str, Any]] = []
        for edge in edges:
            if token in field_tokens(edge.get("传递字段", "")):
                field_edges.append({
                    "source": edge.get("源节点"),
                    "target": edge.get("目标节点"),
                    "transform": edge.get("转换类型"),
                    "data_volume": edge.get("数据量级"),
                    "depth": edge.get("血缘深度"),
                })

        return {
            "field": field_name,
            "edge_count": len(field_edges),
            "lineage_chain": field_edges,
            "traced_at": datetime.now().isoformat(),
        }

    def compute_impact_scope(self, source_node: str, max_depth: int = 5) -> dict[str, Any]:
        """计算源节点变更的影响范围。

        向下游递归遍历血缘链路，统计受影响的表和处理节点数量，
        用于变更审批和风险评估。
        """
        all_downstream = self.query_downstream(source_node)
        visited: set[str] = {source_node}
        affected_tables: set[str] = set()

        def _traverse(target: str, depth: int):
            if depth > max_depth:
                return
            for row in store.rows(self.MODULE):
                tgt = str(row.get("目标节点", ""))
                if target.casefold() in tgt.casefold() and tgt not in visited:
                    visited.add(tgt)
                    affected_tables.add(tgt)
                    _traverse(tgt, depth + 1)

        for edge in all_downstream:
            tgt = str(edge.get("目标节点", ""))
            _traverse(tgt, 1)

        return {
            "source": source_node,
            "direct_downstream": len(all_downstream),
            "total_affected": len(affected_tables),
            "max_depth": max_depth,
            "affected_tables": sorted(affected_tables),
            "computed_at": datetime.now().isoformat(),
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, msg = super().run_action(entry_id, action)
        if entry:
            entry["last_operated_at"] = datetime.now().isoformat()
        return entry, msg