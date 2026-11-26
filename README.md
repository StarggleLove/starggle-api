# LLM Gateway

一个轻量的多厂商大模型统一接入网关：用 OpenAI 兼容协议对接多家模型服务，
在管理台统一维护提供商、模型、集群与请求参数，并记录调用用量。

## 特性

- **多厂商统一接入**：OpenAI 兼容 `POST /chat/completions`，支持流式（SSE）与非流式
- **提供商集中配置**：API 地址与 Key 在提供商层统一维护，模型引用提供商配置
- **模型集群与轮询**：模型按 `async1..async9` 划分为 9 个集群，同集群内按策略选择
- **项目与 API Key**：按项目签发 `sk-gw-...` Token，支持吊销、轮换与模型范围限制
- **项目配额**：每日/每月调用次数、Token 数与成本上限
- **用户配额与封禁**：按用户设置每日/每月 Token 上限与访问封禁开关
- **渠道健康**：成功率、TTFB 延迟、熔断/冷却，同集群内自动换渠道重试
- **成本路由**：每个集群可选轮询、成本优先或延迟优先
- **厂商参数适配**：内置 DeepSeek/GLM/MiniMax/Claude/Gemini/Qwen 等思考参数与协议差异处理
- **模型级参数覆盖**：`temperature` / `top_p` / `top_k` / `max_tokens` 可按模型覆盖
- **用量统计**：Token 用量、调用记录、按项目/模型/功能码/用户分组
- **业务能力**：提示词占位符注入、会员积分日限额、共享 JWT、用户/订单管理
- **内置聊天页**：`/chat` 提供离线 Chatbox 风格界面，会话保存在浏览器本地，支持流式、停止、重新生成、导入/导出
- **管理台**：提供商、模型、集群、项目、密钥、渠道健康与接入说明一体化

## 快速开始（Docker）

```bash
cp .env.example .env
# 修改 .env 中的 MySQL 密码、管理员账号、JWT_SECRET_KEY
docker compose up -d --build
```

打开 <http://localhost:9010/>，使用 Starggle 账号（共享 `user_info` 表的邮箱 + 密码）登录；
账号的 `user_id` 必须在 `ADMIN_USER_IDS` 白名单内，否则无法登录管理台。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# 准备 MySQL 数据库（默认库名 llm_gateway），并填写 .env

uvicorn main:app --host 0.0.0.0 --port 9010
```

应用启动时会自动建表并执行幂等迁移。

## 配置

| 环境变量 | 说明 | 默认 |
|---|---|---|
| `MYSQL_HOST` / `MYSQL_PORT` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` | MySQL 连接 | localhost / 3306 / root / 空 / `llm_gateway` |
| `JWT_SECRET_KEY` | JWT 签名密钥，需与 StarggleCom 一致 | 开发默认值，生产必须注入 |
| `ADMIN_USER_IDS` | 管理端 user_id 白名单，逗号分隔；为空时管理接口 fail-closed | 空 |
| `ADMIN_ROLES` | 角色配置 JSON，如 `{"u_1001":"operator","ops@example.com":"viewer"}`；白名单内未配置的账号默认 `admin` | 空 |
| 钻石计费 | 按 `X-Generation-Id` 聚合一次生成的实际成本，×3 后按 1 美元 = 50 钻石扣除；无开关 | 常开 |
| `GATEWAY_TOKEN_REQUIRED` | `1` 时所有调用必须带 `sk-gw-` 项目 Token | `0` |
| `PORT` | 服务端口 | `9010` |
| `GATEWAY_LOG_BODY` | `0` 时关闭完整请求/返回报文日志（含提示词，敏感环境应关闭） | `1`（开启） |
| `GATEWAY_LOG_BODY_MAX` | 单条报文日志最大字符数，`0` 不截断 | `0` |
| `GATEWAY_DEBUG` | 兼容旧开关，效果等同 `GATEWAY_LOG_BODY=1` | 空 |
| `CORS_ORIGINS` | 跨域管理台来源，逗号分隔 | 空（同源即可） |

### 管理角色

`ADMIN_USER_IDS` 决定谁能进入管理台，`ADMIN_ROLES` 细化每个账号的角色（修改后需重启）：

| 角色 | 权限 |
|---|---|
| `admin` | 全部权限，含删除提供商、查看上游 API Key、用户充值/配额 |
| `operator` | 增改模型/提供商/项目/渠道与路由，可查看上游 Key；不可删除提供商、不可操作用户资金 |
| `developer` | 只读 + 项目 Token 的创建/轮换/吊销 |
| `viewer` | 只读；上游 API Key 一律返回空值 + `apiKeySet` 标记 |

登录接口为 `POST /api/v1/auth/login`（邮箱 + 密码），`GET /api/v1/auth/me` 返回当前角色与权限。
角色在每次请求时按环境变量重新解析，调整 `ADMIN_ROLES` 后重启即可生效，无需重新登录。

## 调用

推荐使用 OpenAI 兼容入口 `/v1/chat/completions`：

```bash
curl -X POST "http://localhost:9010/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gw-你的项目Token" \
  -H "X-Async-Type: async1" \
  -H "X-Feature-Code: role_reply" \
  -d '{
    "model": "",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": false
  }'
```

兼容入口：`/router/call-manage/chat/completions`、`/api/v1/call-manage/chat/completions`。

