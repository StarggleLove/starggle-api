"""项目与 API Token 管理控制器"""
import csv
import io
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.schemas.project_manage import ProjectCreate, ProjectUpdate, TokenCreate, TokenRotate
from app.services import project_manage_service, usage_manage_service
from app.utils.admin_auth import require_admin_user, require_permission
from app.utils.rbac import Perm
from app.utils.response import ResponseFactory

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin_user)])


# ==================== 项目 ====================


@router.get("/projects", dependencies=[Depends(require_permission(Perm.PROJECTS_READ))])
async def list_projects(keyword: str = "", db: AsyncSession = Depends(get_db)):
    """项目列表（含今日/本月用量与有效 Token 数）"""
    data = await project_manage_service.list_projects(db, keyword)
    return ResponseFactory.ok(data={"list": data, "total": len(data)}, message="查询成功")


@router.post("/projects", dependencies=[Depends(require_permission(Perm.PROJECTS_WRITE))])
async def create_project(data: ProjectCreate, db: AsyncSession = Depends(get_db)):
    """新建项目"""
    try:
        project = await project_manage_service.create_project(db, data)
    except ValueError as e:
        return ResponseFactory.bad_request(message=str(e))
    return ResponseFactory.created(
        data=project_manage_service.project_to_dict(project), message="新增成功",
    )


@router.get("/projects/{project_id}", dependencies=[Depends(require_permission(Perm.PROJECTS_READ))])
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """项目详情 + Token 列表"""
    project = await project_manage_service.get_project(db, project_id)
    if project is None:
        return ResponseFactory.not_found(message="项目不存在")
    tokens = await project_manage_service.list_tokens(db, project_id)
    return ResponseFactory.ok(
        data={"project": project_manage_service.project_to_dict(project), "tokens": tokens},
        message="查询成功",
    )


@router.put("/projects/{project_id}", dependencies=[Depends(require_permission(Perm.PROJECTS_WRITE))])
async def update_project(
        project_id: str,
        data: ProjectUpdate,
        db: AsyncSession = Depends(get_db),
):
    """更新项目（只传需要修改的字段；限额显式传 null 表示清空）"""
    try:
        project = await project_manage_service.update_project(db, project_id, data)
    except ValueError as e:
        return ResponseFactory.bad_request(message=str(e))
    if project is None:
        return ResponseFactory.not_found(message="项目不存在")
    return ResponseFactory.ok(
        data=project_manage_service.project_to_dict(project), message="更新成功",
    )


# ==================== Token ====================


@router.get("/projects/{project_id}/tokens", dependencies=[Depends(require_permission(Perm.PROJECTS_READ))])
async def list_tokens(project_id: str, db: AsyncSession = Depends(get_db)):
    """项目 Token 列表（不包含明文与哈希）"""
    project = await project_manage_service.get_project(db, project_id)
    if project is None:
        return ResponseFactory.not_found(message="项目不存在")
    tokens = await project_manage_service.list_tokens(db, project_id)
    return ResponseFactory.ok(data={"list": tokens, "total": len(tokens)}, message="查询成功")


@router.post("/projects/{project_id}/tokens", dependencies=[Depends(require_permission(Perm.TOKENS_WRITE))])
async def create_token(
        project_id: str,
        data: TokenCreate,
        db: AsyncSession = Depends(get_db),
):
    """创建 Token；完整密钥只在本次响应中返回一次"""
    try:
        token, raw = await project_manage_service.create_token(db, project_id, data)
    except ValueError as e:
        return ResponseFactory.bad_request(message=str(e))
    payload = project_manage_service.token_to_dict(token)
    payload["token"] = raw
    return ResponseFactory.created(data=payload, message="创建成功，密钥只显示一次")


@router.post("/tokens/{token_id}/revoke", dependencies=[Depends(require_permission(Perm.TOKENS_WRITE))])
async def revoke_token(token_id: str, db: AsyncSession = Depends(get_db)):
    """吊销 Token"""
    token = await project_manage_service.revoke_token(db, token_id)
    if token is None:
        return ResponseFactory.not_found(message="Token 不存在")
    return ResponseFactory.ok(
        data=project_manage_service.token_to_dict(token), message="已吊销",
    )


@router.post("/tokens/{token_id}/rotate", dependencies=[Depends(require_permission(Perm.TOKENS_WRITE))])
async def rotate_token(
        token_id: str,
        data: TokenRotate | None = None,
        db: AsyncSession = Depends(get_db),
):
    """轮换 Token：旧密钥立即失效，新密钥只返回一次"""
    try:
        result = await project_manage_service.rotate_token(db, token_id, data)
    except ValueError as e:
        return ResponseFactory.bad_request(message=str(e))
    if result is None:
        return ResponseFactory.not_found(message="Token 不存在")
    token, raw = result
    payload = project_manage_service.token_to_dict(token)
    payload["token"] = raw
    return ResponseFactory.ok(data=payload, message="轮换成功，新密钥只显示一次")


# ==================== 项目用量 ====================


@router.get("/projects/{project_id}/usage", dependencies=[Depends(require_permission(Perm.PROJECTS_READ))])
async def project_usage(
        project_id: str,
        days: int = 30,
        db: AsyncSession = Depends(get_db),
):
    """项目用量：配额快照 + 每日趋势 + 按模型/功能码分组"""
    data = await project_manage_service.get_project_dashboard(db, project_id, days)
    if data is None:
        return ResponseFactory.not_found(message="项目不存在")
    return ResponseFactory.ok(data=data, message="查询成功")


@router.get("/projects/{project_id}/records", dependencies=[Depends(require_permission(Perm.PROJECTS_READ))])
async def project_records(
        project_id: str,
        page: int = 1,
        page_size: int = 20,
        start_date: str = "",
        end_date: str = "",
        db: AsyncSession = Depends(get_db),
):
    """项目调用记录分页"""
    records, total = await usage_manage_service.list_call_records(
        db, page=page, page_size=page_size, project_id=project_id,
        start_date=start_date, end_date=end_date,
    )
    return ResponseFactory.ok(data={"list": records, "total": total}, message="查询成功")


@router.get("/projects/{project_id}/records.csv", dependencies=[Depends(require_permission(Perm.PROJECTS_READ))])
async def export_project_records(
        project_id: str,
        start_date: str = "",
        end_date: str = "",
        db: AsyncSession = Depends(get_db),
):
    """导出项目调用记录 CSV（最多 2 万行）"""
    project = await project_manage_service.get_project(db, project_id)
    if project is None:
        return ResponseFactory.not_found(message="项目不存在")

    records = await usage_manage_service.export_call_records(
        db, project_id=project_id, start_date=start_date, end_date=end_date,
    )
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([
        "请求ID", "时间", "用户", "模型", "功能码", "流式",
        "输入Token", "输出Token", "总Token", "成本", "币种", "重试次数",
    ])
    for row in records:
        writer.writerow([
            row.get("requestId") or "",
            row.get("createdAt") or "",
            row.get("userId") or "",
            row.get("model") or "",
            row.get("featureCode") or "",
            "是" if row.get("isStream") else "否",
            row.get("promptTokens") or 0,
            row.get("completionTokens") or 0,
            row.get("totalTokens") or 0,
            row.get("estimatedCost") if row.get("estimatedCost") is not None else "",
            row.get("costCurrency") or "",
            row.get("retryCount") or 0,
        ])

    content = "﻿" + buffer.getvalue()
    filename = f"project-{project_id}-records.csv"
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
