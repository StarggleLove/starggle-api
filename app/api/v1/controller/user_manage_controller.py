"""用户管理 控制器（管理端，需要 JWT 鉴权）

- GET /user-manage/list            用户列表（含积分余额、等级、今日用量、Token 限额与状态）
- POST /user-manage/recharge-points 手动充值/扣减积分
- POST /user-manage/reset-call-count 手动重置用户今日 LLM 调用次数
- GET /user-manage/{user_id}/quota 查询用户配额策略与用量
- PUT /user-manage/{user_id}/quota 保存每日/每月 Token 上限与访问封禁
- GET /user-manage/orders          订单列表
"""
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.services import user_manage_service, user_quota_service
from app.utils.response import ResponseFactory
from app.utils.admin_auth import require_admin_user, require_permission
from app.utils.rbac import Perm

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin_user)])


class RechargePointsBody(BaseModel):
    """手动充值/扣减积分请求体（points 正数为充值、负数为扣减）"""
    user_id: str = Field(..., min_length=1, max_length=64, description="用户ID")
    points: int = Field(..., ge=-10_000_000, le=10_000_000, description="积分变动量（正充负扣）")
    remark: str | None = Field(default=None, max_length=255, description="备注")


class ResetCallCountBody(BaseModel):
    """重置今日调用次数请求体"""
    user_id: str = Field(..., min_length=1, max_length=64, description="用户ID")
    remark: str | None = Field(default=None, max_length=255, description="备注（仅日志留痕）")


class QuotaPolicyBody(BaseModel):
    """用户配额与访问策略请求体（PUT 全量替换；null 限额=不限，0=禁止）

    限额与状态均为必填，避免客户端漏传字段时静默清空限额或解除封禁。
    """
    daily_token_limit: int | None = Field(..., ge=0, le=9_000_000_000_000_000,
                                         description="每日Token上限；null=不限")
    monthly_token_limit: int | None = Field(..., ge=0, le=9_000_000_000_000_000,
                                           description="每月Token上限；null=不限")
    access_status: Literal["active", "blocked"] = Field(..., description="访问状态")
    remark: str | None = Field(default=None, max_length=255, description="备注")


def _quota_payload(user_id: str, policy, usage, exists: bool) -> dict:
    """序列化用户配额策略 + 用量快照"""
    return {
        "userId": user_id,
        "accessStatus": (policy.status if policy is not None else user_quota_service.STATUS_ACTIVE),
        "dailyTokenLimit": policy.daily_token_limit if policy is not None else None,
        "monthlyTokenLimit": policy.monthly_token_limit if policy is not None else None,
        "remark": policy.remark if policy is not None else None,
        "userExists": exists,
        "todayCalls": usage.today_calls,
        "todayTokens": usage.today_tokens,
        "monthCalls": usage.month_calls,
        "monthTokens": usage.month_tokens,
        "updatedAt": (
            policy.updated_at.isoformat()
            if policy is not None and policy.updated_at else None
        ),
    }


