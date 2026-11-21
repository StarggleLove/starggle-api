"""LLM 调用管理 控制器

提供基于数据库模型配置的 LLM 转发端点：
- POST /chat/completions — 转发聊天补全请求（支持流式/非流式）
- POST /models — 从上游 API 获取可用模型列表
"""
import copy
import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncGenerator, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.services import (
    call_manage_service,
    health_manage_service,
    project_manage_service,
    user_quota_service,
)
from app.utils.admin_auth import require_permission
from app.utils.gateway_error import gateway_error_response
from app.utils.rbac import Perm
from app.utils.response import ResponseFactory
from propmt import (
    ai_sec_propmt,
    core_list,
    core_propmt_deepseek,
    core_propmt_one,
    core_propmt_two,
    super_core_propmt,
    user_active_propmt,
    user_core_propmt,
    user_default_propmt,
    user_sec_propmt,
    user_top_propmt,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# 完整请求体/响应正文可能包含用户提示词，默认不落日志
DEBUG = os.getenv("GATEWAY_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")

# ── 模型名称校验 ──
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-./@:]+$")

# ── 功能码：仅用于用量分组统计，非法值回落 uncategorized ──
_DEFAULT_FEATURE_CODE = "uncategorized"
_FEATURE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")

# ── o 系列模型（上游不支持 response_format / 非默认 temperature）──
_O_SERIES_RE = re.compile(r"o[13](-|mini|pro|\b).*", re.IGNORECASE)

# ── Claude 思考模式默认预算（_apply_thinking_params 注入与预算收敛共用）──
_CLAUDE_THINKING_BUDGET = 10000

# ── Anthropic 原生 /v1/messages 协议（ofox 等兼容网关，样例见 test.txt）──
_ANTHROPIC_VERSION = "2023-06-01"       # 请求头 anthropic-version 固定值（协议要求）
_ANTHROPIC_DEFAULT_MAX_TOKENS = 8192    # 原生协议 max_tokens 必填：模型/客户端均未配置时的兜底

# 原生 SSE 事件族：命中即走 _ingest_anthropic_sse_data。分流必须先于通用 usage 提取——
# message_delta 的顶层 usage 只带 output_tokens，落进通用提取会把 prompt_tokens 覆写为 0，
# 冲掉 message_start 已记下的 input_tokens。未列出的（ping/content_block_start 等）
# 落回 OpenAI 通用路径亦为无害 no-op
_ANTHROPIC_SSE_EVENT_TYPES = frozenset({"message_start", "content_block_delta", "message_delta", "error"})

# ── 智谱 GLM 版本门槛（模型名形如 glm-<major>.<minor>[-变体]，如 glm-5.3-flash / z-ai/glm-4.6）──
# 官方文档（docs.bigmodel.cn「深度思考」）：
#   · reasoning_effort 仅 GLM-5.2 及以上支持，低版本传了报错（GLM-4.6 及以下无强度档）；
#   · GLM-5.3 / 5.3-FLASH / 5.3-FLASHX 起**不再支持 thinking.type=disabled**
#     （传了返回 HTTP 400，错误码 1210「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」）。
_GLM_NAME_RE = re.compile(r"glm[-_ ]?(\d+)(?:\.(\d+))?", re.IGNORECASE)
_GLM_EFFORT_MIN_VER = (5, 2)
_GLM_FORCED_THINKING_MIN_VER = (5, 3)
_GLM_THINKING_EFFORT = "low"      # GLM-5.2+ 的思考强度档；厂商默认 max 最贵最慢

# ── MiniMax 强制思考门槛（模型名形如 minimax-m3.1 / MiniMax-M3.1）──
# M3.1 起与 GLM-5.3 族同款「强制思考、不可关闭」，且支持 reasoning_effort 强度档，
# 参数格式与智谱 GLM 完全一致（thinking.type + reasoning_effort），整体复用下方 GLM 判定链路。
# 注意：M 系版本号数字小（3.1 < 5.2），门槛不能拿 GLM 的常量来比，须按家族取（见 _thinking_gates）。
_MINIMAX_NAME_RE = re.compile(r"minimax[-_ ]?m?(\d+)(?:\.(\d+))?", re.IGNORECASE)
_MINIMAX_EFFORT_MIN_VER = (3, 1)
_MINIMAX_FORCED_THINKING_MIN_VER = (3, 1)


# ── systemPropmt 占位符替换（业务版提示词注入）──
_PROMPT_MARKERS = (
    "<core_list>",
    "<core_propmt>",
    "</core_propmt>",
    "<user_top_propmt>",
    "<user_default_propmt>",
    "<user_core_propmt>",
    "<user_active_propmt>",
    "<super_core_propmt>",
    "<user_sec_propmt>",
    "<ai_sec_propmt>",
    "<core_propmt_deepseek>",
    "</role_core_propmt>",
)
_PROMPT_REPLACEMENTS = {
    "<core_list>": core_list,
    "<core_propmt>": core_propmt_one,
    "</core_propmt>": core_propmt_two,
    "<user_top_propmt>": user_top_propmt,
    "<user_default_propmt>": user_default_propmt,
    "<user_core_propmt>": user_core_propmt,
    "<user_active_propmt>": user_active_propmt,
    "<super_core_propmt>": super_core_propmt,
    "<user_sec_propmt>": user_sec_propmt,
    "<ai_sec_propmt>": ai_sec_propmt,
    "<core_propmt_deepseek>": core_propmt_deepseek,
}


def _replace_core_prompts(obj: Any) -> Any:
    """递归遍历请求体，替换 system prompt 中的占位符."""
    if isinstance(obj, str):
        if not any(marker in obj for marker in _PROMPT_MARKERS):
            return obj
        obj = obj.replace("</role_core_propmt>", "")
        for marker, builder in _PROMPT_REPLACEMENTS.items():
            if marker in obj:
                obj = obj.replace(marker, builder())
        return obj
    if isinstance(obj, dict):
        return {k: _replace_core_prompts(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_core_prompts(item) for item in obj]
    return obj


def _glm_version(model_name: str) -> Optional[tuple]:
    """从模型名解析 GLM/MiniMax 版本号 → (major, minor)；解析不出返回 None（按最低能力处理）。

    旧世代命名（chatglm-130b / glm-130b 之类）会解析出三位数版本号，越界即按「解析不出」
    处理——避免把 reasoning_effort 一类新参数发给不认识它的老模型。
    """
    text = model_name or ""
    m = _GLM_NAME_RE.search(text) or _MINIMAX_NAME_RE.search(text)
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2) or 0)
    if major > 9 or minor > 9:
        return None
    return major, minor


def _is_minimax(model_name: str) -> bool:
    """模型名是否属于 MiniMax 家族（与 GLM 共用思考判定链路，见 _glm_thinking_plan）。"""
    return "minimax" in (model_name or "").lower()


def _thinking_gates(model_name: str) -> tuple:
    """当前模型的（effort 支持门槛 / 强制思考门槛），按家族取常量。

    MiniMax 的 M3.1 版本数字上小于 GLM 的 5.2/5.3，但能力对齐——不能拿 GLM 的门槛去比。
    """
    if _is_minimax(model_name):
        return _MINIMAX_EFFORT_MIN_VER, _MINIMAX_FORCED_THINKING_MIN_VER
    return _GLM_EFFORT_MIN_VER, _GLM_FORCED_THINKING_MIN_VER


def _glm_forced_thinking(model_name: str) -> bool:
    """所选模型是否为「强制思考、不可关闭」型号（GLM-5.3 族 / MiniMax-M3.1+）。

    这类型号关不掉：官方对 disabled 返 400，聚合网关则静默忽略并让模型按默认 max 跑。
    与运营商地址无关（智谱直连与 OpenRouter 都一样关不掉），故只按模型名判定。
    """
    ver = _glm_version(model_name)
    if ver is None:
        return False
    forced_min = _thinking_gates(model_name)[1]
    return ver >= forced_min


def _glm_thinking_plan(model_name: str, enabled: bool) -> Optional[tuple]:
    """GLM/MiniMax 的思考决策 → (是否思考, 强度|None)；其他模型返回 None。

    两种出站格式共用同一套档位判断，各分支只负责把结果拼成自己的字段形态：
      · 智谱直连/聚合（非 OpenRouter url）：``thinking={type}`` + 顶层 ``reasoning_effort``
      · OpenRouter：嵌套 ``reasoning={enabled, effort}``
    与客户端 CallModelStreamService._glm_thinking_plan 同口径（两仓共用契约，改动须同步，客户端仓需同步加 MiniMax-M3.1 判定）。
    """
    text = (model_name or "").lower()
    if "glm" not in text and "minimax" not in text:
        return None                    # 非 GLM/MiniMax：不属于本规则的适用范围
    ver = _glm_version(model_name)
    effort_min, forced_min = _thinking_gates(model_name)
    effort = _GLM_THINKING_EFFORT if (ver is not None and ver >= effort_min) else None
    if ver is not None and ver >= forced_min:
        return True, effort            # 强制思考型号：关不掉，只能压强度
    return enabled, (effort if enabled else None)


