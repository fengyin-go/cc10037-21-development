"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import business, field_identity, resource

# field_identity 的 /api/field-identities/... 路径比资源路由的 /api/{module}/{entry_id}
# 更具体，必须排在资源路由之前，否则会被动态段抢先匹配。
ROUTERS = [field_identity, resource, business]
