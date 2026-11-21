"""管理角色 / 权限矩阵 / 接口鉴权测试"""
import json
import unittest
from unittest.mock import patch

import bcrypt
from jose import jwt

from tests_business.base import BusinessGatewayTestCase, seed_user
from app.schemas.project_manage import ProjectCreate
from app.schemas.llm_provider import ProviderCreate
from app.schemas.model_manage import ModelManageCreate
from app.services import (
    llm_provider_service,
    model_manage_service,
    project_manage_service,
)
from app.utils import admin_auth
from app.utils.admin_auth import ALGORITHM, SECRET_KEY, require_admin_user
from app.utils.rbac import Perm, Role, has_permission, permissions_for
from main import app


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=4),
    ).decode("utf-8")


class PermissionMatrixTest(unittest.TestCase):
    """纯权限矩阵 / 角色配置解析测试：不依赖数据库与 HTTP"""

    def test_admin_has_every_permission(self):
        perms = permissions_for(Role.ADMIN)
        for permission in (
            Perm.PROVIDERS_DELETE, Perm.MODELS_WRITE, Perm.PROJECTS_WRITE,
            Perm.TOKENS_WRITE, Perm.USERS_READ, Perm.USERS_WRITE,
            Perm.HEALTH_WRITE, Perm.SECRETS_READ,
        ):
            self.assertIn(permission, perms)

    def test_operator_cannot_delete_provider_or_manage_users(self):
        self.assertTrue(has_permission(Role.OPERATOR, Perm.PROVIDERS_WRITE))
        self.assertTrue(has_permission(Role.OPERATOR, Perm.SECRETS_READ))
        self.assertTrue(has_permission(Role.OPERATOR, Perm.USERS_READ))
        self.assertFalse(has_permission(Role.OPERATOR, Perm.PROVIDERS_DELETE))
        self.assertFalse(has_permission(Role.OPERATOR, Perm.USERS_WRITE))

    def test_developer_issues_tokens_only(self):
        perms = permissions_for(Role.DEVELOPER)
        self.assertIn(Perm.TOKENS_WRITE, perms)
        self.assertIn(Perm.USAGE_READ, perms)
        self.assertNotIn(Perm.PROJECTS_WRITE, perms)
        self.assertNotIn(Perm.MODELS_WRITE, perms)
        self.assertNotIn(Perm.USERS_READ, perms)
        self.assertNotIn(Perm.HEALTH_WRITE, perms)
        self.assertNotIn(Perm.SECRETS_READ, perms)

    def test_viewer_is_read_only(self):
        perms = permissions_for(Role.VIEWER)
        self.assertIn(Perm.USAGE_READ, perms)
        self.assertFalse(any(p.endswith(":write") or p.endswith(":delete") for p in perms))
        self.assertNotIn(Perm.SECRETS_READ, perms)
        self.assertNotIn(Perm.USERS_READ, perms)

    def test_build_role_maps(self):
        by_user, by_email = admin_auth.build_role_maps(json.dumps({
            "u1": "admin",
            "ops@example.com": "Operator",
            "u2": {"role": "developer"},
            "u3": {"role": "divine"},
        }))
        self.assertEqual(by_user["u1"], Role.ADMIN)
        self.assertEqual(by_user["u2"], Role.DEVELOPER)
        self.assertEqual(by_user["u3"], Role.VIEWER)  # 非法角色按最小权限
        self.assertEqual(by_email["ops@example.com"], Role.OPERATOR)

    def test_build_role_maps_rejects_invalid_json(self):
        # 配置写错时启动失败，避免静默退回默认管理员
        with self.assertRaises(ValueError):
            admin_auth.build_role_maps("{not json")
        with self.assertRaises(ValueError):
            admin_auth.build_role_maps('["not", "an", "object"]')