# ── SSRF 防护：禁止的目标地址 ──
_BLOCKED_HOSTS = frozenset({
    "127.0.0.1", "localhost", "0.0.0.0",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "::1", "fc00::/7", "fe80::/10",
})

def _validate_model_name(name: str) -> str:
    """校验 model 名称，防止路径注入 / SSRF.

    允许 ``/``（如 ``deepseek/deepseek-v4-flash``），
    拒绝 ``..``（目录穿越）、``?``、``#`` 及空白/换行符.
    """
    if not name or not _MODEL_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="非法 model 名称")
    if ".." in name:
        raise HTTPException(status_code=400, detail="非法 model 名称")
    return name


def _normalize_feature_code(raw: Optional[str]) -> str:
    """规范化功能码：小写、限长 64；非法值回落 uncategorized."""
    code = (raw or "").strip().lower()
    if not code:
        return _DEFAULT_FEATURE_CODE
    if not _FEATURE_CODE_RE.match(code):
        logger.warning("忽略非法 X-Feature-Code=%r，按 uncategorized 统计", raw)
        return _DEFAULT_FEATURE_CODE
    return code


def _extract_bearer(authorization: Optional[str]) -> str:
    """从 Authorization 头取出 Bearer 值（大小写不敏感）."""
    value = (authorization or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _counts_as_channel_failure(status_code: int) -> bool:
    """只有渠道自身问题才计入健康失败；400/422 等客户端错误不计."""
    return status_code in (401, 403, 408, 409, 425, 429) or status_code >= 500


def _is_retryable_status(status_code: int) -> bool:
    """可换渠道重试的上游状态码；400/404/422 是请求或模型本身问题，不重试."""
    return _counts_as_channel_failure(status_code)


def _prepare_forward_plan(
        original_body: dict,
        selected,
        *,
        is_async: bool,
        identity,
        raw_token: str,
        response_format_header: Optional[str],
        thinking_header: Optional[str],
        request_id: str = "",
) -> dict:
    """为一次上游尝试构造独立请求体 / URL / 请求头.

    每次重试都从 original_body 深拷贝，避免上一次尝试注入的模型参数污染下一次。
    """
    # ── 校验并构造转发 URL ──
    raw_url = selected.com_url.rstrip("/")
    # 兼容 DB 里存 https://openrouter.ai 或 https://openrouter.ai/api/v1 的情况
    # Anthropic 原生协议（ofox 等：POST /anthropic/v1/messages）不走 /chat/completions：
    #   · DB 存完整端点 …/v1/messages → 原样使用
    #   · DB 存 …/anthropic 基址 → 补 /v1/messages
    if raw_url.endswith("/chat/completions"):
        forward_url = raw_url
    elif raw_url.endswith("/messages"):
        forward_url = raw_url
    elif raw_url.endswith("/anthropic"):
        forward_url = f"{raw_url}/v1/messages"
    else:
        forward_url = f"{raw_url}/chat/completions"
    _validate_url(forward_url)
    is_anthropic_native = _is_anthropic_native_url(forward_url)

    body = copy.deepcopy(original_body)

    # Anthropic 原生端点：messages 里的 role:"system" 提为顶层 system 字段
    if is_anthropic_native:
        _apply_anthropic_native_body(body)

    # ── 确定 API Key ──
    # 网关 Token 永远不转发给上游；异步链与项目 Token 强制使用 DB 配置的 Key。
    if is_async or identity is not None:
        api_key = selected.api_key
    else:
        client_key = raw_token if raw_token else ""
        api_key = client_key if client_key else selected.api_key
        if not api_key:
            raise HTTPException(status_code=401, detail="缺少 Authorization header 或 API Key")

    # ── response_format 注入 ──
    if response_format_header == "json_object":
        model_name_lower = selected.ai_model_name.lower()
        is_o_series = bool(_O_SERIES_RE.match(model_name_lower))
        if is_anthropic_native:
            logger.info(
                "POST /call-manage/chat/completions -> Anthropic 原生端点不支持 response_format，跳过注入 model=%s",
                selected.ai_model_name,
            )
        elif not is_o_series:
            body["response_format"] = {"type": "json_object"}
        else:
            logger.info(
                "POST /call-manage/chat/completions -> model=%s 不支持 response_format，跳过注入",
                selected.ai_model_name,
            )

    client_model = body.get("model", "")
    logger.info(
        "POST /call-manage/chat/completions -> 客户端传入 model=%r, selected.ai_model_name=%r",
        client_model, selected.ai_model_name,
    )
    model_name = client_model.strip()
    if not model_name:
        model_name = selected.ai_model_name
        body["model"] = model_name
        logger.info(
            "POST /call-manage/chat/completions -> 客户端未传 model，使用数据库配置: %r",
            model_name,
        )
    else:
        model_name = _validate_model_name(model_name)
        logger.info(
            "POST /call-manage/chat/completions -> 使用客户端传入 model: %r",
            model_name,
        )

    # ── 思考模式开关判定 ──
    think_enabled: Optional[bool] = None
    model_think_mode = selected.is_think or "0"
    if model_think_mode == "1":
        think_enabled = True
    elif model_think_mode == "2":
        think_enabled = False
    elif thinking_header is not None:
        think_enabled = thinking_header.strip().lower() in ("1", "true", "yes", "on", "enabled")
        logger.info(
            "POST /call-manage/chat/completions -> X-Thinking=%r enabled=%s model=%s",
            thinking_header, think_enabled, model_name,
        )
    if model_think_mode in ("1", "2"):
        logger.info(
            "POST /call-manage/chat/completions -> 模型思考模式=%s 生效 enabled=%s model=%s",
            model_think_mode, think_enabled, model_name,
        )

    # ── 模型级参数覆盖 ──
    model_name_lower = model_name.lower()
    is_o_series = bool(_O_SERIES_RE.match(model_name_lower))
    is_claude = "claude" in model_name_lower
    body_thinking = body.get("thinking")
    client_think_on = (
        (isinstance(body_thinking, dict) and body_thinking.get("type") == "enabled")
        or body.get("reasoning_effort") is not None
    )
    think_on = think_enabled is True or (think_enabled is None and client_think_on)
    if selected.temperature is not None and selected.top_p is not None:
        logger.warning(
            "模型同时配置 temperature 与 top_p，按 temperature 生效: model=%s", model_name,
        )
    sampling_blocked = is_o_series or (is_claude and think_on)
    if not sampling_blocked:
        if selected.temperature is not None:
            body["temperature"] = selected.temperature
        elif selected.top_p is not None:
            body["top_p"] = selected.top_p
        if selected.top_k is not None:
            body["top_k"] = selected.top_k
    if selected.max_tokens is not None:
        body["max_tokens"] = selected.max_tokens
    if any(v is not None for v in (selected.temperature, selected.top_p, selected.top_k, selected.max_tokens)):
        logger.info(
            "POST /call-manage/chat/completions -> 模型级参数覆盖: model=%r temperature=%r top_p=%r top_k=%r max_tokens=%r",
            model_name, body.get("temperature"), body.get("top_p"), body.get("top_k"), body.get("max_tokens"),
        )

    # ── Anthropic 原生端点：max_tokens 必填 ──
    body_max_tokens = body.get("max_tokens")
    if is_anthropic_native and (not isinstance(body_max_tokens, int) or isinstance(body_max_tokens, bool)):
        body["max_tokens"] = _ANTHROPIC_DEFAULT_MAX_TOKENS
        logger.info(
            "POST /call-manage/chat/completions -> Anthropic 原生端点缺 max_tokens，注入默认 %d model=%s",
            _ANTHROPIC_DEFAULT_MAX_TOKENS, model_name,
        )

    # ── 思考参数注入 ──
    if think_enabled is not None:
        _apply_thinking_params(body, model_name, selected.com_url, think_enabled)
    elif _glm_forced_thinking(model_name):
        _apply_thinking_params(body, model_name, selected.com_url, False)

    # ── Claude 思考模式预算收敛 ──
    if "claude" in model_name.lower():
        thinking = body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            max_tokens = body.get("max_tokens")
            if isinstance(max_tokens, int) and max_tokens <= _CLAUDE_THINKING_BUDGET:
                budget = max_tokens - 1024
                if budget >= 1024:
                    thinking["budget_tokens"] = budget
                    logger.info(
                        "POST /call-manage/chat/completions -> Claude 思考 budget 收敛为 %d（max_tokens=%s）",
                        budget, max_tokens,
                    )
                else:
                    thinking["type"] = "disabled"
                    logger.warning(
                        "POST /call-manage/chat/completions -> max_tokens=%s 不足以容纳 Claude 思考最低预算(1024)，已关闭思考",
                        max_tokens,
                    )

    # 提供商路由参数注入（OpenRouter）
    _apply_provider_params(body, selected)

    # Gemini 安全阈值注入
    _apply_gemini_safety(body, model_name)

    is_stream = body.get("stream", False)

    logger.info(
        "POST /call-manage/chat/completions -> [%s] model=%s stream=%s "
        "selected=%s(%s) url=%s body_model=%r",
        request_id, model_name, is_stream, selected.ai_model_name, selected.com_name,
        forward_url, body.get("model"),
    )

    if is_anthropic_native:
        fwd_headers = {
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
    else:
        fwd_headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    if DEBUG:
        logger.info(
            "POST /call-manage/chat/completions -> 发给上游的请求报文:\n%s",
            json.dumps(body, ensure_ascii=False, indent=2),
        )

    return {
        "body": body,
        "forward_url": forward_url,
        "fwd_headers": fwd_headers,
        "model_name": model_name,
        "is_stream": is_stream,
        "is_anthropic_native": is_anthropic_native,
    }


def _validate_url(url: str) -> str:
    """校验转发目标 URL，防止 SSRF.

    要求 https 协议，拒绝私有/回环地址.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="仅支持 HTTPS 转发目标")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise HTTPException(status_code=400, detail="无效的转发目标 URL")
    # 拒绝回环地址
    if hostname in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
        raise HTTPException(status_code=400, detail="不允许的转发目标")
    # 拒绝私有网络地址
    if hostname.startswith(("10.", "172.16.", "192.168.")):
        raise HTTPException(status_code=400, detail="不允许的转发目标")
    return url


def _is_anthropic_native_url(url: str) -> bool:
    """转发目标是否为 Anthropic 原生 ``/v1/messages`` 端点（ofox 等兼容网关）.

    原生协议（doc/test.txt）与 OpenAI ``/chat/completions`` 的差异，由调用方分别处理：
      · 认证头用 ``x-api-key``（非 Bearer），且必须带 ``anthropic-version``
      · ``max_tokens`` 必填，缺了上游直接 400
      · SSE 事件为 message_start / content_block_delta / message_delta 一族（见 _ingest_anthropic_sse_data）

    仅按 URL 路径判定：DB 须存完整端点 ``…/v1/messages`` 或基址 ``…/anthropic``
    （构造转发 URL 时补 ``/v1/messages``）；裸域名（如 https://api.anthropic.com）不会命中。
    """
    path = urlparse(url).path.rstrip("/").lower()
    return path.endswith("/messages")


def _apply_anthropic_native_body(body: dict) -> None:
    """Anthropic 原生协议的请求体适配（ofox /v1/messages 等原生端点）.

    OpenAI 格式允许 ``role: "system"`` 混在 messages 里（本平台客户端即按此发送），
    原生协议没有这个角色——system 必须提为顶层 ``system`` 字段，留在 messages 里
    上游直接 400。其余字段（model/max_tokens/stream/thinking/temperature）两协议
    同名同义，原样透传。
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return
    system_parts: list[str] = []
    kept: list[Any] = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                # content blocks 形态：只拼其中的 text 块
                system_parts.append("".join(
                    blk.get("text", "") for blk in content if isinstance(blk, dict)
                ))
            continue
        kept.append(msg)
    if not system_parts:
        return
    body["messages"] = kept
    hoisted = "\n".join(p for p in system_parts if p)
    prev = body.get("system")
    if isinstance(prev, str) and prev:
        hoisted = f"{prev}\n{hoisted}"
    body["system"] = hoisted
    logger.info(
        "POST /call-manage/chat/completions -> Anthropic 原生适配: %d 条 system 消息提为顶层 system 字段",
        len(system_parts),
    )


# ── 思考模式参数注入 ──
def _apply_thinking_params(body: dict, model_name: str, com_url: str, enabled: bool) -> None:
    """按选中模型/运营商的参数规范，注入或移除请求体中的思考参数.

    与客户端 CallModelStreamService 的厂商分支保持一致的参数格式：
    - OpenRouter 网关: 用官方 ``reasoning.enabled`` 布尔值统一控制（``{"enabled": true|false}``），
      开启时额外注入 ``reasoning.effort = "low"``；
      经 OpenRouter 的推理模型共用这一格式，不按模型名逐个特判（GLM/MiniMax 除外，见下）；
      非推理模型传该参数会被网关忽略，同时清理旧格式 ``extra_body.thinking`` 避免冲突
    - DeepSeek 直连 / MiMo 官方 API（api.xiaomimimo.com）: ``thinking = {"type": "enabled"|"disabled"}``；
      其中 DeepSeek 开启时额外注入 ``reasoning_effort = "low"``（参考 doc/temp.txt）
    - Claude: 开启用 ``thinking = {"type": "enabled", "budget_tokens": N}``；
      关闭即移除字段（无 ``type=disabled`` 语义）
    - Qwen: ``enable_thinking`` + ``chat_template_kwargs.enable_thinking``
    - GLM（智谱直连）: ``thinking = {"type": "enabled"|"disabled"}``（与 DeepSeek 同格式）；
      思考开启时默认注入 ``reasoning_effort = "low"``（智谱官方思考强度档位 low/high/max，
      模型默认 max 最费 token，客户端已显式传入则保留客户端档位），关闭时移除该参数。
      **但 GLM-5.3/flash/flashx 是强制思考型号**：模型层面不允许关闭，传 ``disabled`` 官方返 400、
      聚合网关则静默忽略（模型仍按默认 max 思考）。故这类型号恒发 ``enabled`` + 压到 low，
      把「关」降级为「开着但最低档」；``reasoning_effort`` 仅 GLM-5.2+ 支持，低版本不发
      **MiniMax-M3.1+ 同属强制思考型号**，参数格式与 GLM 一致，走同一分支
      （判定复用 _glm_thinking_plan / _glm_forced_thinking，仅版本门槛数值不同）
    - 其余模型族（GPT/o系列、Grok 等）无可控思考参数 → 不处理，保持原样
    """
    name = (model_name or "").lower()
    url = (com_url or "").lower()

    if "openrouter" in url:
        # OpenRouter 是统一网关：推理模型都用官方 reasoning.enabled 布尔值控制，
        # 不按模型名逐个特判；非推理模型传该参数会被网关忽略，无副作用。
        # **但 GLM/MiniMax 例外**：它有强制思考型号（关不掉，发 enabled=false 会被静默忽略并让模型
        # 按默认 max 跑）与独立强度档，故先过一遍 _glm_thinking_plan——否则同一个静默 max
        # 故障只是从智谱直连搬到了 OpenRouter 路由上（2026-10-02 两仓同修的教训）。
        prev_reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), dict) else {}
        prev_effort = prev_reasoning.get("effort")
        glm_plan = _glm_thinking_plan(name, enabled)
        if glm_plan is None:
            body["reasoning"] = {"enabled": enabled}
            # 思考模式开启时控制思考深度（OpenRouter 官方 reasoning.effort 格式）
            if enabled:
                body["reasoning"]["effort"] = _GLM_THINKING_EFFORT
        else:
            think_on, effort = glm_plan
            if effort:
                # GLM 5.2+ 有强度档：**只发 effort 不带 enabled**——OpenRouter 官方推荐用
                # effort/max_tokens 控制思考，enabled 仅少数 provider 支持；2026-10-13 线上
                # enabled+low 仍被按默认 max 思考烧穿 max_tokens（docs/2.txt），优先怀疑
                # enabled 走了不支持路径。强制思考型号默认即思考，无 enabled 兜底需求。
                # 强制降级（客户端要关、型号关不掉）时不用客户端带来的档位——那多半是残留，
                # 顺着用会出现「客户端以为关了、实际按 high/max 烧」的最坏组合；其余保留其档位
                clamped = think_on and not enabled
                body["reasoning"] = {"effort": _GLM_THINKING_EFFORT if clamped else (prev_effort or effort)}
            else:
                # 无强度档的老 GLM（5.2-）：关得掉，enabled 布尔是唯一开关，保持
                body["reasoning"] = {"enabled": think_on}
        # 清理旧格式 extra_body.thinking，避免与官方 reasoning 参数冲突
        extra_body = body.get("extra_body")
        if isinstance(extra_body, dict):
            extra_body.pop("thinking", None)
            if not extra_body:
                body.pop("extra_body", None)
        return

    # DeepSeek 原生接口与 MiMo 官方 API 格式一致，均用 thinking.type 控制
    if "deepseek" in name or "mimo" in name or "xiaomimimo" in url:
        body["thinking"] = {"type": "enabled" if enabled else "disabled"}
        # DeepSeek 思考模式额外支持 reasoning_effort 控制思考深度（参考 doc/temp.txt）
        if "deepseek" in name:
            if enabled:
                body["reasoning_effort"] = "low"
            else:
                body.pop("reasoning_effort", None)
    elif "claude" in name:
        if enabled:
            body["thinking"] = {"type": "enabled", "budget_tokens": _CLAUDE_THINKING_BUDGET}
            # Anthropic 硬约束：思考开启时 temperature 必须为 1，top_p/top_k 不可携带，
            # 违反即上游 400（ofox 等原生网关同约束）。客户端带来的值一律压掉。
            body["temperature"] = 1
            body.pop("top_p", None)
            body.pop("top_k", None)
        else:
            body.pop("thinking", None)
    elif "qwen" in name:
        body["enable_thinking"] = enabled
        body["chat_template_kwargs"] = {"enable_thinking": enabled}
    elif "glm" in name or "bigmodel" in url or "zhipu" in url or _is_minimax(name):
        # GLM（智谱）/ MiniMax：thinking.type 与 DeepSeek 同格式。但 GLM-5.3 / 5.3-flash / 5.3-flashx
        # 是**强制思考**型号（含 MiniMax-M3.1）——模型层面不允许关闭（官方：传 disabled 返回 400，错误码 1210
        # 「该模型始终思考，请使用 low、high 或 max」）。实测经聚合网关（ofox 等）时 disabled
        # 会被静默**忽略**：请求通（200），但模型按**默认 max** 强度一路思考——客户端以为自己
        # 关掉了，实际拿到最贵最慢那一档，且报文里没有任何强度参数可查。
        # 故对强制思考型号恒发 enabled，把「关」降级为「开着但压到最低档」，而不是发一个无效的 disabled。
        glm_ver = _glm_version(name)
        forced_thinking = _glm_forced_thinking(name)
        effort_min = _thinking_gates(name)[0]
        if enabled or forced_thinking:
            body["thinking"] = {"type": "enabled"}
            # 思考强度：官方独立参数（GLM/MiniMax） reasoning_effort（low/high/max，模型默认 max），
            # 与 thinking.type 开关分离。默认压到 low 省 token；客户端已显式传入则保留其档位。
            # 仅 GLM-5.2+ / MiniMax-3.1+ 认该参数（低版本传了报错），故按版本门禁。
            if glm_ver is not None and glm_ver >= effort_min:
                if forced_thinking and not enabled:
                    # 强制降级（客户端要关、型号关不掉）：不用客户端带来的档位，最低档处理
                    body["reasoning_effort"] = _GLM_THINKING_EFFORT
                else:
                    body.setdefault("reasoning_effort", _GLM_THINKING_EFFORT)
        else:
            body["thinking"] = {"type": "disabled"}
            body.pop("reasoning_effort", None)


