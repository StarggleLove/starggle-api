"""模型管理 控制器"""
import logging
from difflib import SequenceMatcher
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.schemas.llm_provider import ProviderCreate, ProviderUpdate
from app.schemas.model_manage import ModelManageCreate, ModelManageUpdate, ModelManageQuery
from app.services import llm_provider_service, model_manage_service, usage_manage_service
from app.utils.response import ResponseFactory
from app.utils.admin_auth import require_admin_user, require_permission
from app.utils.rbac import AdminIdentity, Perm, has_permission
from app.api.v1.controller.call_manage_controller import (
    _fetch_models_from_upstream,
    _validate_url,
    FetchModelsBody as FetchModelsRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin_user)])

# companlist.txt 文件路径
_companlist_path = Path(__file__).parent.parent.parent.parent.parent / "companlist.txt"


def _read_companies() -> list[dict]:
    """读取厂家列表文件"""
    companies = []
    if not _companlist_path.exists():
        return companies
    with open(_companlist_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split(",", 2)
                if len(parts) >= 2:
                    companies.append({
                        "domain": parts[0].strip(),
                        "name": parts[1].strip(),
                        "notes": parts[2].strip() if len(parts) > 2 else "",
                    })
    return companies


def _fuzzy_match(query: str, target: str) -> float:
    """计算两个字符串的模糊匹配度（0~1）"""
    return SequenceMatcher(None, query.lower(), target.lower()).ratio()


@router.get("/company-search", dependencies=[Depends(require_permission(Perm.MODELS_READ))])
async def search_company(name: str = ""):
    """
    根据厂家名称模糊搜索，返回匹配的厂家信息（URL 等）
    用于前端输入运营商名称时自动填充 URL
    name 为空时返回全部厂商列表
    """
    companies = _read_companies()

    # 空查询返回全部
    if not name or len(name) < 2:
        return ResponseFactory.ok(data={"matches": companies}, message="查询成功")

    scored = []
    for c in companies:
        # 综合名称和域名进行匹配
        name_score = _fuzzy_match(name, c["name"])
        domain_score = _fuzzy_match(name, c["domain"])
        # 子串匹配加成
        bonus = 0.2 if name.lower() in c["name"].lower() or name.lower() in c["domain"].lower() else 0
        score = max(name_score, domain_score) + bonus
        if score >= 0.4:
            scored.append({"company": c, "score": round(min(score, 1.0), 2)})

    scored.sort(key=lambda x: x["score"], reverse=True)
    matches = [s["company"] for s in scored[:5]]  # 最多返回5条
    return ResponseFactory.ok(data={"matches": matches}, message="查询成功")


@router.post("/fetch-models", dependencies=[Depends(require_permission(Perm.MODELS_WRITE))])
async def fetch_models(body: FetchModelsRequest):
    """
    从运营商 API 拉取可用模型列表
    POST body: { com_url, api_key }
    """
    try:
        _validate_url(body.com_url.rstrip("/") + "/models")
    except Exception:
        return ResponseFactory.bad_request(message="API 地址不合法（仅支持 HTTPS 公网地址）")

    models, error = await _fetch_models_from_upstream(body.com_url, body.api_key)

    if not models and error:
        return ResponseFactory.bad_request(message=f"获取模型列表失败: {error}")

    models = sorted(set(models))
    return ResponseFactory.ok(data={"models": models}, message="查询成功")


# ==================== 大模型提供商 CRUD ====================
# 注意：/providers 系列必须定义在 /{model_id} 之前，否则会被当作模型 ID 匹配


def _reveal_secret(identity: AdminIdentity) -> bool:
    """当前角色是否允许查看上游 API Key"""
    return has_permission(identity.role, Perm.SECRETS_READ)


def _provider_row(provider, model_count: int, reveal_secret: bool) -> dict:
    return {
        "providerId": provider.provider_id,
        "providerName": provider.provider_name,
        "apiUrl": provider.api_url,
        "apiKey": provider.api_key if reveal_secret else "",
        "apiKeySet": bool(provider.api_key),
        "modelCount": model_count,
        "remarks": provider.remarks,
        "createdAt": provider.created_at.isoformat() if provider.created_at else None,
        "updatedAt": provider.updated_at.isoformat() if provider.updated_at else None,
    }


def _model_payload(model, reveal_secret: bool) -> dict:
    """模型字典序列化，未授权角色不返回上游密钥"""
    data = {
        column.name: getattr(model, column.name)
        for column in model.__table__.columns
    }
    data["api_key_set"] = bool(data.get("api_key"))
    if not reveal_secret:
        data["api_key"] = ""
    return data


@router.get("/providers", dependencies=[Depends(require_permission(Perm.PROVIDERS_READ))])
async def list_providers(
        keyword: str = "",
        db: AsyncSession = Depends(get_db),
        identity: AdminIdentity = Depends(require_admin_user),
):
    """提供商列表（含关联模型数量）"""
    providers = await llm_provider_service.list_providers(db, keyword)
    counts = await llm_provider_service.get_model_counts(db)
    reveal = _reveal_secret(identity)
    data = [_provider_row(p, counts.get(p.provider_id, 0), reveal) for p in providers]
    return ResponseFactory.ok(data={"list": data, "total": len(data)}, message="查询成功")


@router.get("/providers/{provider_id}", dependencies=[Depends(require_permission(Perm.PROVIDERS_READ))])
async def get_provider(
        provider_id: str,
        db: AsyncSession = Depends(get_db),
        identity: AdminIdentity = Depends(require_admin_user),
):
    """提供商详情"""
    provider = await llm_provider_service.get_provider_by_id(db, provider_id)
    if provider is None:
        return ResponseFactory.not_found(message="提供商不存在")
    counts = await llm_provider_service.get_model_counts(db)
    return ResponseFactory.ok(
        data=_provider_row(provider, counts.get(provider_id, 0), _reveal_secret(identity)),
        message="查询成功",
    )


@router.post("/providers", dependencies=[Depends(require_permission(Perm.PROVIDERS_WRITE))])
async def create_provider(
        data: ProviderCreate,
        db: AsyncSession = Depends(get_db),
        identity: AdminIdentity = Depends(require_admin_user),
):
    """新增提供商"""
    try:
        provider = await llm_provider_service.create_provider(db, data)
    except ValueError as e:
        return ResponseFactory.bad_request(message=str(e))
    return ResponseFactory.created(
        data=_provider_row(provider, 0, _reveal_secret(identity)), message="新增成功",
    )


@router.put("/providers/{provider_id}", dependencies=[Depends(require_permission(Perm.PROVIDERS_WRITE))])
async def update_provider(
        provider_id: str,
        data: ProviderUpdate,
        db: AsyncSession = Depends(get_db),
        identity: AdminIdentity = Depends(require_admin_user),
):
    """更新提供商（名称/地址/密钥级联同步到关联模型）"""
    try:
        provider = await llm_provider_service.update_provider(db, provider_id, data)
    except ValueError as e:
        return ResponseFactory.bad_request(message=str(e))
    if provider is None:
        return ResponseFactory.not_found(message="提供商不存在")
    counts = await llm_provider_service.get_model_counts(db)
    return ResponseFactory.ok(
        data=_provider_row(provider, counts.get(provider_id, 0), _reveal_secret(identity)),
        message="更新成功",
    )


@router.delete("/providers/{provider_id}", dependencies=[Depends(require_permission(Perm.PROVIDERS_DELETE))])
async def delete_provider(provider_id: str, db: AsyncSession = Depends(get_db)):
    """删除提供商（仍有关联模型时拒绝）"""
    deleted, reason = await llm_provider_service.delete_provider(db, provider_id)
    if not deleted:
        return ResponseFactory.bad_request(message=reason)
    return ResponseFactory.ok(message="删除成功")


# ==================== 用量统计 / 调用记录 ====================
# 同样必须定义在 /{model_id} 之前


@router.get("/usage/overview", dependencies=[Depends(require_permission(Perm.USAGE_READ))])
async def usage_overview(days: int = 7, db: AsyncSession = Depends(get_db)):
    """用量总览：汇总 + 每日趋势 + 按模型分布"""
    data = await usage_manage_service.get_usage_overview(db, days)
    return ResponseFactory.ok(data=data, message="查询成功")


@router.get("/calls", dependencies=[Depends(require_permission(Perm.CALLS_READ))])
async def list_call_records(
        page: int = 1,
        page_size: int = 20,
        project_id: str = "",
        user_id: str = "",
        model: str = "",
        purpose: str = "",
        start_date: str = "",
        end_date: str = "",
        db: AsyncSession = Depends(get_db),
):
    """调用记录分页列表"""
    records, total = await usage_manage_service.list_call_records(
        db, page=page, page_size=page_size, project_id=project_id,
        user_id=user_id, model=model,
        purpose=purpose, start_date=start_date, end_date=end_date,
    )
    return ResponseFactory.ok(data={"list": records, "total": total}, message="查询成功")


# ==================== 模型 CRUD ====================


@router.get("/list", dependencies=[Depends(require_permission(Perm.MODELS_READ))])
async def list_models(
        query: ModelManageQuery = Depends(),
        db: AsyncSession = Depends(get_db),
        identity: AdminIdentity = Depends(require_admin_user),
):
    """获取模型列表（支持多条件筛选）"""
    models = await model_manage_service.get_model_list(db, query)
    total = await model_manage_service.get_model_count(db)
    reveal = _reveal_secret(identity)
    return ResponseFactory.ok(
        data={"list": [_model_payload(m, reveal) for m in models], "total": total},
        message="查询成功",
    )


@router.get("/{model_id}", dependencies=[Depends(require_permission(Perm.MODELS_READ))])
async def get_model(
        model_id: str,
        db: AsyncSession = Depends(get_db),
        identity: AdminIdentity = Depends(require_admin_user),
):
    """根据 ID 获取模型详情"""
    model = await model_manage_service.get_model_by_id(db, model_id)
    if not model:
        return ResponseFactory.not_found(message="模型不存在")
    return ResponseFactory.ok(data=_model_payload(model, _reveal_secret(identity)), message="查询成功")


@router.post("/", dependencies=[Depends(require_permission(Perm.MODELS_WRITE))])
async def create_model(
        data: ModelManageCreate,
        db: AsyncSession = Depends(get_db),
        identity: AdminIdentity = Depends(require_admin_user),
):
    """新增模型（通过 provider_id 关联提供商并回填地址/密钥）"""
    try:
        model = await model_manage_service.create_model(db, data)
    except ValueError as e:
        return ResponseFactory.bad_request(message=str(e))
    return ResponseFactory.created(
        data=_model_payload(model, _reveal_secret(identity)), message="新增成功",
    )


@router.put("/{model_id}", dependencies=[Depends(require_permission(Perm.MODELS_WRITE))])
async def update_model(
        model_id: str,
        data: ModelManageUpdate,
        db: AsyncSession = Depends(get_db),
        identity: AdminIdentity = Depends(require_admin_user),
):
    """更新模型（部分更新）—— 服务层为单一真实来源，不存在则返回 404"""
    try:
        model = await model_manage_service.update_model(db, model_id, data)
    except ValueError as e:
        return ResponseFactory.bad_request(message=str(e))
    if not model:
        return ResponseFactory.not_found(message="模型不存在")
    return ResponseFactory.ok(
        data=_model_payload(model, _reveal_secret(identity)), message="更新成功",
    )


@router.delete("/{model_id}", dependencies=[Depends(require_permission(Perm.MODELS_DELETE))])
async def delete_model(model_id: str, db: AsyncSession = Depends(get_db)):
    """删除模型"""
    deleted = await model_manage_service.delete_model(db, model_id)
    if not deleted:
        return ResponseFactory.not_found(message="模型不存在")
    return ResponseFactory.ok(message="删除成功")
