"""血缘字段检索与 Schema 影响分析的回归测试。

重点覆盖：按传递字段的字符串找路径时不能前缀误命中，按表+生效日期
查询字段身份后，影响分析必须用核实过的生产列。
"""
from __future__ import annotations

import unittest

from app.services.field_identity import field_identity_service
from app.services.lineage_graph import LineageGraphService
from app.services.schema_evolution import SchemaEvolutionService


class LineageFieldTraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = LineageGraphService()

    def test_exact_field_match_does_not_prefix_hit(self) -> None:
        exact = self.service.trace_field_lineage("order_code")
        prefixed = self.service.trace_field_lineage("order")
        self.assertGreater(exact["edge_count"], 0)
        self.assertEqual(prefixed["edge_count"], 0)

    def test_trace_returns_read_edge_for_order_table(self) -> None:
        result = self.service.trace_field_lineage("order_code")
        pairs = {(edge["source"], edge["target"]) for edge in result["lineage_chain"]}
        self.assertIn(("ods.ods_order_info", "dw.dwd_order_wide"), pairs)


class SchemaImpactTest(unittest.TestCase):
    """用独立表名隔离全局服务，避免和其他用例的有效期冲突。"""

    TABLE = "ods.ods_order_info_impact"

    @classmethod
    def setUpClass(cls) -> None:
        cls.service = SchemaEvolutionService()
        field_identity_service.register_field(
            table=cls.TABLE, column="order_code",
            effective_from="2026-01-01",
        )
        field_identity_service.register_field(
            table=cls.TABLE, column="order_code_v2",
            effective_from="2026-07-01",
        )
        field_identity_service.register_alias(
            table=cls.TABLE, alias="order_no", column="order_code",
            effective_from="2026-01-01", effective_to="2026-06-30",
        )
        field_identity_service.register_alias(
            table=cls.TABLE, alias="order_no", column="order_code_v2",
            effective_from="2026-07-01",
        )

    def test_impact_uses_historical_production_column(self) -> None:
        result = self.service.analyze_field_impact(
            self.TABLE, "order_no", "2026-03-01",
        )
        self.assertEqual(result["field"], "order_code")
        self.assertEqual(result["table"], self.TABLE)

    def test_impact_uses_current_production_column(self) -> None:
        result = self.service.analyze_field_impact(
            self.TABLE, "order_no", "2026-09-01",
        )
        self.assertEqual(result["field"], "order_code_v2")

    def test_unresolved_identity_does_not_guess(self) -> None:
        result = self.service.analyze_field_impact(
            self.TABLE, "unknown_field", "2026-09-01",
        )
        self.assertFalse(result["resolved"])
        self.assertEqual(result["affected_downstream_nodes"], [])
        self.assertIn("未核实", result["risk_summary"])


if __name__ == "__main__":
    unittest.main()