# ── 提供商路由参数注入 ──
def _apply_provider_params(body: dict, selected) -> None:
    """将 model_manage.html 维护的提供商参数（extra_config.provider）注入 OpenRouter 请求.

    仅缓存一个事实：OpenRouter 将路由参数放在请求体**顶层** ``provider`` 键
    （参考 temp/temp.txt），与 deepseek 的 ``extra_body.thinking`` 不是一回事。
    仅在运营商为 OpenRouter 且配置了 provider 时注入；
    客户端已显式传 ``body["provider"]`` 时保留客户端值，避免覆盖业务侧显式指定的路由。
    """
    url = (selected.com_url or "").lower()
    com_name = (selected.com_name or "").lower()
    if "openrouter" not in url and "openrouter" not in com_name:
        return
    if not selected.extra_config:
        return
    try:
        extra = json.loads(selected.extra_config)
    except (json.JSONDecodeError, TypeError):
        extra = None
    provider = (extra or {}).get("provider")
    if not isinstance(provider, dict) or not provider:
        return
    if "provider" in body:
        logger.info(
            "OpenRouter provider 参数：客户端已显式传入，跳过注入 body.provider=%s",
            json.dumps(body["provider"], ensure_ascii=False),
        )
        return
    body["provider"] = provider
    logger.info(
        "注入 OpenRouter provider 参数: %s",
        json.dumps(provider, ensure_ascii=False),
    )