OpenAI SDK：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:9010/v1",
    api_key="sk-gw-你的项目Token",
    default_headers={"X-Async-Type": "async1"},
)
resp = client.chat.completions.create(
    model="",
    messages=[{"role": "user", "content": "你好"}],
)
```

模型列表：`GET /v1/models` 返回使用中的文本模型；传入 `sk-gw-` 项目 Token 时按 Token 的模型白名单过滤。响应中的 `x_clusters` 可直接作为 `X-Async-Type` 使用。

请求头：

| 请求头 | 取值 | 说明 |
|---|---|---|
| `Authorization` | `Bearer sk-gw-...` | 项目 Token；校验项目、配额与模型范围，不转发给上游 |
| `X-Async-Type` | `async1`..`async9` | 模型集群；不传时主链默认 `async1`、异步链默认 `async2` |
| `X-Purpose` | `main` / `async1`..`async13` | 用途标识，用于日志、会员判定与用量统计 |
| `X-Thinking` | `1` / `0` | 思考开关；仅在模型思考模式为「跟随请求」时生效 |
| `X-Response-Format` | `json_object` | 要求 JSON 输出 |
| `X-User-Id` | 调用方标识 | 用于调用记录、用量统计与会员日限额 |
| `X-Feature-Code` | 如 `role_reply` | 功能码，用于用量分组统计 |
| `X-Request-Id` | 可选，≤64 字符 | 调用链路追踪；不传由网关生成 |

> 不带 `sk-gw-` 前缀的 `Authorization` 保持旧语义：主链可覆盖上游 Key，异步链使用服务端 Key。
> 带 `X-User-Id` 且未使用项目 Token 时，仍按会员等级校验每日调用次数。

## 内置聊天页

启动后打开 <http://127.0.0.1:9010/chat>，或从管理台左侧「对话」进入。这是一个轻量的
Chatbox 风格客户端，直接调用网关的 `/v1/chat/completions`：

- 会话和消息保存在浏览器 IndexedDB（不可用时回退 localStorage），不写入网关数据库
- 支持流式输出、停止生成、重新生成、模型切换、系统提示词、导入/导出 JSON
- 业务模式启用 `GATEWAY_TOKEN_REQUIRED=1` 或使用项目配额时，在「设置」中填写 `sk-gw-...`
- 需要完整客户端时，Chatbox / Open WebUI / Cherry Studio 等可直接使用 `base_url=http://host:9010/v1` 和项目 Token

## 项目与配额

1. 打开管理台 →「项目与密钥」→ 新增项目
2. 在项目详情创建 Token，复制 `sk-gw-...`。完整密钥只显示一次
3. 客户端用 `Authorization: Bearer sk-gw-...` 调用

项目可设置六项限额：每日/每月 × 调用次数、Token 数、成本。规则：

- 留空 = 不限，`0` = 禁止
- 调用次数在请求前检查；Token/成本在响应后累计，属于软限额
- 只有成功请求计数；网关内部重试仍算一次逻辑调用
- 成本只累计与项目币种一致的记录，不做汇率换算
- 配额超限返回 `429`，错误码如 `daily_call_quota_exceeded`
- 项目 Token 走项目配额，不再叠加会员日限额；项目可停用但保留历史用量

成功响应带 `X-Request-Id`、`X-Gateway-Model`、`X-Gateway-Cluster`、
`X-Gateway-Retry-Count`。

## 用户配额与封禁

控制台「用户管理」分区（`/model-manage#users`，旧地址 `/user-manage` 会自动重定向）可为单个用户设置：

- 每日 Token 上限、每月 Token 上限：留空 = 不限，`0` = 禁止
- 访问封禁开关：被封禁用户的调用直接返回 `403`（错误码 `user_blocked`）

规则：

- 按调用方传入的 `X-User-Id` 归属；`sk-gw` 项目 Token 调用同时检查
  `X-User-Id` 与 `gw:<token_id>`，任一被封禁即拦截
- Token 限额是软限额：请求前已用 >= 上限才拒绝，返回 `429`
  （`daily_token_quota_exceeded` / `monthly_token_quota_exceeded`）
- 用量按服务器本地自然日/自然月累计；只有成功请求计数
- 上游未返回 `usage` 的调用按 0 token 计，选择这类上游时 Token 限额无法精确生效

> 安全边界：`X-User-Id` 是内部调用方（如 StarggleCom 后端）传入的受信请求头，
> 网关不校验其真实性。网关应只对内网/后端开放，不要把推理入口直接暴露给浏览器
> 或公网，否则用户可以伪造身份绕过限额。内置 `/chat` 页固定使用 `local-chat`，
> 不参与按用户限额。

## 渠道健康与成本路由

一个模型行视为一个渠道。网关记录今日成功/失败次数、平均 TTFB 延迟、连续失败次数与最后错误。

- 连续失败达到阈值后熔断，冷却期间不再被选中
- 冷却结束后自动半开探测；成功恢复，失败重新熔断
- 同集群重试：连接失败、超时、429、5xx、401/403
- 400/404/422 不重试
- 全部渠道熔断时仍会降级尝试
- 流式请求在开始向客户端输出前完成重试；开始输出后不再换渠道

每个集群可选 `round_robin`（默认）、`cost_first`、`latency_first`；缺价格或延迟数据时退回轮询。

## 项目结构

```
app/
  api/v1/controller/   管理端、模型、项目、渠道健康、调用转发接口
  config/              数据库与运行配置、会员配置
  db/schema.sql        MySQL 建表脚本
  models/              提供商、模型、用量、项目、Token、健康、路由 ORM
  services/            模型选择/路由、用量统计、项目管理、渠道健康
  utils/               JWT、雪花 ID、统一响应、OpenAI 错误格式
templates/model_manage.html   管理台（含用户 / 订单分区）
templates/chat.html           内置聊天页
static/chat/                  聊天页 CSS / JS
main.py                       FastAPI 入口
```

## License

Apache-2.0
