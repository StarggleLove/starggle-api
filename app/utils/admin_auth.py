"""JWT 鉴权依赖与角色解析（与 StarggleCom 共享密钥和用户表）

管理类 API 通过 Authorization: Bearer <jwt> 校验身份。
JWT 由登录接口 /api/v1/auth/login 签发，或复用 StarggleCom 已登录的 token。

角色配置（环境变量，启动时读取，改动后需重启）：

- ``ADMIN_USER_IDS``：管理台访问白名单（逗号分隔的 user_id）。不在白名单内的
  账号即使登录成功也无法访问管理接口（fail-closed）。
- ``ADMIN_ROLES``：角色细化，JSON 对象：:

      ADMIN_ROLES={"u_1001": "operator", "ops@example.com": "viewer",
                   "u_1002": {"role": "developer"}}

  键含 ``@`` 按邮箱匹配（不区分大小写），否则按 user_id 精确匹配。白名单内
  但未配置角色的账号默认 ``admin``。非法 JSON 会让服务启动失败；非法角色值
  记错误日志并按 ``viewer`` 处理。

角色在每次请求时按当前配置重新解析，JWT 里的 ``role`` 仅供展示，不作为
权限依据。
"""
import json
import logging
import os

from fastapi import HTTPException, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from app.utils.rbac import (
    ROLE_LABELS,
    AdminIdentity,
    Role,
    has_permission,
    parse_role,
)

logger = logging.getLogger(__name__)

# 与 StarggleCom 共享同一个 JWT 密钥。
# 注意：未设置 JWT_SECRET_KEY 时会使用代码内默认值（仅限本地开发），
# 生产环境必须通过环境变量注入随机密钥，否则任何拿到源码的人可伪造 token。
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "stargglechat_jwt_secret_2026")
if not os.getenv("JWT_SECRET_KEY"):
    logger.critical(
        "未设置 JWT_SECRET_KEY，正在使用代码内默认密钥！"
        "生产环境任何用户可伪造 token，请立即通过环境变量注入随机密钥。"
    )
ALGORITHM = "HS256"

# 管理员白名单（逗号分隔的 user_id）。为空时拒绝所有管理请求（fail-closed）。
ADMIN_USER_IDS = {
    uid.strip() for uid in os.getenv("ADMIN_USER_IDS", "").split(",") if uid.strip()
}
if not ADMIN_USER_IDS:
    logger.critical(
        "未设置 ADMIN_USER_IDS，管理端接口将拒绝所有登录用户（fail-closed）！"
        "生产环境请设置管理员用户ID白名单（逗号分隔）。"
    )

ADMIN_ROLES_JSON = os.getenv("ADMIN_ROLES", "")
DEFAULT_ADMIN_ROLE = Role.ADMIN


def build_role_maps(raw: str) -> tuple[dict[str, Role], dict[str, Role]]:
    """解析 ADMIN_ROLES，返回 (user_id 角色表, email 角色表)。

    JSON 非法或不是对象时抛 ValueError，让服务启动失败而不是静默退回默认管理员。
    """
    if not raw or not raw.strip():
        return {}, {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"ADMIN_ROLES 不是合法 JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("ADMIN_ROLES 必须是 {user_id或邮箱: 角色} 对象")

    by_user_id: dict[str, Role] = {}
    by_email: dict[str, Role] = {}
    for key, spec in data.items():
        account = str(key).strip()
        if not account:
            continue

        role_raw = spec.get("role") if isinstance(spec, dict) else spec
        role = parse_role(role_raw) if role_raw is not None else None
        if role is None:
            logger.error(
                "ADMIN_ROLES 中 %r 的角色 %r 无效，按只读角色处理", account, role_raw,
            )
            role = Role.VIEWER

        if "@" in account:
            by_email[account.lower()] = role
        else:
            by_user_id[account] = role

    return by_user_id, by_email


ROLE_BY_USER_ID, ROLE_BY_EMAIL = build_role_maps(ADMIN_ROLES_JSON)


def _log_effective_roles() -> None:
    """启动时打印生效的账号→角色，便于审计配置错误"""
    for uid in sorted(ADMIN_USER_IDS):
        role = ROLE_BY_USER_ID.get(uid, DEFAULT_ADMIN_ROLE)
        logger.info(
            "管理台账号 user_id=%s 角色=%s",
            uid, ROLE_LABELS.get(role, role.value),
        )
    for uid in ROLE_BY_USER_ID:
        if uid not in ADMIN_USER_IDS:
            logger.warning(
                "ADMIN_ROLES 中的 user_id=%s 不在 ADMIN_USER_IDS 白名单内，已忽略",
                uid,
            )
    if ROLE_BY_EMAIL:
        logger.warning(
            "ADMIN_ROLES 使用了邮箱键；账号邮箱变更后可能退回默认管理员，建议优先用 user_id 键",
        )


_log_effective_roles()

security = HTTPBearer(auto_error=False)


def resolve_identity(
    user_id: str,
    email: str | None = None,
    user_name: str | None = None,
) -> AdminIdentity | None:
    """按白名单与 ADMIN_ROLES 解析请求身份；不在白名单返回 None。

    角色优先级：user_id 配置 > 邮箱配置 > 默认管理员。
    """
    if user_id not in ADMIN_USER_IDS:
        return None

    role = ROLE_BY_USER_ID.get(user_id)
    if role is None and email:
        role = ROLE_BY_EMAIL.get(email.strip().lower())
    if role is None:
        role = DEFAULT_ADMIN_ROLE

    return AdminIdentity(
        username=user_name or user_id,
        role=role,
        user_id=user_id,
        user_name=user_name,
        email=email,
    )


def decode_token(token: str) -> dict | None:
    """解码 JWT token，失败返回 None"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError as e:
        logger.warning(f"JWT 解码失败: {e}")
        return None


def verify_admin_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> AdminIdentity:
    """校验 Bearer JWT 和白名单，通过则返回当前管理员身份。

    用法：
        @router.get("/list")
        async def list_models(identity: AdminIdentity = Depends(verify_admin_token)):
            ...
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="未提供认证 Token，请先登录")

    payload = decode_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")

    user_id = payload.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token 缺少用户标识")

    identity = resolve_identity(
        user_id,
        email=payload.get("email"),
        user_name=payload.get("user_name"),
    )
    if identity is None:
        logger.warning("非管理员访问管理端点被拒绝: user_id=%s", user_id)
        raise HTTPException(status_code=403, detail="无管理员权限")
    return identity


async def require_admin_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> AdminIdentity:
    """管理端点专用依赖：校验 JWT 并确认账号仍在 ADMIN_USER_IDS 白名单内。"""
    return verify_admin_token(request, credentials)


def require_permission(permission: str):
    """生成“需要指定权限”的依赖，无权限时返回 403 并提示当前角色。"""

    async def checker(
        identity: AdminIdentity = Depends(require_admin_user),
    ) -> AdminIdentity:
        if not has_permission(identity.role, permission):
            label = ROLE_LABELS.get(identity.role, identity.role.value)
            raise HTTPException(
                status_code=403,
                detail=f"当前角色（{label}）无权执行该操作",
            )
        return identity

    checker.__name__ = "require_permission_" + permission.replace(":", "_")
    return checker