# ── Gemini 安全阈值（_apply_gemini_safety 注入 extra_body.safety_settings）──
_GEMINI_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


# ── Gemini 安全阈值注入 ──
def _apply_gemini_safety(body: dict, model_name: str) -> None:
    """Gemini 系模型关闭安全拦截：extra_body.safety_settings 四类全部 BLOCK_NONE.

    与客户端 CallModelStreamService 的 Gemini 分支保持一致（extra_body 字面键，
    经 OpenRouter 透传）。合并不覆盖：客户端已显式传 extra_body.safety_settings
    时保留客户端值；extra_body 已存在（如其他参数）时只补 safety_settings 键。
    """
    if "gemini" not in (model_name or "").lower():
        return
    extra_body = body.get("extra_body")
    if not isinstance(extra_body, dict):
        if extra_body is not None:
            logger.warning(
                "Gemini safety_settings：客户端 extra_body 非对象(%r)，丢弃后注入", extra_body,
            )
        extra_body = {}
        body["extra_body"] = extra_body
    if "safety_settings" in extra_body:
        logger.info(
            "Gemini safety_settings：客户端已显式传入，跳过注入 model=%s", model_name,
        )
        return
    # 深拷贝注入：常量是模块级共享对象，本文件惯例是原地改 body，
    # 引用共享会让未来任何原地修改跨请求串状态
    extra_body["safety_settings"] = [dict(s) for s in _GEMINI_SAFETY_SETTINGS]
    logger.info("注入 Gemini safety_settings(BLOCK_NONE): model=%s", model_name)


# ── httpx 安全关闭 ──
async def _close_safely(obj: Optional[object]) -> None:
    """安全关闭 httpx 客户端或响应，避免 finally 中异常覆盖原始错误."""
    if obj is not None and hasattr(obj, "aclose"):
        try:
            await obj.aclose()
        except Exception as exc:
            logger.debug("关闭 httpx 资源时出现异常: %s", exc)


async def _stream_aiter_bytes(resp: httpx.Response) -> AsyncGenerator[bytes, None]:
    """将 httpx 流式响应逐块 yield 为 bytes（SSE 透传）."""
    async for chunk in resp.aiter_bytes():
        yield chunk