class RbacApiTest(BusinessGatewayTestCase):
    """管理端接口按角色放行 / 拒绝"""

    WHITELIST = frozenset({
        "u-admin", "u-op", "u-dev", "u-view", "u-mail", "u-legacy", "u-cn",
    })

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # 本用例验证真实 JWT + 白名单，移除基类的管理员依赖覆盖
        app.dependency_overrides.pop(require_admin_user, None)

        self._patches = [
            patch.object(admin_auth, "ADMIN_USER_IDS", self.WHITELIST),
            patch.object(admin_auth, "ROLE_BY_USER_ID", {
                "u-admin": Role.ADMIN,
                "u-op": Role.OPERATOR,
                "u-dev": Role.DEVELOPER,
                "u-view": Role.VIEWER,
                "u-cn": Role.VIEWER,
            }),
            patch.object(admin_auth, "ROLE_BY_EMAIL", {
                "mail@example.com": Role.OPERATOR,
            }),
        ]
        for p in self._patches:
            p.start()

        for uid, pwd, email in (
            ("u-admin", "adminpass", None),
            ("u-op", "oppass", None),
            ("u-dev", "devpass", None),
            ("u-view", "viewpass", None),
            ("u-mail", "mailpass", "mail@example.com"),
            ("u-legacy", "legacypass", None),
            ("u-guest", "guestpass", None),
            ("u-cn", "密码123", None),
        ):
            await seed_user(
                self.Session, uid, password=_hash_password(pwd), email=email,
            )

        async with self.Session() as db:
            project = await project_manage_service.create_project(
                db, ProjectCreate(project_name="rbac-project"),
            )
            self.project_id = project.project_id
            provider = await llm_provider_service.create_provider(
                db,
                ProviderCreate(
                    provider_name="mock-provider",
                    api_url="https://mock.example/v1",
                    api_key="sk-upstream-secret",
                ),
            )
            self.provider_id = provider.provider_id
            model = await model_manage_service.create_model(
                db,
                ModelManageCreate(
                    ai_model_name="mock-model",
                    provider_id=provider.provider_id,
                    is_using="1",
                ),
            )
            self.model_id = model.ai_model_id

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        await super().asyncTearDown()

    @staticmethod
    def _token(user_id: str, email: str | None = None, role: str | None = None) -> str:
        payload = {
            "user_id": user_id,
            "user_name": user_id,
            "email": email or f"{user_id}@example.com",
        }
        if role:
            payload["role"] = role
        return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    def _headers(self, user_id: str, email: str | None = None) -> dict:
        return {"Authorization": f"Bearer {self._token(user_id, email=email)}"}

    async def _login(self, email: str, password: str) -> dict:
        resp = await self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": password},
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()["data"]

    async def test_login_returns_role_and_permissions(self):
        data = await self._login("u-dev@example.com", "devpass")
        self.assertTrue(data["success"])
        self.assertEqual(data["user"]["role"], "developer")
        self.assertEqual(data["user"]["roleLabel"], "开发者")
        self.assertIn(Perm.TOKENS_WRITE, data["user"]["permissions"])
        self.assertNotIn(Perm.MODELS_WRITE, data["user"]["permissions"])

        payload = jwt.decode(data["token"], SECRET_KEY, algorithms=[ALGORITHM])
        self.assertEqual(payload["role"], "developer")

    async def test_login_rejects_wrong_password(self):
        data = await self._login("u-dev@example.com", "wrong")
        self.assertFalse(data["success"])

    async def test_login_rejects_non_whitelisted_account(self):
        # 账号密码正确但不在 ADMIN_USER_IDS 白名单内
        data = await self._login("u-guest@example.com", "guestpass")
        self.assertFalse(data["success"])
        self.assertIn("管理台", data["message"])

    async def test_login_handles_non_ascii_password(self):
        data = await self._login("u-cn@example.com", "密码123")
        self.assertTrue(data["success"])
        self.assertEqual(data["user"]["role"], "viewer")

    async def test_jwt_role_claim_is_not_trusted(self):
        # 白名单内的 viewer 即使 token 里写 role=admin，权限仍按 env 解析
        headers = {"Authorization": f"Bearer {self._token('u-view', role='admin')}"}
        resp = await self.client.get("/api/v1/auth/me", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["role"], "viewer")

        resp = await self.client.post(
            "/api/v1/model-manage/providers",
            headers=headers,
            json={
                "provider_name": "spoofed",
                "api_url": "https://spoofed.example/v1",
                "api_key": "sk-spoof",
            },
        )
        self.assertEqual(resp.status_code, 403)

    async def test_me_returns_current_identity(self):
        resp = await self.client.get("/api/v1/auth/me", headers=self._headers("u-view"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["role"], "viewer")

    async def test_email_keyed_role_refines_whitelisted_user(self):
        resp = await self.client.get(
            "/api/v1/auth/me",
            headers=self._headers("u-mail", email="mail@example.com"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["role"], "operator")

    async def test_whitelisted_user_without_role_defaults_to_admin(self):
        resp = await self.client.get("/api/v1/auth/me", headers=self._headers("u-legacy"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["role"], "admin")

    async def test_removed_account_token_is_rejected(self):
        resp = await self.client.get(
            "/api/v1/model-manage/providers",
            headers={"Authorization": f"Bearer {self._token('u-ghost', role='admin')}"},
        )
        self.assertEqual(resp.status_code, 403)

    async def test_viewer_can_read_but_not_write(self):
        headers = self._headers("u-view")
        resp = await self.client.get("/api/v1/model-manage/providers", headers=headers)
        self.assertEqual(resp.status_code, 200)

        resp = await self.client.post(
            "/api/v1/model-manage/providers",
            headers=headers,
            json={
                "provider_name": "nope",
                "api_url": "https://nope.example/v1",
                "api_key": "sk-nope",
            },
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("无权", resp.json()["detail"])

        # 读取上游模型列表同样属于写操作
        resp = await self.client.post(
            "/api/v1/model-manage/fetch-models",
            headers=headers,
            json={"com_url": "https://nope.example/v1", "api_key": "sk-nope"},
        )
        self.assertEqual(resp.status_code, 403)

    async def test_operator_can_write_but_not_delete_provider(self):
        headers = self._headers("u-op")
        resp = await self.client.post(
            "/api/v1/model-manage/providers",
            headers=headers,
            json={
                "provider_name": "op-provider",
                "api_url": "https://op.example/v1",
                "api_key": "sk-op",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 201)
        provider_id = resp.json()["data"]["providerId"]

        resp = await self.client.delete(
            f"/api/v1/model-manage/providers/{provider_id}", headers=headers,
        )
        self.assertEqual(resp.status_code, 403)

    async def test_developer_issues_token_but_not_create_project(self):
        headers = self._headers("u-dev")

        resp = await self.client.post(
            "/api/v1/project-manage/projects",
            headers=headers,
            json={"project_name": "dev-project"},
        )
        self.assertEqual(resp.status_code, 403)

        resp = await self.client.post(
            f"/api/v1/project-manage/projects/{self.project_id}/tokens",
            headers=headers,
            json={"name": "dev-key"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 201)

    async def test_user_management_permissions(self):
        op_headers = self._headers("u-op")
        resp = await self.client.get("/api/v1/user-manage/list", headers=op_headers)
        self.assertEqual(resp.status_code, 200)

        # 充值属于资金操作，仅 admin
        resp = await self.client.post(
            "/api/v1/user-manage/recharge-points",
            headers=op_headers,
            json={"user_id": "u-view", "points": 100},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("无权", resp.json()["detail"])

        resp = await self.client.post(
            "/api/v1/user-manage/recharge-points",
            headers=self._headers("u-admin"),
            json={"user_id": "u-view", "points": 100},
        )
        self.assertEqual(resp.status_code, 200)

        # developer / viewer 连用户列表都不可读
        for uid in ("u-dev", "u-view"):
            resp = await self.client.get(
                "/api/v1/user-manage/list", headers=self._headers(uid),
            )
            self.assertEqual(resp.status_code, 403)

    async def test_upstream_api_key_hidden_from_read_only_roles(self):
        resp = await self.client.get(
            "/api/v1/model-manage/providers", headers=self._headers("u-dev"),
        )
        provider = resp.json()["data"]["list"][0]
        self.assertEqual(provider["apiKey"], "")
        self.assertTrue(provider["apiKeySet"])

        resp = await self.client.get(
            f"/api/v1/model-manage/providers/{self.provider_id}",
            headers=self._headers("u-view"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["apiKey"], "")
        self.assertTrue(resp.json()["data"]["apiKeySet"])

        resp = await self.client.get(
            "/api/v1/model-manage/providers", headers=self._headers("u-admin"),
        )
        provider = resp.json()["data"]["list"][0]
        self.assertEqual(provider["apiKey"], "sk-upstream-secret")

    async def test_model_api_key_hidden_from_read_only_roles(self):
        headers = self._headers("u-view")
        resp = await self.client.get("/api/v1/model-manage/list", headers=headers)
        model = resp.json()["data"]["list"][0]
        self.assertEqual(model["apiKey"], "")
        self.assertTrue(model["apiKeySet"])

        resp = await self.client.get(
            f"/api/v1/model-manage/{self.model_id}", headers=headers,
        )
        self.assertEqual(resp.json()["data"]["apiKey"], "")

        resp = await self.client.get(
            "/api/v1/model-manage/list", headers=self._headers("u-admin"),
        )
        model = resp.json()["data"]["list"][0]
        self.assertEqual(model["apiKey"], "sk-upstream-secret")


if __name__ == "__main__":
    unittest.main()
