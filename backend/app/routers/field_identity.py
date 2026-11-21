"""字段身份与别名有效期接口：登记、按表/生效日期检索、影响单和候选列演练。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from app.services.field_identity import (
    FieldIdentityError,
    field_identity_service as service,
)

router = APIRouter(prefix="/api/field-identities", tags=["字段身份"])


def _fail(exc: FieldIdentityError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/fields")
def register_field(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """登记字段身份：表 + 列 + 生效期。"""
    try:
        record = service.register_field(
            table=body.get("table", ""),
            column=body.get("column", ""),
            effective_from=body.get("effective_from", ""),
            effective_to=body.get("effective_to"),
            data_type=body.get("data_type", ""),
            note=body.get("note", ""),
        )
    except FieldIdentityError as exc:
        raise _fail(exc)
    return {"ok": True, "field": record, "message": "字段身份已登记"}


@router.post("/aliases")
def register_alias(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """登记旧别名及其有效期，必须落在同表目标列的生效期内。"""
    try:
        record = service.register_alias(
            table=body.get("table", ""),
            alias=body.get("alias", ""),
            column=body.get("column", ""),
            effective_from=body.get("effective_from", ""),
            effective_to=body.get("effective_to"),
            note=body.get("note", ""),
        )
    except FieldIdentityError as exc:
        raise _fail(exc)
    return {"ok": True, "alias": record, "message": "别名已登记"}


@router.post("/aliases/{alias_id}/revisions")
def revise_alias(alias_id: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """修订别名口径：旧记录只关闭，不覆盖；同时重算血缘与写入结果。"""
    try:
        return service.revise_alias(
            alias_id,
            effective_from=body.get("effective_from", ""),
            column=body.get("column"),
            alias=body.get("alias"),
            note=body.get("note", ""),
        )
    except FieldIdentityError as exc:
        raise _fail(exc)


@router.get("/aliases/history")
def alias_history(
    table: str | None = Query(default=None),
    alias: str | None = Query(default=None),
) -> dict[str, Any]:
    """别名修订历史：旧日期记录保留，便于回查。"""
    rows = service.alias_history(table=table, alias=alias)
    return {"items": rows, "total": len(rows)}


@router.get("/resolve")
def resolve_identity(
    table: str | None = Query(default=None),
    name: str | None = Query(default=None),
    effective_date: str = Query(...),
) -> dict[str, Any]:
    """按表和生效日期查询字段身份；无表名时遇到同名候选不猜来源。"""
    try:
        return service.query_identity(
            table=table, effective_date=effective_date, name=name,
        )
    except FieldIdentityError as exc:
        raise _fail(exc)


@router.get("/history-query")
def history_query(
    table: str | None = Query(default=None),
    name: str | None = Query(default=None),
    effective_date: str = Query(...),
) -> dict[str, Any]:
    """别名修订后用历史日期回查，返回当时能确认的生产表与列。"""
    try:
        return service.history_query(
            table=table, name=name, effective_date=effective_date,
        )
    except FieldIdentityError as exc:
        raise _fail(exc)


@router.post("/impact-entries")
def generate_impact_entries(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """用字段影响数据生成写入任务评估条目。"""
    try:
        return service.generate_impact_entries(
            table=body.get("table"),
            column=body.get("column") or body.get("field"),
            effective_date=body.get("effective_date"),
            change_type=body.get("change_type", "MODIFY_TYPE"),
            note=body.get("note", ""),
        )
    except FieldIdentityError as exc:
        raise _fail(exc)


@router.post("/impact-entries/from-change")
def generate_from_change(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """基于 Schema 变更记录生成影响单条目。"""
    try:
        return service.generate_from_change(
            int(body.get("change_id", 0)),
            column=body.get("column") or body.get("field"),
            columns=body.get("columns"),
            effective_date=body.get("effective_date"),
            note=body.get("note"),
        )
    except FieldIdentityError as exc:
        raise _fail(exc)


@router.get("/impact-entries")
def list_impact_entries(
    status: str | None = Query(default=None),
    table: str | None = Query(default=None),
) -> dict[str, Any]:
    """查看影响单：区分可提交条目与仍欠表/日期证据的条目。"""
    return service.list_entries(status=status, table=table)


@router.post("/impact-entries/{entry_id}/confirm")
def confirm_entry(entry_id: int) -> dict[str, Any]:
    """确认评估条目；证据不全的条目拒绝确认。"""
    try:
        return service.confirm_entry(entry_id)
    except FieldIdentityError as exc:
        raise _fail(exc)


@router.get("/candidate-columns")
def candidate_columns(
    table: str | None = Query(default=None),
    column: str | None = Query(default=None),
) -> dict[str, Any]:
    """候选列演练输入：只返回已确认条目。"""
    return service.candidate_columns(table=table, column=column)


@router.post("/drill")
def run_drill(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """用已确认条目执行候选列演练。"""
    try:
        return service.run_drill([int(item) for item in body.get("entry_ids", [])])
    except FieldIdentityError as exc:
        raise _fail(exc)


@router.get("/recalculations")
def list_recalculations() -> dict[str, Any]:
    """别名口径变更后的血缘重算与写入结果留痕。"""
    rows = service.recalculations()
    return {"items": rows, "total": len(rows)}


@router.get("/snapshot")
def snapshot() -> dict[str, Any]:
    return service.snapshot()
