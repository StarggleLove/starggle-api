"""business-sync 集成测试基类：独立 SQLite + 依赖覆盖，不连接 MySQL."""
import unittest
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession

from app.config.database import Base, get_db
from app.models.channel_health_info import ChannelHealthInfo
from app.models.cluster_routing_config import ClusterRoutingConfig
from app.models.llm_provider_info import LlmProviderInfo
from app.models.model_manage_info import ModelManageInfo
from app.models.order_info import OrderInfo
from app.models.project_info import ProjectInfo
from app.models.project_token_info import ProjectTokenInfo
from app.models.project_usage_daily import ProjectUsageDaily
from app.models.user_daily_quota import UserDailyQuota
from app.models.user_info import UserInfo
from app.models.user_quota_policy import UserQuotaPolicy
from app.models.user_token_info import UserTokenInfo
from app.utils.admin_auth import require_admin_user
from app.utils.rbac import AdminIdentity, Role
from main import app

TEST_DB = Path(__file__).resolve().parent.parent / "data" / "business_unittest.db"

TABLES = [
    ModelManageInfo.__table__,
    LlmProviderInfo.__table__,
    UserTokenInfo.__table__,
    UserDailyQuota.__table__,
    UserInfo.__table__,
    UserQuotaPolicy.__table__,
    OrderInfo.__table__,
    ProjectInfo.__table__,
    ProjectTokenInfo.__table__,
    ProjectUsageDaily.__table__,
    ChannelHealthInfo.__table__,
    ClusterRoutingConfig.__table__,
]


async def seed_user(
        session_factory,
        user_id: str = "u1",
        *,
        point_balance: int = 5000,
        user_name: str = "测试用户",
        password: str = "x",
        email: str | None = None,
) -> None:
    """写入一条共享表 user_info 记录（默认钻石档，避免会员次数限额干扰）."""
    async with session_factory() as db:
        db.add(UserInfo(
            user_id=user_id,
            user_name=user_name,
            user_head_img="",
            login_id=f"login-{user_id}",
            password=password,
            email=email or f"{user_id}@example.com",
            point_balance=point_balance,
        ))
        await db.commit()


def _cleanup_db() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(TEST_DB) + suffix)
        if path.exists():
            path.unlink()


class BusinessGatewayTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _cleanup_db()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{TEST_DB.as_posix()}")
        self.Session = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False,
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))

        async def override_get_db():
            async with self.Session() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[require_admin_user] = lambda: AdminIdentity(
            username="test-admin",
            role=Role.ADMIN,
            user_id="test-admin",
            user_name="测试管理员",
            email="test-admin@example.com",
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()
        await self.engine.dispose()
        _cleanup_db()
