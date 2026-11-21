"""渠道健康与路由配置管理控制器"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.models.model_manage_info import ModelManageInfo
from app.schemas.health_manage import RoutingConfigUpdate
from app.services import health_manage_service
from app.utils.admin_auth import require_admin_user, require_permission
from app.utils.rbac import Perm
from app.utils.response import ResponseFactory

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin_user)])


@router.get("/overview", dependencies=[Depends(require_permission(Perm.HEALTH_READ))])
async def health_overview(db: AsyncSession = Depends(get_db)):
    """渠道健康看板：集群汇总 + 渠道明细 + 路由配置"""
    data = await health_manage_service.get_health_overview(db)
    return ResponseFactory.ok(data=data, message="查询成功")


@router.get("/routing", dependencies=[Depends(require_permission(Perm.HEALTH_READ))])
async def list_routing(db: AsyncSession = Depends(get_db)):
    """async1..async9 路由配置"""
    data = await health_manage_service.list_routing_configs(db)
    return ResponseFactory.ok(data={"list": data, "total": len(data)}, message="查询成功")


@router.put("/routing/{async_type}", dependencies=[Depends(require_permission(Perm.HEALTH_WRITE))])
async def update_routing(
        async_type: str,
        data: RoutingConfigUpdate,
        db: AsyncSession = Depends(get_db),
):
    """更新集群路由策略"""
    try:
        config = await health_manage_service.update_routing_config(db, async_type, data)
    except ValueError as e:
        return ResponseFactory.bad_request(message=str(e))
    return ResponseFactory.ok(data=config.to_dict(), message="更新成功")


@router.post("/channels/{model_id}/reset", dependencies=[Depends(require_permission(Perm.HEALTH_WRITE))])
async def reset_channel(model_id: str, db: AsyncSession = Depends(get_db)):
    """手动重置渠道熔断状态"""
    model = await db.get(ModelManageInfo, model_id)
    if model is None:
        return ResponseFactory.not_found(message="模型不存在")
    await health_manage_service.reset_channel(db, model_id)
    return ResponseFactory.ok(message="已重置")
