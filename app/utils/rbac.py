"""管理端角色与权限矩阵。

四个内置角色（权限从大到小）：

- ``admin``（管理员）：全部权限，含删除提供商、查看上游 API Key
- ``operator``（运维）：日常配置模型 / 提供商 / 项目 / 渠道 / 路由，不可删除提供商
- ``developer``（开发者）：只读 + 项目 API Key 的签发、轮换、吊销
- ``viewer``（只读）：查看用量、调用记录、模型与渠道状态

权限使用 ``资源:动作`` 字符串表示，后端按接口逐项校验；前端按钮显隐只是
体验优化，真正的安全边界在服务端。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    """管理端角色"""

    ADMIN = "admin"
    OPERATOR = "operator"
    DEVELOPER = "developer"
    VIEWER = "viewer"


ROLE_LABELS: dict[Role, str] = {
    Role.ADMIN: "管理员",
    Role.OPERATOR: "运维",
    Role.DEVELOPER: "开发者",
    Role.VIEWER: "只读",
}


class Perm:
    """权限标识常量（``资源:动作``）"""

    USAGE_READ = "usage:read"
    CALLS_READ = "calls:read"

    PROVIDERS_READ = "providers:read"
    PROVIDERS_WRITE = "providers:write"
    PROVIDERS_DELETE = "providers:delete"

    MODELS_READ = "models:read"
    MODELS_WRITE = "models:write"
    MODELS_DELETE = "models:delete"

    PROJECTS_READ = "projects:read"
    PROJECTS_WRITE = "projects:write"
    TOKENS_WRITE = "tokens:write"

    HEALTH_READ = "health:read"
    HEALTH_WRITE = "health:write"

    # 查看上游 API Key / 密钥类字段；其余角色只能看到掩码或“已隐藏”
    SECRETS_READ = "secrets:read"


# 所有角色都有的只读权限
_READ_ONLY: frozenset[str] = frozenset({
    Perm.USAGE_READ,
    Perm.CALLS_READ,
    Perm.PROVIDERS_READ,
    Perm.MODELS_READ,
    Perm.PROJECTS_READ,
    Perm.HEALTH_READ,
})

_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.ADMIN: _READ_ONLY | {
        Perm.PROVIDERS_WRITE,
        Perm.PROVIDERS_DELETE,
        Perm.MODELS_WRITE,
        Perm.MODELS_DELETE,
        Perm.PROJECTS_WRITE,
        Perm.TOKENS_WRITE,
        Perm.HEALTH_WRITE,
        Perm.SECRETS_READ,
    },
    Role.OPERATOR: _READ_ONLY | {
        Perm.PROVIDERS_WRITE,
        Perm.MODELS_WRITE,
        Perm.MODELS_DELETE,
        Perm.PROJECTS_WRITE,
        Perm.TOKENS_WRITE,
        Perm.HEALTH_WRITE,
        Perm.SECRETS_READ,
    },
    Role.DEVELOPER: _READ_ONLY | {Perm.TOKENS_WRITE},
    Role.VIEWER: _READ_ONLY,
}


def parse_role(raw: str | None) -> Role | None:
    """把配置/Token 中的角色字符串解析为 Role；非法值返回 None"""
    if raw is None:
        return None
    try:
        return Role(str(raw).strip().lower())
    except ValueError:
        return None


def permissions_for(role: Role | str) -> frozenset[str]:
    """返回角色的权限集合；未知角色按只读处理"""
    parsed = role if isinstance(role, Role) else parse_role(role)
    return _PERMISSIONS.get(parsed, _READ_ONLY) if parsed else _READ_ONLY


def has_permission(role: Role | str, permission: str) -> bool:
    return permission in permissions_for(role)


@dataclass(frozen=True)
class AdminIdentity:
    """当前登录的管理员身份（由 JWT + 账号表解析而来）"""

    username: str
    role: Role

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role.value)

    @property
    def permissions(self) -> frozenset[str]:
        return permissions_for(self.role)

    def to_public_dict(self) -> dict:
        """给前端 / 登录响应使用的身份信息（不含任何密钥）"""
        return {
            "userId": self.username,
            "userName": self.username,
            "role": self.role.value,
            "roleLabel": self.role_label,
            "permissions": sorted(self.permissions),
        }
