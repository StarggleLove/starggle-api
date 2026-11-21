"""管理端登录接口（复用 StarggleCom 用户表与 JWT 密钥）"""
import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from jose import jwt
import bcrypt

from app.config.database import get_db
from app.utils.admin_auth import (
    ALGORITHM,
    SECRET_KEY,
    require_admin_user,
    resolve_identity,
)
from app.utils.rbac import AdminIdentity
from app.utils.response import ResponseFactory

logger = logging.getLogger(__name__)

router = APIRouter()

ACCESS_TOKEN_EXPIRE_HOURS = 24 * 7  # 7 天


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=128)


# 账号不存在时也执行一次 bcrypt，避免通过响应耗时枚举已注册邮箱
_DUMMY_BCRYPT_HASH = "$2b$12$HKyaKyENJmOqaKgzj35so.yaznwbl9K4PVDfoMe3oJYWkPhM66z7q"


def _verify_password(password: str, hashed: str) -> bool:
    """验证 bcrypt 哈希密码"""
    pwd_bytes = password.encode("utf-8")[:72]
    try:
        return bcrypt.checkpw(pwd_bytes, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    管理端登录：邮箱 + 密码 → JWT

    与 StarggleCom /auth/verifyPassword 使用相同的用户表和 JWT 密钥，
    两边的 token 可以互通。只有 ADMIN_USER_IDS 白名单内的账号能登录管理台。
    """
    from app.models.user_info import UserInfo

    email = body.email.strip().replace("＠", "@")
    if not email or not body.password:
        return ResponseFactory.bad_request(message="邮箱和密码不能为空")

    result = await db.execute(select(UserInfo).where(UserInfo.email == email))
    user = result.scalar_one_or_none()

    if not user:
        _verify_password(body.password, _DUMMY_BCRYPT_HASH)
        return ResponseFactory.ok(data={"success": False, "message": "邮箱或密码错误"})

    if not _verify_password(body.password, user.password):
        return ResponseFactory.ok(data={"success": False, "message": "邮箱或密码错误"})

    identity = resolve_identity(user.user_id, user.email, user.user_name)
    if identity is None:
        logger.warning("非管理员账号尝试登录管理台: user_id=%s", user.user_id)
        return ResponseFactory.ok(
            data={"success": False, "message": "该账号无管理台访问权限"}
        )

    # 签发 JWT（payload 格式与 StarggleCom 一致；role 仅供展示，不参与鉴权）
    now = datetime.utcnow()
    token = jwt.encode(
        {
            "user_id": user.user_id,
            "email": user.email,
            "user_name": user.user_name,
            "role": identity.role.value,
            "iat": now,
            "exp": now + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )

    logger.info("管理台登录成功: user_id=%s role=%s", user.user_id, identity.role.value)
    return ResponseFactory.ok(
        data={
            "success": True,
            "message": "登录成功",
            "token": token,
            "user": identity.to_public_dict(),
        }
    )


@router.get("/me")
async def me(identity: AdminIdentity = Depends(require_admin_user)):
    """返回当前登录管理员的身份、角色与权限（角色按 env 实时解析）"""
    return ResponseFactory.ok(data=identity.to_public_dict(), message="查询成功")
