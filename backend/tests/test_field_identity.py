"""字段身份与别名有效期的领域回归测试。

只依赖标准库，直接调用领域服务，不启动 HTTP 服务：
    cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services.field_identity import FieldIdentityError, FieldIdentityService


class FieldIdentityTestBase(unittest.TestCase):
    """每个用例独立实例，且用独立的表名，避免与全局服务的登记冲突。"""

    ORDER_TABLE = "ods.ods_order_info"
    REGION_TABLE = "ods.ods_region_info"

    def setUp(self) -> None:
        self.service = FieldIdentityService()
        self.service.register_field(
            table=self.ORDER_TABLE, column="order_code",
            effective_from="2026-01-01",
        )
        self.service.register_field(
            table=self.REGION_TABLE, column="region_code",
            effective_from="2026-01-01",
        )


class IdentityQueryTest(FieldIdentityTestBase):
    def test_scoped_query_resolves_table_and_column(self) -> None:
        self.service.register_alias(
            table="ods.ods_order_info", alias="order_no", column="order_code",
            effective_from="2026-01-01", effective_to="2026-06-30",
        )
        result = self.service.query_identity(
            table="ods.ods_order_info", name="order_no", effective_date="2026-03-01",
        )
        self.assertTrue(result["resolved"])
        self.assertEqual(result["production_table"], "ods.ods_order_info")
        self.assertEqual(result["production_column"], "order_code")
        self.assertTrue(result["read_paths"])

    def test_same_alias_without_table_is_ambiguous(self) -> None:
        self.service.register_alias(
            table="ods.ods_order_info", alias="code", column="order_code",
            effective_from="2026-01-01",
        )
        self.service.register_alias(
            table="ods.ods_region_info", alias="code", column="region_code",
            effective_from="2026-01-01",
        )
        result = self.service.query_identity(name="code", effective_date="2026-03-01")
        self.assertFalse(result["resolved"])
        self.assertTrue(result["ambiguous"])
        self.assertIsNone(result["production_table"])
        self.assertEqual(len(result["candidates"]), 2)

    def test_table_without_matching_period_reports_periods(self) -> None:
        result = self.service.query_identity(
            table="ods.ods_order_info", name="order_code",
            effective_date="2025-06-01",
        )
        self.assertFalse(result["resolved"])
        self.assertIn("有效期为", result["message"])

    def test_table_scoping_beats_cross_table_candidate(self) -> None:
        self.service.register_field(
            table="ods.ods_other", column="code", effective_from="2026-01-01",
        )
        result = self.service.query_identity(
            table="ods.ods_order_info", name="order_code", effective_date="2026-03-01",
        )
        self.assertTrue(result["resolved"])
        self.assertEqual(result["production_table"], "ods.ods_order_info")


class AliasPeriodTest(FieldIdentityTestBase):
    def test_alias_cannot_overwrite_effective_history(self) -> None:
        self.service.register_alias(
            table="ods.ods_order_info", alias="order_no", column="order_code",
            effective_from="2026-01-01", effective_to="2026-06-30",
        )
        with self.assertRaises(FieldIdentityError):
            self.service.register_alias(
                table="ods.ods_order_info", alias="order_no", column="order_code",
                effective_from="2026-02-01", effective_to="2026-05-01",
            )

    def test_alias_period_must_fit_column_period(self) -> None:
        self.service.register_field(
            table="ods.ods_short", column="c",
            effective_from="2026-01-01", effective_to="2026-03-01",
        )
        with self.assertRaises(FieldIdentityError):
            self.service.register_alias(
                table="ods.ods_short", alias="c_old", column="c",
                effective_from="2026-01-01", effective_to="2026-12-31",
            )

    def test_revision_must_start_after_original(self) -> None:
        self.service.register_alias(
            table="ods.ods_order_info", alias="order_no", column="order_code",
            effective_from="2026-01-01",
        )
        alias_id = self.service.alias_history(
            table="ods.ods_order_info", alias="order_no",
        )[0]["id"]
        with self.assertRaises(FieldIdentityError):
            self.service.revise_alias(alias_id, effective_from="2026-01-01")


class RevisionHistoryTest(FieldIdentityTestBase):
    def test_revision_keeps_history_and_switches_new_alias(self) -> None:
        self.service.register_field(
            table="ods.ods_order_info", column="order_code_v2",
            effective_from="2026-07-01",
        )
        self.service.register_alias(
            table="ods.ods_order_info", alias="order_no", column="order_code",
            effective_from="2026-01-01", effective_to="2026-06-30",
        )
        alias_id = self.service.alias_history(
            table="ods.ods_order_info", alias="order_no",
        )[0]["id"]
        self.service.revise_alias(
            alias_id, effective_from="2026-07-01", column="order_code_v2",
        )

        old = self.service.history_query(
            table="ods.ods_order_info", name="order_no", effective_date="2026-03-01",
        )
        self.assertEqual(old["production_column"], "order_code")
        new = self.service.history_query(
            table="ods.ods_order_info", name="order_no", effective_date="2026-09-01",
        )
        self.assertEqual(new["production_column"], "order_code_v2")

        history = self.service.alias_history(table="ods.ods_order_info", alias="order_no")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["effective_from"], "2026-01-01")
        self.assertEqual(history[1]["revision"], 2)

    def test_revision_records_recalculation_and_write_results(self) -> None:
        self.service.register_field(
            table="ods.ods_order_info", column="order_code_v2",
            effective_from="2026-07-01",
        )
        self.service.register_alias(
            table="ods.ods_order_info", alias="order_no", column="order_code",
            effective_from="2026-01-01",
        )
        alias_id = self.service.alias_history(
            table="ods.ods_order_info", alias="order_no",
        )[0]["id"]
        result = self.service.revise_alias(
            alias_id, effective_from="2026-07-01", column="order_code_v2",
        )
        recalculation = result["recalculation"]
        self.assertTrue(recalculation["history_snapshot"]["preserved"])
        self.assertEqual(recalculation["history_snapshot"]["column"], "order_code")
        self.assertEqual(self.service.recalculations()[-1]["id"], recalculation["id"])


class ImpactEntryTest(FieldIdentityTestBase):
    def test_verified_entry_is_submittable(self) -> None:
        batch = self.service.generate_impact_entries(
            table="ods.ods_order_info", column="order_code",
            effective_date="2026-03-01",
        )
        self.assertGreaterEqual(batch["submittable_count"], 1)
        self.assertTrue(all(
            entry["production_table"] == "ods.ods_order_info"
            for entry in batch["entries"]
        ))

    def test_missing_table_blocks_entry_and_confirmation(self) -> None:
        batch = self.service.generate_impact_entries(
            column="order_code", effective_date="2026-03-01",
        )
        self.assertEqual(batch["submittable_count"], 0)
        for entry in batch["entries"]:
            self.assertIn("表", entry["缺失证据"])
            with self.assertRaises(FieldIdentityError):
                self.service.confirm_entry(entry["id"])

    def test_missing_date_blocks_entry(self) -> None:
        batch = self.service.generate_impact_entries(
            table="ods.ods_order_info", column="order_code",
        )
        self.assertEqual(batch["submittable_count"], 0)
        self.assertIn("生效日期", batch["entries"][0]["缺失证据"])

    def test_only_confirmed_entries_can_drill(self) -> None:
        batch = self.service.generate_impact_entries(
            table="ods.ods_order_info", column="order_code",
            effective_date="2026-03-01",
        )
        entry_id = batch["entries"][0]["id"]
        with self.assertRaises(FieldIdentityError):
            self.service.run_drill([entry_id])

        self.service.confirm_entry(entry_id)
        candidates = self.service.candidate_columns()
        self.assertEqual(candidates["count"], 1)
        drill = self.service.run_drill([entry_id])
        self.assertEqual(drill["count"], 1)

    def test_entry_from_change_uses_change_table_and_date(self) -> None:
        batch = self.service.generate_from_change(
            1, columns=["payment_channel"],
        )
        self.assertEqual(batch["change"]["change_id"], 1)
        self.assertTrue(batch["query"]["effective_date"])


if __name__ == "__main__":
    unittest.main()