def _ingest_sse_chunk(
    buf: bytes,
    accumulated_content: list[str],
    accumulated_reasoning: list[str],
    accumulated_usage: dict[str, Any],
    flush: bool = False,
) -> bytes:
    """消费 ``buf`` 里的**完整** SSE 行并累加，返回残余的半行（留给下次拼接）。

    **必须按行缓冲，不能逐 chunk 独立切行**：``aiter_bytes`` 吐的是任意网络分片，
    边界与 SSE 行边界无关——一条 ``data: {...}`` 经常被切成两块，逐 chunk
    ``decode().split("\\n")`` 会让两个半行都不以 ``data: `` 开头，**整条 delta 被静默
    丢弃**；流尾没有换行收尾的末行同样永远等不到处理。

    实测：长正文会随机缺字（甚至语义反转），且正文在句中断掉；同一次请求的思维链
    同样缺字——两条流共用一个坏的解析，并非模型问题。

    ``flush=True`` 时把残余当完整行处理（流结束时调用，末帧常无尾随 ``\\n``）。
    """
    if b"\n" in buf:
        *lines, buf = buf.split(b"\n")
    elif not flush:
        return buf
    else:
        lines, buf = [buf], b""
    for raw in lines:
        line = raw.decode("utf-8", errors="replace")
        if not (line.startswith("data: ") and line[6:] != "[DONE]"):
            continue
        try:
            data = json.loads(line[6:])
            # Anthropic 原生事件（ofox /v1/messages 一族）先按 type 分流——必须先于下方
            # 通用 usage 提取：message_delta 的顶层 usage 只有 output_tokens，落进通用
            # 提取会把 prompt_tokens 覆写为 0，冲掉 message_start 已记下的 input_tokens
            if data.get("type") in _ANTHROPIC_SSE_EVENT_TYPES:
                _ingest_anthropic_sse_data(
                    data, accumulated_content, accumulated_reasoning, accumulated_usage,
                )
                continue
            # 提取 usage（若有）：必须放在 choices 处理之前——
            # DeepSeek 等供应商的末帧 usage 带空 choices []，若先取 choices[0] 会 IndexError，
            # 导致 usage 提取被 except 吞掉，流式 token 记录丢失
            usage = data.get("usage")
            if usage and isinstance(usage, dict):
                accumulated_usage["prompt_tokens"] = usage.get("prompt_tokens", 0)
                accumulated_usage["completion_tokens"] = usage.get("completion_tokens", 0)
                accumulated_usage["total_tokens"] = usage.get("total_tokens", 0)
                accumulated_usage["prompt_cache_hit_tokens"] = usage.get("prompt_cache_hit_tokens", 0)
                accumulated_usage["prompt_cache_miss_tokens"] = usage.get("prompt_cache_miss_tokens", 0)
                if usage.get("cost") is not None:
                    accumulated_usage["cost"] = usage.get("cost")
            # 正文 delta：仅当 choices 非空时提取
            choices = data.get("choices")
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                if content:
                    accumulated_content.append(content)
                # 思维链字段：DeepSeek 用 reasoning_content，OpenRouter 用 reasoning
                reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if reasoning and isinstance(reasoning, str):
                    accumulated_reasoning.append(reasoning)
        except (json.JSONDecodeError, KeyError, IndexError):
            pass
    return buf


def _ingest_anthropic_sse_data(
    data: dict,
    accumulated_content: list[str],
    accumulated_reasoning: list[str],
    accumulated_usage: dict[str, int],
) -> None:
    """消费 Anthropic 原生 SSE 事件（ofox /v1/messages 等），累加正文/思维链/usage.

    与 OpenAI chunk 的字段差异：
      · 正文在 content_block_delta.delta.text，思维链在 thinking_delta.delta.thinking
      · usage 拆两帧：message_start.message.usage.input_tokens（入参）、
        message_delta.usage.output_tokens（出参终值），total 两帧各自求和
      · 流内 error 事件以 HTTP 200 到达，只记日志（原始事件已透传给客户端自行处理）
    缓存字段沿用 DeepSeek 口径换算：cache_read → hit、cache_creation → miss。
    """
    etype = data.get("type")
    if etype == "content_block_delta":
        delta = data.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta" and delta.get("text"):
            accumulated_content.append(delta["text"])
        elif dtype == "thinking_delta" and delta.get("thinking"):
            accumulated_reasoning.append(delta["thinking"])
    elif etype == "message_start":
        usage = ((data.get("message") or {}).get("usage") or {})
        if usage.get("input_tokens") is not None:
            cache_read = usage.get("cache_read_input_tokens") or 0
            cache_creation = usage.get("cache_creation_input_tokens") or 0
            # Anthropic 的 input_tokens 不含缓存部分；对齐 DeepSeek 口径——
            # prompt_tokens = 未命中 + 命中 + 新建，hit/miss 作为其子集保留
            accumulated_usage["prompt_tokens"] = usage.get("input_tokens", 0) + cache_read + cache_creation
            accumulated_usage["completion_tokens"] = accumulated_usage.get("completion_tokens", 0)
            accumulated_usage["prompt_cache_hit_tokens"] = cache_read
            accumulated_usage["prompt_cache_miss_tokens"] = cache_creation
            accumulated_usage["total_tokens"] = (
                accumulated_usage["prompt_tokens"] + accumulated_usage["completion_tokens"]
            )
    elif etype == "message_delta":
        usage = data.get("usage") or {}
        if usage.get("output_tokens") is not None:
            accumulated_usage["completion_tokens"] = usage.get("output_tokens", 0)
            accumulated_usage["prompt_tokens"] = accumulated_usage.get("prompt_tokens", 0)
            accumulated_usage["total_tokens"] = (
                accumulated_usage["prompt_tokens"] + accumulated_usage["completion_tokens"]
            )
    elif etype == "error":
        err = data.get("error") or {}
        logger.error(
            "Anthropic 原生流内 error 事件: type=%s message=%s", err.get("type"), err.get("message"),
        )


# ── 共享的模型拉取逻辑（call-manage 和 model-manage 共用） ──

async def _fetch_models_from_upstream(com_url: str, api_key: str) -> tuple[list[str], str]:
    """从上游 API 拉取可用模型列表.

    Args:
        com_url: 运营商 API 基础地址.
        api_key: API Key.

    Returns:
        (模型 ID 列表, 错误消息). 成功时错误消息为空字符串.
    """
    base_url = com_url.rstrip("/")
    # 所有调用方（含公开的 /v1/models 别名）都先做 SSRF 校验
    _validate_url(f"{base_url}/models")
    models: list[str] = []
    candidates = [
        f"{base_url}/v1/models",
        f"{base_url}/models",
        f"{base_url}/api/v1/models",
    ]
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    last_error = ""

    for url in candidates:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}"
                    continue
                data = resp.json()
                raw_list = data.get("data", [])
                if isinstance(raw_list, list):
                    for item in raw_list:
                        if isinstance(item, dict) and "id" in item:
                            models.append(item["id"])
                        elif isinstance(item, str):
                            models.append(item)
                    break
                last_error = "响应格式不匹配"
        except httpx.TimeoutException:
            last_error = "请求超时"
            continue
        except Exception as e:
            last_error = str(e)
            continue

    return models, "" if models else last_error


# ── 共享的 Pydantic 模型 ──
class FetchModelsBody(BaseModel):
    com_url: str = Field(..., min_length=1, description="运营商 API 地址")
    api_key: str = Field(..., min_length=1, description="API Key")


# ── LLM 转发端点 ──