def _user_row(user, usage, policy) -> dict:
    """序列化单条用户记录（含 Token 用量、限额与访问状态）"""
    level = user_manage_service.effective_level_of(user)
    usage = usage or user_quota_service.UserUsageSnapshot()
    return {
        "userId": user.user_id,
        "userName": user.user_name,
        "email": user.email,
        "loginId": user.login_id,
        "pointBalance": int(user.point_balance or 0),
        "membershipLevel": level,
        "todayUsed": usage.today_calls,
        "todayTokens": usage.today_tokens,
        "monthTokens": usage.month_tokens,
        "accessStatus": (policy.status if policy is not None else user_quota_service.STATUS_ACTIVE),
        "dailyTokenLimit": policy.daily_token_limit if policy is not None else None,
        "monthlyTokenLimit": policy.monthly_token_limit if policy is not None else None,
        "quotaRemark": policy.remark if policy is not None else None,
        "createdAt": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("/list", dependencies=[Depends(require_permission(Perm.USERS_READ))])
async def list_users(
    page: int = 1,
    page_size: int = 20,
    keyword: str = "",
    db: AsyncSession = Depends(get_db),
):
    """用户列表（分页 + 关键字模糊搜索）"""
    page = max(page, 1)
    page_size = max(1, min(page_size, 100))

    users, total = await user_manage_service.list_users(db, keyword, page, page_size)
    user_ids = [u.user_id for u in users]
    usage_map = await user_quota_service.get_usage_snapshot_map(db, user_ids)
    policy_map = await user_quota_service.get_quota_policy_map(db, user_ids)

    data = [
        _user_row(u, usage_map.get(u.user_id), policy_map.get(u.user_id))
        for u in users
    ]
    return ResponseFactory.ok(data={"list": data, "total": total}, message="查询成功")


@router.post("/recharge-points", dependencies=[Depends(require_permission(Perm.USERS_WRITE))])
async def recharge_points(
    body: RechargePointsBody,
    db: AsyncSession = Depends(get_db),
):
    """管理员手动充值/扣减积分（创建一笔 paid 订单 + 更新余额）"""
    try:
        result = await user_manage_service.grant_points(
            db, body.user_id, body.points, body.remark
        )
    except ValueError as e:
        return ResponseFactory.bad_request(message=str(e))
    if result is None:
        return ResponseFactory.bad_request(message="用户不存在")

    order, user = result
    return ResponseFactory.ok(data={
        "orderNo": order.order_no,
        "userId": user.user_id,
        "points": body.points,
        "pointBalance": int(user.point_balance or 0),
    }, message="充值成功" if body.points > 0 else "扣减成功")


@router.post("/reset-call-count", dependencies=[Depends(require_permission(Perm.USERS_WRITE))])
async def reset_call_count(
    body: ResetCallCountBody,
    db: AsyncSession = Depends(get_db),
):
    """管理员手动重置用户今日 LLM 调用次数（call_count 归零）.

    token 计数（total_tokens）与 user_token_info 的用量明细完整保留，
    避免重置调用次数时意外恢复用户级 Token 额度.
    """
    updated = await user_manage_service.reset_today_call_count(db, body.user_id)
    if updated is None:
        return ResponseFactory.bad_request(message="用户不存在")
    snapshot = await user_quota_service.get_user_usage_snapshot(db, body.user_id)
    return ResponseFactory.ok(data={
        "userId": body.user_id,
        "updatedRows": updated,
        "todayUsed": snapshot.today_calls,
        "todayTokens": snapshot.today_tokens,
    }, message="已重置今日调用次数")


@router.get("/{user_id}/quota", dependencies=[Depends(require_permission(Perm.USERS_READ))])
async def get_user_quota(
    user_id: str = Path(..., min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
):
    """查询用户配额策略与当日/当月用量（无策略时按 active + 不限返回）"""
    policy = await user_quota_service.get_quota_policy(db, user_id)
    usage = await user_quota_service.get_user_usage_snapshot(db, user_id)
    exists = await user_manage_service.user_exists(db, user_id)
    return ResponseFactory.ok(
        data=_quota_payload(user_id, policy, usage, exists),
        message="查询成功",
    )


@router.put("/{user_id}/quota", dependencies=[Depends(require_permission(Perm.USERS_WRITE))])
async def save_user_quota(
    body: QuotaPolicyBody,
    user_id: str = Path(..., min_length=1, max_length=64),
    db: AsyncSession = Depends(get_db),
):
    """保存用户每日/每月 Token 上限与访问封禁（upsert，显式 null 表示不限）"""
    try:
        policy = await user_quota_service.upsert_quota_policy(
            db,
            user_id,
            daily_token_limit=body.daily_token_limit,
            monthly_token_limit=body.monthly_token_limit,
            access_status=body.access_status,
            remark=body.remark,
        )
    except ValueError as e:
        return ResponseFactory.bad_request(message=str(e))

    usage = await user_quota_service.get_user_usage_snapshot(db, user_id)
    exists = await user_manage_service.user_exists(db, user_id)
    return ResponseFactory.ok(
        data=_quota_payload(user_id, policy, usage, exists),
        message="保存成功",
    )


@router.get("/orders", dependencies=[Depends(require_permission(Perm.USERS_READ))])
async def list_orders(
    user_id: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """订单列表（可按用户ID/状态筛选）"""
    page = max(page, 1)
    page_size = max(1, min(page_size, 100))

    orders, total = await user_manage_service.list_orders(db, user_id, status, page, page_size)

    def _order_row(o):
        return {
            "orderNo": o.order_no,
            "userId": o.user_id,
            "plan": o.plan,
            "amountCents": o.amount_cents,
            "currency": o.currency,
            "status": o.status,
            "channel": o.channel,
            "providerOrderId": o.provider_order_id,
            "remark": o.remark,
            "createdAt": o.created_at.isoformat() if o.created_at else None,
        }

    return ResponseFactory.ok(data={"list": [_order_row(o) for o in orders], "total": total}, message="查询成功")
