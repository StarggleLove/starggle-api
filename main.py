"""LLM Gateway - 多厂商大模型统一接入网关"""
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# 先加载 .env，再导入会在模块级读取环境变量的模块
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from app.api.v1.api import api_router  # noqa: E402
from app.api.v1.controller import call_manage_controller  # noqa: E402

logger = logging.getLogger()  # root logger — 让所有模块的日志都能输出
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)

# 关闭第三方库的 INFO 日志（httpx, urllib3 等）
for lib in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(lib).setLevel(logging.WARNING)

# 与 call_manage_controller 同口径：完整报文日志默认开启，GATEWAY_LOG_BODY=0 关闭；
# GATEWAY_DEBUG 保留为兼容别名
LOG_BODY = os.environ.get("GATEWAY_LOG_BODY", "1").strip().lower() not in ("0", "false", "no", "off") \
    or os.environ.get("GATEWAY_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动时：初始化数据库
    from app.config.database import init_database
    db_ok = await init_database()
    if not db_ok:
        logger.warning("数据库初始化未完全成功，部分功能可能不可用")
    if LOG_BODY:
        logger.info("完整报文日志已开启 — 请求/返回报文将完整打印（GATEWAY_LOG_BODY=0 可关闭）")
    yield
    # 关闭时：清理资源
    from app.config.database import engine
    await engine.dispose()
    logger.info("数据库连接池已释放")


app = FastAPI(title="LLM Gateway", lifespan=lifespan)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestIdMiddleware:
    """为每个请求生成/透传 X-Request-Id，纯 ASGI 实现，不影响流式响应."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = ""
        for key, value in scope.get("headers", []):
            if key.lower() == b"x-request-id":
                raw = value.decode("latin-1").strip()
                break
        request_id = raw if _REQUEST_ID_RE.match(raw) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers[:] = [
                    (k, v) for k, v in headers if k.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_with_request_id)


# 管理台默认同源访问；跨域部署时通过 CORS_ORIGINS 配置允许来源（逗号分隔）
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# 最后添加的中间件最先执行，确保所有响应都带请求 ID
app.add_middleware(RequestIdMiddleware)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# 挂载 v1 API 路由
app.include_router(api_router, prefix="/api/v1")

# 同时挂载到 /router 中间路径（兼容既有调用）
app.include_router(api_router, prefix="/router")

# OpenAI 客户端友好入口：base_url=http://host:9010/v1
app.include_router(call_manage_controller.router, prefix="/v1", tags=["OpenAI 兼容入口"])


# ---------- 前端管理台 ----------
@app.get("/", response_class=HTMLResponse)
@app.get("/model-manage", response_class=HTMLResponse)
async def manage_page():
    html_path = BASE_DIR / "templates" / "model_manage.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/user-manage")
async def user_manage_page():
    """用户管理已集成到控制台，旧地址重定向到「用户管理」分区."""
    return RedirectResponse(url="/model-manage#users", status_code=307)


@app.get("/chat", response_class=HTMLResponse)
@app.get("/chat/", response_class=HTMLResponse)
async def chat_page():
    """内置聊天页：会话保存在浏览器本地，网关只负责转发和记账."""
    html_path = BASE_DIR / "templates" / "chat.html"
    return HTMLResponse(
        html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    import asyncio

    import uvicorn

    port = int(os.getenv("PORT", "9010"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port)
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