@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
    x_async_type: Optional[str] = Header(None, alias="X-Async-Type"),
    x_purpose: Optional[str] = Header(None, alias="X-Purpose"),
    x_response_format: Optional[str] = Header(None, alias="X-Response-Format"),
    x_thinking: Optional[str] = Header(None, alias="X-Thinking"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_feature_code: Optional[str] = Header(None, alias="X-Feature-Code"),
    x_generation_id: Optional[str] = Header(None, alias="X-Generation-Id"),
):
    """转发聊天补全请求到配置的 LLM 模型（从数据库 model_manage_info 表选取）.

    Headers:
        Authorization: 可选的 Bearer token（主 LLM 模式下可覆盖 DB 中的 api_key）.
        X-Async-Type: 集群类型 "async1".."async9"（对应管理页集群1..集群9），
                      服务端在该集群内按轮询选择模型。缺省兼容回落：
                      主链 "async1"、异步链 "async2".
        X-Purpose: 用途标识，默认 "main"；以 "async" 开头时按异步链处理
                   （强制使用服务端 API Key）。仅用于异步识别、日志与 token 用量记账，
                   不影响集群选择。
        X-Thinking: 思考模式开关（"1"/"true" 开启，"0"/"false" 关闭）.
                    仅在模型「思考模式」为跟随请求时生效；模型配置为强制开/关时以模型为准.
        X-Response-Format: "json_object" 时按实际模型注入 response_format.
        X-User-Id: 调用方标识，用于 token 用量与调用记录统计；缺省视为匿名.
        X-Feature-Code: 功能码（如 role_reply），仅用于用量分组统计；
                        不传或非法时记为 uncategorized.
        X-Generation-Id: 生成ID。同一次 AI 生成触发的所有调用共用该值，
                        钻石计费按它聚合本次生成的全部成本后一次性扣钻.

    请求体: 标准 OpenAI chat completions 格式。模型配置了 temperature / top_p / top_k /
            max_tokens 时服务端会覆盖同名请求参数，未配置（NULL）则保留客户端值.
    """
    t0 = time.perf_counter()
    request_id = getattr(request.state, "request_id", "") or uuid.uuid4().hex
    purpose_header = x_purpose
    async_type_header = x_async_type
    response_format_header = x_response_format
    thinking_header = x_thinking
    user_id = (x_user_id or "").strip()
    feature_code = _normalize_feature_code(x_feature_code)
    generation_id = (x_generation_id or "").strip()[:64]

    # ── 网关 Token / 项目配额 ──
    # sk-gw- 前缀一律按网关 Token 校验；即使未强制开启，非法 Token 也不能回落成上游 Key。
    raw_token = _extract_bearer(authorization)
    identity = None
    project = None
    project_id = ""
    token_id = ""
    user_policy = None
    if raw_token.startswith(project_manage_service.TOKEN_PREFIX):
        try:
            identity = await project_manage_service.resolve_gateway_token(db, raw_token)
        except project_manage_service.GatewayAuthError as e:
            logger.warning("[%s] 网关 Token 校验失败: %s", request_id, e.code)
            return gateway_error_response(e.status_code, e.message, code=e.code)

        project = identity.project
        project_id = project.project_id
        token_id = identity.token_id
        if not user_id:
            user_id = f"gw:{token_id}"
        # 用户级封禁对项目 Token 调用同样生效，放在项目配额之前；
        # 显式 X-User-Id 与 gw:<token_id> 任一被封禁都拦截。
        # 安全边界：X-User-Id 由内部调用方（StarggleCom）传入，网关不应直接暴露公网。
        user_id = user_quota_service.normalize_user_id(user_id)
        token_user_id = user_quota_service.normalize_user_id(f"gw:{token_id}")
        user_policy = await user_quota_service.get_quota_policy(db, user_id)
        blocked = user_quota_service.is_blocked(user_policy)
        if not blocked and token_user_id != user_id:
            token_policy = await user_quota_service.get_quota_policy(db, token_user_id)
            blocked = user_quota_service.is_blocked(token_policy)
        if blocked:
            logger.warning("[%s] 用户已封禁: user=%s", request_id, user_id)
            return gateway_error_response(
                403, "User is blocked", error_type="permission_error", code="user_blocked",
            )
        try:
            snapshot = await project_manage_service.get_usage_snapshot(db, project_id)
            project_manage_service.check_project_quota(project, snapshot)
        except project_manage_service.ProjectQuotaError as e:
            logger.warning(
                "[%s] 项目配额超限: project=%s code=%s", request_id, project_id, e.code,
            )
            return gateway_error_response(
                429, e.message, error_type="rate_limit_error", code=e.code,
            )
    elif project_manage_service.GATEWAY_TOKEN_REQUIRED:
        return gateway_error_response(401, "Missing API key", code="missing_api_key")
    elif user_id:
        # 普通调用：按调用方传入的 X-User-Id 校验封禁
        user_id = user_quota_service.normalize_user_id(user_id)
        user_policy = await user_quota_service.get_quota_policy(db, user_id)
        if user_quota_service.is_blocked(user_policy):
            logger.warning("[%s] 用户已封禁: user=%s", request_id, user_id)
            return gateway_error_response(
                403, "User is blocked", error_type="permission_error", code="user_blocked",
            )

    # ── 用户级 Token 配额（软限额，NULL=不限、0=禁止） ──
    if user_quota_service.has_token_limits(user_policy):
        usage_snapshot = await user_quota_service.get_user_usage_snapshot(db, user_id)
        try:
            user_quota_service.check_user_token_limits(user_policy, usage_snapshot)
        except user_quota_service.UserTokenQuotaError as e:
            logger.warning(
                "[%s] 用户Token配额超限: user=%s code=%s", request_id, user_id, e.code,
            )
            return gateway_error_response(
                429, e.message, error_type="rate_limit_error", code=e.code,
            )

    llm_purpose = purpose_header or "main"
    # 任何以 async 开头的 purpose 都按异步计（如 async、async:summary 等），
    # 防止自定义 purpose 串到主模型路由使用客户端 Authorization key
    is_async = llm_purpose.startswith("async")

    # ── 集群类型：请求头是模型池的唯一选择依据 ──
    async_type = (async_type_header or "").strip().lower()
    if async_type and async_type not in call_manage_service.VALID_ASYNC_TYPES:
        raise HTTPException(
            status_code=400,
            detail="非法 X-Async-Type，可选值：async1..async9",
        )
    if not async_type:
        # 未指定集群时按用途回落：主链→集群1，异步链→集群2
        async_type = "async2" if is_async else "async1"
        logger.info(
            "POST /call-manage/chat/completions -> 未提供 X-Async-Type，按 purpose=%s 回落 %s",
            llm_purpose, async_type,
        )

    # 计费口径（2026-11-16）：会员日调用次数限额已下线，改为按「一次生成」聚合扣钻石。
    # 预检（余额为负则拦截）与结算由 /billing/precheck、/billing/settle 负责，
    # 本端点只负责把 X-Generation-Id 随用量落库，不在这里扣费。

    # ── 解析原始请求体（每次重试都从这份 body 深拷贝）──
    try:
        original_body = await request.json()
    except Exception:
        return gateway_error_response(400, "Invalid JSON body", code="invalid_json")
    if not isinstance(original_body, dict):
        return gateway_error_response(400, "Request body must be a JSON object", code="invalid_json")
    requested_stream = bool(original_body.get("stream", False))
    client_model_preview = str(original_body.get("model") or "").strip()

    # ── 从数据库获取所选集群的可用模型并轮询 ──
    models = await call_manage_service.get_active_models(db, async_type=async_type)
    if identity is not None and client_model_preview:
        # 客户端显式指定模型时按该名称校验；候选渠道仍需保留，便于同集群重试
        if not project_manage_service.model_allowed(identity, client_model_preview):
            logger.warning(
                "[%s] Token 不允许使用模型: token=%s model=%s",
                request_id, token_id, client_model_preview,
            )
            return gateway_error_response(
                403, f"API key is not allowed to use model {client_model_preview}",
                code="model_not_allowed",
            )
    elif identity is not None:
        models = project_manage_service.filter_allowed_models(identity, models)
        if not models:
            logger.warning(
                "[%s] Token 不允许使用集群 %s 中的任何模型: token=%s",
                request_id, async_type, token_id,
            )
            return gateway_error_response(
                403,
                f"API key is not allowed to use any model in cluster {async_type}",
                code="model_not_allowed",
            )
    if not models:
        logger.error("集群 %s 没有可用的 %s 模型（is_using=1）", async_type, llm_purpose)
        raise HTTPException(
            status_code=503,
            detail=f"集群 {async_type} 没有可用的 LLM 模型，请检查 model_manage_info 表配置",
        )

    candidates = await health_manage_service.plan_candidates(db, models, async_type)
    if not candidates:
        candidates = [models[0]]
    selected = candidates[0]
    routing_config = await health_manage_service.get_routing_config(db, async_type)

    logger.info(
        "POST /call-manage/chat/completions -> 选中模型详情: cluster=%s strategy=%s "
        "ai_model_id=%s ai_model_name=%r com_name=%s com_url=%s",
        async_type, routing_config.strategy, selected.ai_model_id, selected.ai_model_name,
        selected.com_name, selected.com_url,
    )

    project_currency = project.cost_currency if project is not None else "USD"
    token_last_used_at = identity.last_used_at if identity is not None else None

    # ── 流式转发：先完成重试，再把已建立的上游响应交给 StreamingResponse ──
    if requested_stream:
        last_error = "All upstream channels failed"
        for attempt_index, selected in enumerate(candidates):
            plan = _prepare_forward_plan(
                original_body, selected,
                is_async=is_async, identity=identity, raw_token=raw_token,
                response_format_header=response_format_header,
                thinking_header=thinking_header,
                request_id=request_id,
            )
            plan["body"] = _replace_core_prompts(plan["body"])
            model_name = plan["model_name"]
            if identity is not None and not project_manage_service.model_allowed(identity, model_name):
                logger.warning(
                    "[%s] Token 不允许使用模型: token=%s model=%s",
                    request_id, token_id, model_name,
                )
                return gateway_error_response(
                    403, f"API key is not allowed to use model {model_name}",
                    code="model_not_allowed",
                )

            attempt_start = time.perf_counter()
            stream_client: Optional[httpx.AsyncClient] = None
            stream_resp: Optional[httpx.Response] = None
            try:
                stream_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, read=60.0))
                stream_resp = await stream_client.send(
                    stream_client.build_request(
                        "POST", plan["forward_url"], json=plan["body"],
                        headers=plan["fwd_headers"],
                    ),
                    stream=True,
                )
                ttfb_ms = (time.perf_counter() - attempt_start) * 1000
                logger.info(
                    "POST /call-manage/chat/completions <- %d model=%s stream ttfb=%.0fms attempt=%d/%d",
                    stream_resp.status_code, model_name, ttfb_ms,
                    attempt_index + 1, len(candidates),
                )
            except Exception as e:
                elapsed = (time.perf_counter() - attempt_start) * 1000
                err_desc = " ".join(f"{type(e).__name__}: {e}".split())
                logger.error(
                    "POST /call-manage/chat/completions <- STREAM CONNECT ERROR model=%s "
                    "attempt=%d/%d error=%s",
                    model_name, attempt_index + 1, len(candidates), err_desc,
                )
                await _close_safely(stream_resp)
                await _close_safely(stream_client)
                await health_manage_service.record_failure(
                    db, selected.ai_model_id, elapsed, type(e).__name__,
                    failure_threshold=routing_config.failure_threshold,
                    cooldown_seconds=routing_config.cooldown_seconds,
                )
                last_error = f"流式转发失败: {err_desc}"
                if attempt_index + 1 < len(candidates):
                    continue
                return gateway_error_response(502, last_error)

            if stream_resp.status_code >= 400:
                error_body = await stream_resp.aread()
                error_text = error_body.decode("utf-8", errors="replace")
                logger.error(
                    "POST /call-manage/chat/completions <- %d model=%s stream_error body=%s",
                    stream_resp.status_code, model_name, error_text,
                )
                if _counts_as_channel_failure(stream_resp.status_code):
                    await health_manage_service.record_failure(
                        db, selected.ai_model_id, ttfb_ms,
                        f"http_{stream_resp.status_code}",
                        failure_threshold=routing_config.failure_threshold,
                        cooldown_seconds=routing_config.cooldown_seconds,
                    )
                await _close_safely(stream_resp)
                await _close_safely(stream_client)
                last_error = f"上游返回错误 (HTTP {stream_resp.status_code}): {error_text[:500]}"
                if _is_retryable_status(stream_resp.status_code) and attempt_index + 1 < len(candidates):
                    logger.warning(
                        "POST /call-manage/chat/completions -> 流式请求重试下一个渠道: "
                        "from=%s status=%d attempt=%d/%d",
                        selected.ai_model_id, stream_resp.status_code,
                        attempt_index + 1, len(candidates),
                    )
                    continue
                return gateway_error_response(502, last_error)

            await health_manage_service.record_success(db, selected.ai_model_id, ttfb_ms)
            stream_started_at = time.perf_counter()

            async def event_generator(
                    _client=stream_client,
                    _resp=stream_resp,
                    _selected=selected,
                    _model_name=model_name,
                    _attempt_index=attempt_index,
                    _started=stream_started_at,
            ) -> AsyncGenerator[bytes, None]:
                upstream_ok = False
                total_bytes = 0
                accumulated_content: list[str] = []
                accumulated_reasoning: list[str] = []
                accumulated_usage: dict[str, Any] = {}
                try:
                    upstream_ok = True
                    sse_buf = b""
                    async for chunk in _stream_aiter_bytes(_resp):
                        total_bytes += len(chunk)
                        sse_buf = _ingest_sse_chunk(
                            sse_buf + chunk,
                            accumulated_content,
                            accumulated_reasoning,
                            accumulated_usage,
                        )
                        yield chunk
                    if sse_buf:
                        _ingest_sse_chunk(
                            sse_buf,
                            accumulated_content,
                            accumulated_reasoning,
                            accumulated_usage,
                            flush=True,
                        )
                except Exception as e:
                    elapsed = (time.perf_counter() - _started) * 1000
                    err_desc = " ".join(f"{type(e).__name__}: {e}".split())
                    logger.error(
                        "POST /call-manage/chat/completions <- STREAM ERROR model=%s elapsed=%.0fms error=%s",
                        _model_name, elapsed, err_desc,
                    )
                    await health_manage_service.record_failure(
                        db, _selected.ai_model_id, elapsed, type(e).__name__,
                        failure_threshold=routing_config.failure_threshold,
                        cooldown_seconds=routing_config.cooldown_seconds,
                    )
                    yield f"data: [ERROR] 流式转发失败: {err_desc}\n\n".encode("utf-8")
                finally:
                    await _close_safely(_resp)
                    await _close_safely(_client)
                    elapsed = (time.perf_counter() - t0) * 1000
                    content_text = "".join(accumulated_content)
                    reasoning_text = "".join(accumulated_reasoning)
                    usage_info = ""
                    if accumulated_usage:
                        usage_info = (
                            f" usage[prompt={accumulated_usage.get('prompt_tokens', 0)} "
                            f"completion={accumulated_usage.get('completion_tokens', 0)} "
                            f"total={accumulated_usage.get('total_tokens', 0)}]"
                        )
                    logger.info(
                        "POST /call-manage/chat/completions <- STREAM DONE model=%s "
                        "elapsed=%.0fms total_bytes=%d content_chars=%d reasoning_chars=%d%s",
                        _model_name, elapsed, total_bytes, len(content_text),
                        len(reasoning_text), usage_info,
                    )
                    if DEBUG and content_text:
                        logger.info(
                            "POST /call-manage/chat/completions <- STREAM 完整返回内容 model=%s:\n%s",
                            _model_name, content_text,
                        )
                    if DEBUG and reasoning_text:
                        logger.info(
                            "POST /call-manage/chat/completions <- STREAM 思维链 model=%s:\n%s",
                            _model_name, reasoning_text,
                        )

                    if user_id and upstream_ok:
                        # 独立后台任务落盘：客户端断开导致生成器被取消时仍能记账
                        call_manage_service.schedule_background(
                            call_manage_service.record_stream_usage_background(
                                user_id=user_id,
                                model=_model_name,
                                purpose=llm_purpose,
                                usage=accumulated_usage,
                                feature_code=feature_code,
                                input_price_per_1m=_selected.input_price_per_1m,
                                output_price_per_1m=_selected.output_price_per_1m,
                                price_currency=_selected.price_currency or "USD",
                                provider_cost=accumulated_usage.get("cost"),
                                project_id=project_id,
                                project_currency=project_currency,
                                token_id=token_id,
                                token_last_used_at=token_last_used_at,
                                channel_id=_selected.ai_model_id,
                                request_id=request_id,
                                retry_count=_attempt_index,
                                generation_id=generation_id,
                            )
                        )

            return StreamingResponse(
                event_generator(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "X-Gateway-Model": model_name,
                    "X-Gateway-Cluster": async_type,
                    "X-Gateway-Retry-Count": str(attempt_index),
                    "X-Request-Id": request_id,
                },
            )

        return gateway_error_response(502, last_error)

    # ── 非流式转发：同集群内按渠道顺序重试 ──
    selected = candidates[0]
    model_name = ""
    last_error = "All upstream channels failed"
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            for attempt_index, selected in enumerate(candidates):
                plan = _prepare_forward_plan(
                    original_body, selected,
                    is_async=is_async, identity=identity, raw_token=raw_token,
                    response_format_header=response_format_header,
                    thinking_header=thinking_header,
                    request_id=request_id,
                )
                plan["body"] = _replace_core_prompts(plan["body"])
                model_name = plan["model_name"]
                if identity is not None and not project_manage_service.model_allowed(identity, model_name):
                    logger.warning(
                        "[%s] Token 不允许使用模型: token=%s model=%s",
                        request_id, token_id, model_name,
                    )
                    return gateway_error_response(
                        403, f"API key is not allowed to use model {model_name}",
                        code="model_not_allowed",
                    )

                attempt_start = time.perf_counter()
                try:
                    resp = await client.post(
                        plan["forward_url"], json=plan["body"], headers=plan["fwd_headers"],
                    )
                except Exception as e:
                    elapsed = (time.perf_counter() - attempt_start) * 1000
                    err_desc = " ".join(f"{type(e).__name__}: {e}".split())
                    logger.error(
                        "POST /call-manage/chat/completions <- ERROR model=%s elapsed=%.0fms error=%s",
                        model_name, elapsed, err_desc,
                    )
                    await health_manage_service.record_failure(
                        db, selected.ai_model_id, elapsed, type(e).__name__,
                        failure_threshold=routing_config.failure_threshold,
                        cooldown_seconds=routing_config.cooldown_seconds,
                    )
                    last_error = f"转发请求失败: {err_desc}"
                    if attempt_index + 1 < len(candidates):
                        logger.warning(
                            "POST /call-manage/chat/completions -> 请求重试下一个渠道: "
                            "from=%s attempt=%d/%d",
                            selected.ai_model_id, attempt_index + 1, len(candidates),
                        )
                        continue
                    raise HTTPException(status_code=502, detail=last_error)

                elapsed = (time.perf_counter() - attempt_start) * 1000
                resp_text = resp.text
                logger.info(
                    "POST /call-manage/chat/completions <- %d model=%s elapsed=%.0fms len=%d attempt=%d/%d",
                    resp.status_code, model_name, elapsed, len(resp_text),
                    attempt_index + 1, len(candidates),
                )
                if DEBUG:
                    logger.info(
                        "POST /call-manage/chat/completions <- 完整返回报文:\n%s", resp_text,
                    )

                if resp.status_code >= 400:
                    logger.warning(
                        "POST /call-manage/chat/completions <- upstream error %d model=%s body=%s",
                        resp.status_code, model_name, resp_text[:500],
                    )
                    if _counts_as_channel_failure(resp.status_code):
                        await health_manage_service.record_failure(
                            db, selected.ai_model_id, elapsed,
                            f"http_{resp.status_code}",
                            failure_threshold=routing_config.failure_threshold,
                            cooldown_seconds=routing_config.cooldown_seconds,
                        )
                    last_error = f"上游返回错误 (HTTP {resp.status_code}): {resp_text[:500]}"
                    if _is_retryable_status(resp.status_code) and attempt_index + 1 < len(candidates):
                        logger.warning(
                            "POST /call-manage/chat/completions -> 请求重试下一个渠道: "
                            "from=%s status=%d attempt=%d/%d",
                            selected.ai_model_id, resp.status_code,
                            attempt_index + 1, len(candidates),
                        )
                        continue
                    raise HTTPException(status_code=502, detail=last_error)

                await health_manage_service.record_success(
                    db, selected.ai_model_id, elapsed,
                )

                try:
                    data = resp.json()
                except Exception:
                    return JSONResponse(
                        {"_raw": resp_text, "status_code": resp.status_code},
                        status_code=resp.status_code,
                        headers={
                            "X-Gateway-Model": model_name,
                            "X-Gateway-Cluster": async_type,
                            "X-Gateway-Retry-Count": str(attempt_index),
                            "X-Request-Id": request_id,
                        },
                    )

                # 记录 token 用量（非关键路径，失败不影响响应）
                try:
                    usage = (data or {}).get("usage") or {}
                    provider_cost = usage.get("cost") if isinstance(usage, dict) else None
                    if usage and "prompt_tokens" not in usage and "input_tokens" in usage:
                        # Anthropic 原生 usage 字段名 → OpenAI 口径
                        cache_read = usage.get("cache_read_input_tokens") or 0
                        cache_creation = usage.get("cache_creation_input_tokens") or 0
                        prompt = usage.get("input_tokens", 0) + cache_read + cache_creation
                        usage = {
                            "prompt_tokens": prompt,
                            "completion_tokens": usage.get("output_tokens", 0),
                            "total_tokens": prompt + usage.get("output_tokens", 0),
                            "prompt_cache_hit_tokens": cache_read,
                            "prompt_cache_miss_tokens": cache_creation,
                        }
                    usage_result = await call_manage_service.record_token_usage(
                        db, user_id, model_name, llm_purpose,
                        is_stream=False, usage=usage,
                        feature_code=feature_code,
                        input_price_per_1m=selected.input_price_per_1m,
                        output_price_per_1m=selected.output_price_per_1m,
                        price_currency=selected.price_currency or "USD",
                        provider_cost=provider_cost,
                        project_id=project_id,
                        token_id=token_id,
                        channel_id=selected.ai_model_id,
                        request_id=request_id,
                        retry_count=attempt_index,
                        generation_id=generation_id,
                    )
                    if identity is not None and usage_result is not None:
                        await project_manage_service.increment_project_usage(
                            db, project_id,
                            project_currency=project_currency,
                            tokens=int(usage_result.get("totalTokens") or 0),
                            cost=usage_result.get("cost"),
                            cost_currency=usage_result.get("costCurrency"),
                        )
                        await project_manage_service.touch_token(
                            db, token_id, last_used_at=token_last_used_at,
                        )
                except Exception as e:
                    logger.warning("记录非流式 token 用量失败: user_id=%s error=%s", user_id, e)

                response.headers["X-Gateway-Model"] = model_name
                response.headers["X-Gateway-Cluster"] = async_type
                response.headers["X-Gateway-Retry-Count"] = str(attempt_index)
                response.headers["X-Request-Id"] = request_id
                return data

            raise HTTPException(status_code=502, detail=last_error)

    except HTTPException:
        raise
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        logger.error(
            "POST /call-manage/chat/completions <- ERROR model=%s elapsed=%.0fms error=%s",
            model_name, elapsed, str(e),
        )
        try:
            await health_manage_service.record_failure(
                db, selected.ai_model_id, elapsed, type(e).__name__,
                failure_threshold=routing_config.failure_threshold,
                cooldown_seconds=routing_config.cooldown_seconds,
            )
        except Exception:
            logger.exception("记录渠道失败状态失败")
        raise HTTPException(status_code=502, detail=f"转发请求失败: {str(e)}")


