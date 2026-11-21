"""Schema变更领域服务：管理表字段变更、兼容性评估和影响分析。

上游：异常捕获字段级异常 → schema_evolution 记录变更
消费：quality_rule 根据变更结果调整校验规则
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.services.field_identity import (
    field_identity_service,
    field_tokens,
    mentions_field,
)
from app.store import store


class SchemaEvolutionService(BaseModuleService):
    MODULE = "schema_evolution"
    ENTITY = "变更记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["变更时间", "目标表", "变更类型", "变更详情", "兼容性评估", "影响管线", "审批状态"]
    REQUIRED_FIELDS = ["变更时间", "目标表", "变更类型"]
    STATUS_ORDER = ["待评估", "兼容", "不兼容", "已执行", "已回滚"]
    ACTION_RULES = {"评估兼容": "待评估", "执行变更": "兼容", "回滚变更": "不兼容"}
    NEGATIVE_ACTIONS = ["回滚变更"]

    def query_by_table(self, table_name: str) -> list[dict[str, Any]]:
        """按目标表查询 Schema 变更记录。

        从 store 中筛选指定表的字段变更历史，
        返回变更列表供数据治理和审计消费。
        """
        result: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if table_name.casefold() in str(row.get("目标表", "")).casefold():
                result.append(dict(row))
        return result

    def evaluate_compatibility(self, change_id: int) -> dict[str, Any]:
        """评估 Schema 变更的兼容性。

        分析变更类型和影响范围，根据变更类型判断
        是向前兼容还是向后不兼容的变更。
        """
        entry = store.find(self.MODULE, change_id)
        if entry is None:
            return {"error": f"变更记录 {change_id} 不存在"}

        change_type = str(entry.get("变更类型", ""))
        change_detail = str(entry.get("变更详情", ""))
        table_name = str(entry.get("目标表", ""))

        is_compatible = True
        risk_level = "低"

        compatible_ops = {"增加字段", "新增列", "添加字段", "ADD COLUMN"}
        incompatible_ops = {"删除字段", "重命名字段", "修改类型", "删除列",
                             "DROP COLUMN", "MODIFY COLUMN", "CHANGE COLUMN"}

        if change_type in compatible_ops:
            is_compatible = True
            risk_level = "低"
        elif change_type in incompatible_ops:
            is_compatible = False
            risk_level = "高"
        else:
            is_compatible = True
            risk_level = "中"

        affected_pipelines = str(entry.get("影响管线", ""))
        pipeline_list = [
            p.strip() for p in affected_pipelines.split(",") if p.strip()
        ] if affected_pipelines else []

        entry["兼容性评估"] = "兼容" if is_compatible else "不兼容"

        return {
            "change_id": change_id,
            "table": table_name,
            "change_type": change_type,
            "change_detail": change_detail,
            "is_compatible": is_compatible,
            "risk_level": risk_level,
            "affected_pipelines": pipeline_list,
            "recommendation": (
                "变更可安全执行" if is_compatible
                else "变更不兼容，需制定回滚方案并通知下游管线"
            ),
            "evaluated_at": datetime.now().isoformat(),
        }

    def analyze_field_impact(
        self, table_name: str, field_name: str, effective_date: str | None = None,
    ) -> dict[str, Any]:
        """分析字段变更的影响范围。

        通过 lineage_graph 血缘信息回溯该字段的下游使用情况，
        统计受影响的处理和校验规则。给出生效日期时先按字段身份核实
        生产表和列，遇到同名候选不猜来源，影响单直接标记待补充证据。
        """
        changes = self.query_by_table(table_name)
        related_changes = [
            c for c in changes
            if mentions_field(c.get("变更详情", ""), field_name)
        ]

        if effective_date:
            identity = field_identity_service.query_identity(
                table=table_name,
                effective_date=effective_date,
                name=field_name,
            )
            if not identity["resolved"]:
                return {
                    "table": table_name,
                    "field": field_name,
                    "effective_date": effective_date,
                    "resolved": False,
                    "ambiguous": identity["ambiguous"],
                    "candidates": identity["candidates"],
                    "message": identity["message"],
                    "change_history_count": len(related_changes),
                    "affected_downstream_nodes": [],
                    "affected_quality_rules": [],
                    "total_impact_items": 0,
                    "risk_summary": "字段身份未核实，不能给出字段级影响结论",
                    "impact_entries": [],
                    "analyzed_at": datetime.now().isoformat(),
                }
            impact = identity["impact"]
            return {
                "table": identity["production_table"],
                "field": identity["production_column"],
                "effective_date": effective_date,
                "change_history_count": len(related_changes),
                "affected_downstream_nodes": impact["downstream_targets"],
                "affected_quality_rules": [
                    rule["rule_name"] for rule in impact["affected_quality_rules"]
                ],
                "read_paths": impact["read_paths"],
                "write_tasks": impact["write_tasks"],
                "total_impact_items": impact["total_impact_items"],
                "risk_summary": (
                    "高影响：涉及多个下游节点和质量规则"
                    if impact["total_impact_items"] > 3 else "影响范围可控"
                ),
                "impact_entries": impact.get("impact_entries", []),
                "analyzed_at": datetime.now().isoformat(),
            }

        token = str(field_name).strip().casefold()
        lineage_edges = store.rows("lineage_graph")
        affected_targets: list[str] = []
        for edge in lineage_edges:
            if token in field_tokens(edge.get("传递字段", "")):
                target = str(edge.get("目标节点", ""))
                if target and target not in affected_targets:
                    affected_targets.append(target)

        quality_rules = store.rows("quality_rule")
        affected_rules = [
            rule.get("规则名称") for rule in quality_rules
            if str(rule.get("校验字段", "")).strip().casefold() == token
        ]

        return {
            "table": table_name,
            "field": field_name,
            "change_history_count": len(related_changes),
            "affected_downstream_nodes": affected_targets,
            "affected_quality_rules": affected_rules,
            "total_impact_items": len(affected_targets) + len(affected_rules),
            "risk_summary": (
                "高影响：涉及多个下游节点和质量规则" if len(affected_targets) + len(affected_rules) > 3
                else "影响范围可控"
            ),
            "analyzed_at": datetime.now().isoformat(),
        }

    def impact_entries_for_change(
        self, change_id: int, *, column: str | None = None,
        columns: list[str] | None = None,
        effective_date: str | None = None,
    ) -> dict[str, Any]:
        """用字段影响数据为一次 Schema 变更生成写入任务评估条目。"""
        return field_identity_service.generate_from_change(
            change_id, column=column, columns=columns,
            effective_date=effective_date,
        )

    def field_identity_changes(self, table_name: str) -> dict[str, Any]:
        """列出某表按生效期登记过的列名与别名，供 Schema 变更对照。"""
        fields = field_identity_service.list_fields(table=table_name)
        aliases = field_identity_service.alias_history(table=table_name)
        return {
            "table": table_name,
            "fields": fields,
            "aliases": aliases,
            "field_count": len(fields),
            "alias_count": len(aliases),
        }

    def compute_evolution_stats(self, table_name: str) -> dict[str, Any]:
        """统计表 Schema 变更的演化趋势。

        汇总指定表的字段变更频率和类型分布，
        产出 Schema 稳定性评估数据。
        """
        changes = self.query_by_table(table_name)
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        compatible = 0
        incompatible = 0

        for c in changes:
            ctype = str(c.get("变更类型", "未知"))
            cstatus = str(c.get("status", "未知"))
            by_type[ctype] = by_type.get(ctype, 0) + 1
            by_status[cstatus] = by_status.get(cstatus, 0) + 1
            if c.get("兼容性评估") == "兼容":
                compatible += 1
            elif c.get("兼容性评估") == "不兼容":
                incompatible += 1

        return {
            "table": table_name,
            "total_changes": len(changes),
            "compatible_changes": compatible,
            "incompatible_changes": incompatible,
            "by_type": by_type,
            "by_status": by_status,
            "stability_score": round(compatible / max(len(changes), 1) * 100, 1),
            "snapshot_at": datetime.now().isoformat(),
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, msg = super().run_action(entry_id, action)
        if entry:
            entry["last_operated_at"] = datetime.now().isoformat()
        return entry, msg