# ── 模型列表获取 ──

@router.get("/models")
async def list_models_openai(
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
):
    """OpenAI 兼容的模型列表（供内置聊天页和第三方客户端使用）.

    只返回使用中的文本模型。传入 ``sk-gw-`` 项目 Token 时按 Token 的
    模型白名单过滤；启用 ``GATEWAY_TOKEN_REQUIRED`` 时未传 Token 返回 401。
    同一模型名出现在多个集群时合并为一条，``x_clusters`` 列出可用集群。
    """
    raw_token = _extract_bearer(authorization)
    identity = None
    if raw_token.startswith(project_manage_service.TOKEN_PREFIX):
        try:
            identity = await project_manage_service.resolve_gateway_token(db, raw_token)
        except project_manage_service.GatewayAuthError as e:
            return gateway_error_response(e.status_code, e.message, code=e.code)
    elif project_manage_service.GATEWAY_TOKEN_REQUIRED:
        return gateway_error_response(401, "Missing API key", code="missing_api_key")

    models = await call_manage_service.get_active_models(db)
    models = [m for m in models if (m.ai_model_type or "0") != "1"]
    if identity is not None:
        models = project_manage_service.filter_allowed_models(identity, models)

    grouped: dict[str, dict[str, Any]] = {}
    for model in models:
        name = model.ai_model_name
        entry = grouped.get(name)
        if entry is None:
            created = 0
            if model.created_at is not None:
                try:
                    created = int(model.created_at.timestamp())
                except (AttributeError, ValueError):
                    created = 0
            entry = grouped[name] = {
                "id": name,
                "object": "model",
                "created": created,
                "owned_by": model.com_name or "starggle",
                "x_clusters": set(),
                "x_providers": set(),
                "x_pricing": None,
            }
        entry["x_clusters"].add(model.async_type)
        entry["x_providers"].add(model.com_name or "starggle")
        if entry["x_pricing"] is None and (
            model.input_price_per_1m is not None or model.output_price_per_1m is not None
        ):
            entry["x_pricing"] = {
                "input_per_1m": (
                    float(model.input_price_per_1m)
                    if model.input_price_per_1m is not None else None
                ),
                "output_per_1m": (
                    float(model.output_price_per_1m)
                    if model.output_price_per_1m is not None else None
                ),
                "currency": model.price_currency or "USD",
            }

    data = []
    for name in sorted(grouped):
        entry = grouped[name]
        clusters = sorted(entry["x_clusters"])
        entry["x_clusters"] = clusters
        entry["x_async_type"] = clusters[0] if clusters else "async1"
        entry["x_providers"] = sorted(entry["x_providers"])
        data.append(entry)

    return JSONResponse(
        content={"object": "list", "data": data},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/models", dependencies=[Depends(require_permission(Perm.MODELS_WRITE))])
async def fetch_models(body: FetchModelsBody):
    """从运营商 API 拉取可用模型列表（管理端功能，需要 models:write）.

    请求体: { com_url, api_key }
    """
    t0 = time.perf_counter()
    models, error = await _fetch_models_from_upstream(body.com_url, body.api_key)

    if not models and error:
        return ResponseFactory.bad_request(message=f"获取模型列表失败: {error}")

    elapsed = (time.perf_counter() - t0) * 1000
    logger.info("POST /call-manage/models -> %d models elapsed=%.0fms", len(models), elapsed)
    models = sorted(set(models))
    return ResponseFactory.ok(data={"models": models}, message="查询成功")
