# 智能客服工单平台
由 [hbynn](https://github.com/hbynn) 维护的个人项目，聚焦智能客服与退款工单处理。

- [Python 后端](https://github.com/hbynn/smart-support-backend)：对话服务、退款工作流、业务工具与数据持久化。
- [Web 前端](https://github.com/hbynn/smart-support-frontend)：客服对话、知识库操作和运行状态查看。


智能客服工单平台是面向客服场景的多 Agent 应用，提供通用咨询、技术支持、账单处理和升级转人工等处理路径。它把意图识别、Agent 路由、知识库检索、外部工具、会话记忆和评测串在同一条对话链路中，支持客服咨询、退款审批和工单处理的业务闭环。

本仓库是 **Python 后端**，使用 FastAPI 提供 HTTP API，并提供标准 MCP 服务端。配套 Web 前端和 Java 后端位于独立项目中；只运行本仓库即可通过 Swagger 或 HTTP 客户端使用 Python 服务。

已实现基于 SQLite 的订单、退款工单、审批、幂等执行、结果核验与审计链路。当前退款为本地业务演示，真实资金退回需要对接支付渠道和订单系统。

## 目录

- [项目能力与工作流程](#overview)
- [退款工单链路](#refund-workflow)
- [快速启动](#quickstart)
- [配置说明](#configuration)
- [在线 Embedding 配置](#intent-embedding-setup)
- [对话与知识库使用](#usage)
- [MCP 外部数据接入](#mcp)
- [Skills 业务规则](#skills)
- [评测与监控](#evaluation)
- [本地开发与测试](#development)
- [部署与数据管理](#deployment)
- [常见问题](#faq)
- [项目结构与详细文档](#documentation)

<a id="overview"></a>

## 项目能力与工作流程

| 能力 | 当前实现 |
|---|---|
| 三路意图识别 | LLM、向量相似度、Pattern 规则共同识别意图，输出领域、实体、紧急度和来源分数。 |
| 主辅 Agent 路由 | 在 General、Technical、Billing、Escalation 之间选择处理路径；需要协作时并行执行主辅 Agent，再合成回复。 |
| RAG 工具调用 | Agent 根据问题决定是否调用共享知识库工具，工具层完成检索、可选查询改写与重排。 |
| 分层记忆 | Redis 保存近期消息和摘要；ChromaDB 保存情景记忆及用户画像，为后续对话补充上下文。 |
| 标准 MCP | 通过官方 Python SDK 接入外部 MCP 服务，也将本地知识库暴露给其他 MCP 客户端。 |
| 动态 Skills | 从 Markdown 文件加载业务规范，根据 Agent 和关键词匹配后注入提示词，支持运行时重载。 |
| 故障处理与追踪 | 工具超时、缓存、熔断；专业 Agent 故障回退；通过请求 ID 查询调用轨迹。 |
| 评测与监控 | 异步执行意图及对话质量评测；提供在线统计、Prometheus 指标和路由降权恢复机制。 |

### 一条消息如何处理

以下使用纯文本流程图，普通 Markdown 预览器也能显示：

```text
用户 / Web 前端 / HTTP 客户端
             |
             v
POST /chat：鉴权、会话锁、重复请求回执检查
             |
             v
读取近期消息、摘要、相关情景记忆和用户画像
             |
             v
三路意图识别：LLM + Embedding + Pattern
             |
             v
路由决策
  |-- 需要澄清 ---------------------------> 澄清回复
  |-- 单 Agent --------------------------> 专业回复 / 失败回退
  `-- 主辅 Agent 并行 -------------------> 合成回复
             |
             |  Agent 执行期间可按需调用：
             |  共享 RAG 工具 / 角色工具 / 获准的外部 MCP 工具
             v
记录路由结果和工具轨迹
             |
             v
Redis 事务写入用户消息、助手回复和响应回执
             |
             +----------------------------> 返回 ChatResponse
             `----------------------------> 后台记忆维护、画像更新
```

**意图识别负责路由和提供上下文，RAG 由 Agent 通过工具自主调用。** 细粒度技术意图共享知识库，无需按每个意图维护一套文档。

意图融合默认权重为 LLM 0.7、Embedding 0.2、Pattern 0.1。正常投票先累计同领域的粗、细意图分数，再选择领域内的意图；Pattern 细化要求领域一致。默认低置信度阈值为 0.5，另有识别失败和细化分支。权重与阈值定义在 [意图识别器](core/intent_recognizer.py)，不是 `.env` 配置项；应根据业务标注集验证后调整，不能将融合分数直接视为准确率。

<a id="refund-workflow"></a>

## 退款工单链路

退款链路在 Python 后端实现，由确定性工作流推进状态。聊天咨询与工单执行分开：用户在聊天中提出退款诉求后，可通过 `POST /tickets` 显式创建工单；聊天回复本身不代表已退款。

1. **创建与去重**：接收用户、订单号、金额和原因；可从消息提取订单号及带单位的金额。重复的用户、订单和金额组合复用已有工单。
2. **补齐信息**：缺少订单号进入 `COLLECTING_INFO`，调用补充信息接口后继续规划。
3. **查询与校验**：查询订单归属，检查订单状态及金额；金额须大于零且不超过订单金额，已退款或已取消订单不能再次退款。
4. **风险审批**：金额达到 `REFUND_APPROVAL_THRESHOLD`（默认 100）进入 `WAITING_APPROVAL`；低于阈值直接执行。审批通过后执行，拒绝后进入 `REJECTED`。
5. **幂等执行**：使用 `refund:{ticket_id}` 作为幂等键。退款工具再次检查工单状态、审批记录和操作参数；SQLite 事务负责写入退款记录与更新订单。
6. **结果核验**：执行后进入 `VERIFYING`，按幂等键查询退款记录，查到后进入 `RESOLVED`。超时不直接视为失败，避免重复退款。
7. **恢复与审计**：重试前先查询已有结果，再决定是否用相同幂等键执行；关键状态变更、审批、执行和核验事件均保留审计记录。

```text
创建 → 信息收集 → 规划与校验
                    ├─ 低风险 → 执行
                    └─ 高风险 → 等待审批 → 通过 → 执行
                                      └─ 拒绝 → REJECTED
执行 → 核验 → RESOLVED
         └─ FAILED / VERIFYING → 重试前查询已有结果
```

### 接口与体验方式

启动后打开 `http://localhost:8000/docs`，在“售后工单”分组调用接口。请使用数据库中实际存在且属于请求用户的订单。

- `POST /tickets`：创建工单，请求示例见下方。
- `GET /tickets/{ticket_id}`：查询工单状态和执行结果。
- `POST /tickets/{ticket_id}/details`：补充订单号，可携带金额和原因。
- `POST /tickets/{ticket_id}/approve`：审批通过，随后执行退款。
- `POST /tickets/{ticket_id}/reject`：拒绝审批，携带 `actor` 和 `reason`。
- `POST /tickets/{ticket_id}/retry`：查询并恢复执行，携带 `actor`。
- `GET /tickets/{ticket_id}/audit`：查询审计事件。
- `GET /orders/{order_id}?user_id=用户ID`：查询演示订单。

```json
{
  "user_id": "订单所属用户ID",
  "message": "申请退款",
  "order_id": "实际订单号",
  "amount": 50,
  "reason": "用户申请退款"
}
```

如需关联来源聊天，同时传入 `source_conv_id` 和 `source_request_id`，后端会校验对应用户的对话回执。审批请求示例为 `{"actor":"operator"}`；状态不允许的操作返回 409，工单不存在返回 404。

### 配置与实现位置

在后端 `.env` 中设置 `SUPPORT_DB_PATH` 指定 SQLite 文件位置，通过 `REFUND_APPROVAL_THRESHOLD` 调整审批阈值。其他启动与鉴权配置请按 [.env.example](.env.example) 的实际配置项填写。

- [工作流](workflow/ticket_workflow.py)：状态转换、审批、执行、核验与重试。
- [业务工具](tooling/support_tools.py)：订单查询、退款校验和执行保护。
- [数据仓储](domain/repository.py)：SQLite 持久化、事务和幂等记录。
- [审计记录](audit/audit_logger.py)：业务事件留痕。
- [HTTP 接口](api/main.py)：工单接口与聊天关联。

当前 `RESOLVED` 表示本地退款记录已核验，不代表银行或支付平台到账。接入真实支付服务时，需要替换业务适配实现并核验支付渠道的最终状态。

<a id="quickstart"></a>

## 快速启动

### 1. 准备环境

- 安装并启动 Docker，以及 Docker Compose v2（命令为 `docker compose`）。
- 准备支持 **Anthropic Messages 协议及工具调用**的模型服务、模型名称和 API Key。
- 首次构建需要联网拉取镜像、安装依赖，并下载 ChromaDB 使用的本地向量模型。

以下命令在本仓库根目录执行。Windows 用户可用 Docker Desktop，接口示例可直接在 Swagger 中填写，避免终端引号和换行差异。

### 2. 创建 `.env`

首次配置时复制 [.env.example](.env.example) 为 `.env`：

```bash
cp .env.example .env
```

Windows PowerShell 可使用 `Copy-Item .env.example .env`。已有 `.env` 时直接编辑，保留原配置。

至少核对下面这些值：

```dotenv
# 示例为 DeepSeek 的 Anthropic 兼容入口。
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=替换为服务商当前支持的模型ID
ANTHROPIC_API_KEY=替换为该模型服务的Key

# Redis 密码须与服务端一致
REDIS_PASSWORD=替换为你的Redis密码

# Docker 网络内的 ChromaDB 地址
CHROMA_HOST=chromadb
CHROMA_PORT=8000

# 可选的在线意图向量；没有硅基流动 Key 也能运行。
INTENT_EMBEDDING_PROVIDER=auto
SILICONFLOW_API_KEY=
```

模型名必须是所选平台实际提供、账号可调用的 ID；示例文件中的值不保证适用于所有账号。使用 Anthropic 官方服务时可留空 `ANTHROPIC_BASE_URL`，并填写对应的模型名和 Key。只有 OpenAI Chat Completions 接口的服务，不能直接填到这里使用。

连接 `api.deepseek.com` 时，公共 LLM 调用层会显式关闭默认思考模式，覆盖意图分类、Agent 与汇总回复、查询改写、重排、画像、摘要和评测。这样可避免思考占满短输出预算而没有生成 JSON 或正文；其他模型服务保持原有请求参数。回复真正达到输出上限时仍会标记为 `partial`，不会伪装成完整成功。

### 3. 启动后端及依赖

```bash
docker compose up -d --build
docker compose ps
```


| 入口 | 地址 | 用途 |
|---|---|---|
| Swagger | `http://localhost:8000/docs` | 查看接口、填写请求并执行 |
| 健康检查 | `http://localhost:8000/health` | 确认应用已完成初始化 |
| API | `http://localhost:8000` | 后端接口地址，如 `/chat` |
| Nginx | `http://localhost/docs` | 经反向代理访问 Swagger |
| Prometheus | `http://localhost:9090` | 查询监控指标 |

健康检查成功后，在 Swagger 中执行一次 `POST /chat`，确认模型也能正常调用。`/health` 不验证模型 Key、额度或模型名称。

### 4. 使用配套 Web 前端（可选）

参考 [Web 前端仓库](https://github.com/hbynn/smart-support-frontend) 的本地启动说明。Python API 默认监听 8000 端口。联合部署前请核对 Compose 中的构建路径、环境文件和数据卷配置是否匹配本机目录。

<a id="configuration"></a>

## 配置说明


### 常用配置

| 配置项 | 用途与默认行为 |
|---|---|
| `ANTHROPIC_API_KEY` | 必填，聊天、意图 LLM、摘要和 Judge 等使用的模型服务 Key。 |
| `ANTHROPIC_BASE_URL` | Anthropic 兼容接口地址；留空时使用官方服务。 |
| `ANTHROPIC_MODEL` | 当前服务支持的模型 ID，建议显式填写。 |
| `REDIS_PASSWORD` | Compose 创建 Redis 时使用的密码。 |
| `REDIS_URL` | 应用连接地址；根目录 Compose 根据 `REDIS_PASSWORD` 覆盖为容器网络地址。 |
| `CHROMA_HOST` / `CHROMA_PORT` | Docker 使用 `chromadb:8000`；宿主机开发使用 `localhost:8001`。 |
| `CHROMA_PERSIST_DIRECTORY` | development 无法连接远端 ChromaDB 时的本地存储路径；容器内为 `/app/data/chroma`。production 连接失败时中止启动。 |
| `PROFILE_UPDATE_MIN_INTERVAL_S` | 普通后台画像最短更新间隔，默认 60 秒；明确表达偏好时立即更新。设为 0 恢复逐轮更新。会话消息和业务字段仍每轮保存。 |
| `LOG_LEVEL` | 日志级别，默认 `INFO`。 |
| `INTENT_EMBEDDING_ENABLED` | 是否执行向量分支，默认 `true`；设为 `false` 后仅融合 LLM 和规则。 |
| `INTENT_EMBEDDING_PROVIDER` | `auto`、`local` 或 `siliconflow`，默认 `auto`，见下一节。 |
| `SILICONFLOW_API_KEY` | 可选，在线意图向量使用的独立 Key。 |
| `MCP_EXTERNAL_ENABLED` | 默认 `true`；设为 `false` 关闭外部 MCP 客户端。 |
| `MCP_SERVERS_CONFIG` | 外部 MCP 配置文件路径，默认 `config/mcp_servers.json`。 |


### 修改配置后如何生效

在根目录部署中：

```bash
# 只改 .env：重新创建容器，让它读入环境变量。
docker compose up -d --force-recreate

# 改了应用代码或依赖：重新构建并启动。
docker compose up -d --build
```


联合部署请依据前端 Compose 中的实际服务名和环境文件路径操作。


<a id="intent-embedding-setup"></a>

## 在线 Embedding 配置

项目有两处不同的向量用途：

| 用途 | 模型与存储 |
|---|---|
| 三路意图识别中的向量匹配 | 默认本地字符 n-gram，可配置硅基流动 `BAAI/bge-m3`。 |
| RAG 知识库及 ChromaDB 记忆检索 | 使用 ChromaDB 默认的本地 `all-MiniLM-L6-v2` 向量模型。 |

本节只配置**意图识别的向量分支**，不会替换 RAG 模型，也不需要重建知识库。硅基流动的免费模型范围、账号权限和限流以[官方价格页](https://siliconflow.cn/pricing)为准；项目固定使用非 Pro 的 `BAAI/bge-m3`，不会自动切换到 `Pro/BAAI/bge-m3`。聊天及评测的 LLM 费用由对应服务商计算。

### 1. 申请 Key 并填写配置

1. 在[硅基流动中国站控制台](https://cloud.siliconflow.cn/)注册或登录，按平台要求开通账号。
2. 进入 API 密钥管理，创建 Key，并确认账号可使用 `BAAI/bge-m3`。

```dotenv
INTENT_EMBEDDING_ENABLED=true
INTENT_EMBEDDING_PROVIDER=siliconflow
SILICONFLOW_API_KEY=替换为你自己的硅基流动Key
```

| 模式 | 行为 |
|---|---|
| `auto` | 有 Key 时尝试在线匹配，无 Key 时使用本地 n-gram。 |
| `local` | 始终使用本地 n-gram。 |
| `siliconflow` | 指定在线提供方；缺少 Key 或调用失败时仍会本地降级。 |


### 2. 重新创建容器

### 3. 验证实际调用

先查看 `GET /integrations/embedding`：

```json
{
  "configured_provider": "auto",
  "online_configured": true,
  "model": "BAAI/bge-m3",
  "cooldown": false,
  "last_error": null
}
```

`online_configured=true` 只表示已读取 Key。使用独立检查接口验证模板向量和问题向量，不需要聊天模型余额：

```bash
curl -X POST http://localhost:8000/integrations/embedding/check \
  -H 'Content-Type: application/json' \
  -d '{"message":"这笔钱能退给我吗？"}'
```

只有实际使用在线模型时，结果中的 `success` 才为 `true`。`source` 显示实际向量来源，`status.last_dimensions` 显示维度；本地降级时，`status.fallback_reason` 区分 `missing_api_key`、`configured_local`、`http_401`、`http_429`、`TimeoutError` 等原因。`status.retry_after_s` 表示距离下一次允许在线重试的秒数。状态不包含 API Key。

本地脚本可独立检查，脚本会读取项目根目录 `.env`，环境中已设置的变量优先：

```bash
.venv/bin/python scripts/check_embedding.py --message '这笔钱能退给我吗？'
# 或在运行中的容器执行
```

本地脚本使用 Python 3.12 并安装项目依赖。在线检查成功退出码为 `0`，本地降级或分支关闭时为 `1`，便于在批量评测之前阻止误用本地向量。自己编写测试脚本时，同样需要在创建 `IntentRecognizer` 前加载 `.env`。

接着通过 `/chat` 发送一条新问题，例如“登录时提示 401，应该怎么处理？”，查看响应里的 `intent_embedding_source`：

- `siliconflow/BAAI/bge-m3`：本次匹配实际使用了在线向量。
- `local-ngram`：本次使用本地向量，可能是未配置、主动关闭或调用失败后降级。
- `disabled`：`INTENT_EMBEDDING_ENABLED=false`，没有执行向量分支；使用 LLM 0.85、规则 0.15 的权重。`provider=local` 仍执行向量分支，使用默认三路权重。
- `unavailable`：向量分支异常，未得到匹配结果。

启用鉴权后，状态接口需要管理员访问 Token，聊天接口可使用用户访问 Token。完整聊天还需要有效的 LLM 配置。

首次在线匹配会分批生成意图模板向量，每批最多 32 条，缓存在进程内；后续只需生成问题向量。在线匹配总预算为 8 秒，失败后冷却 60 秒，降级识别结果也会缓存 60 秒。降级时查询和模板一起切换，避免混用向量空间。接口发送当前问题和意图模板，不自动发送会话历史或用户画像。

<a id="usage"></a>

## 对话与知识库使用


### 发送消息

在 Swagger 的 `POST /chat` 中填写：

```json
{
  "message": "登录时提示401错误，应该怎么处理？",
  "user_id": "demo_user",
  "client_request_id": "demo-login-001"
}
```

首次可不传 `conv_id`；后续对话携带响应中的 `conv_id`。每条新消息生成新的 `client_request_id`，同一消息因网络失败重试时复用原 ID。服务端回执保留 24 小时，同一 ID 携带不同消息返回 409。

响应中重点关注：

| 字段 | 如何使用 |
|---|---|
| `response`、`conv_id` | 展示回复、继续同一会话。 |
| `request_id` | 查询本次调用轨迹。 |
| `intent`、`intent_group`、`entities` | 查看细粒度意图、领域及提取实体。 |
| `intent_confidence`、`intent_source_scores` | 排查三路识别结果。 |
| `primary_agent`、`supporting_agents`、`routing_reason` | 了解主辅 Agent 的选择和路由依据。 |
| `tools_used`、`knowledge_used` | 查看工具使用情况；后者仅在知识库工具实际成功时为 true。 |
| `intent_embedding_source` | 确认本次向量分支使用在线模型还是本地 n-gram。 |
| `performance` | 本轮记忆读取、意图和 Agent 执行耗时，以及模型逻辑调用数、供应商返回的 token 用量和检索缓存事件；不包含问题正文。 |
| `status` | `success` 为完整回复，`partial` 表示回复截断或仅部分 Agent 成功，`failed` 为执行失败。 |
| `retryable`、`error_code` | 执行失败返回 HTTP 503；仅 `retryable=true` 时可以复用请求 ID 重新执行。 |
| `latency_ms` | 从进入聊天处理、等待会话锁到生成回复的耗时；完整 HTTP 耗时（含存储提交）见 `Server-Timing` 响应头。 |

检索会缓存“查询改写 → 召回 → 重排”的完整结果，并合并并发的相同检索。缓存按查询、Top-K、工具版本和传入的用户上下文隔离，沿用工具的 TTL；知识库更新会使线上及评测缓存失效。工具 TTL 为 0 时关闭完整流程缓存和并发合并，失败检索不缓存。`/search` 和知识库工具结果的 `cached` 表示完整流程命中，此时不再请求改写或重排模型。

默认回复提示约 200–400 个中文字符，多角色汇总约 300–500 个字符，保留必要依据、核验条件和下一步；用户明确要求详细说明时允许展开。这是生成时的篇幅指导，不是返回后裁剪，实际达到模型输出上限时仍返回 `partial`。结构化实体优先随角色输入包提供，避免在同一次模型请求中重复发送。

`performance.stages_ms` 是到生成回复为止的阶段耗时，嵌套阶段不可直接相加；`performance.llm.latency_ms` 是各次模型调用耗时之和，并行时可能大于实际墙钟时间。持久化提交在这些阶段之后，完整 HTTP 耗时仍以 `Server-Timing` 为准。幂等回放保留原响应中的统计。`/monitor` 的 `llm_usage.by_stage` 统计本进程各阶段用量，包括后台画像；进程重启后重新累计。调用数按 SDK 逻辑调用计算，不展开 SDK 内部重试；token 字段原样采用供应商统计口径，未报告的调用通过 `usage_reported_calls` 区分，不据此推算账单金额。

聊天总超时为 50 秒。同一会话串行处理，不同会话可并行；用户和助手消息一起写入 Redis。后台记忆压缩与画像更新不阻塞回复返回。

失败回执不会写入正常对话历史。可安全重试的失败在复用原 ID 时重新执行；成功和部分成功的回执直接返回。可能修改数据的工具执行前会保存不可重放标记，超时或连接中断后需要先核验业务结果，避免重复操作。自定义副作用工具必须将 `AgentToolSpec.retry_safe` 设置为 `false`；外部 MCP 工具按配置和只读声明确定。

订单号、金额和日期等字段按会话保存，并在下一轮交给工具使用；切换订单或业务领域时清除旧字段。字段来源可追踪，但这些字段不代表身份已核验或业务操作已获授权。

### 导入与检索知识库

在 `POST /knowledge/add` 中提交：

```json
{
  "documents": [
    {
      "document_id": "demo-login-guide",
      "title": "登录401排查",
      "content": "演示文档：登录返回401时，先检查登录状态和令牌有效期；令牌过期后重新登录。不要在聊天中提交密码或完整令牌。"
    }
  ]
}
```

- 相同 `document_id` 表示更新同一文档；不传时按标题识别文档，同标题导入会覆盖。
- 同名不同来源的文档应指定不同 `document_id`。同批次相同身份保留最后一个版本。
- 文档会切片、向量化；更新会删除多余旧片段，并使相关检索缓存失效。接口返回的数量是**片段数**。
- `POST /knowledge/upload` 支持 txt、md、json，单文件最大 10 MB；JSON 格式可在 Swagger 查看。
- `GET /knowledge/stats` 查看片段总数。
- `POST /search` 使用查询参数 `query`、`top_k`，用于独立验证检索效果，不需要先经过 Agent 路由。
- `query` 最多 2000 字符，`top_k` 范围为 1–20；检索结果保留文档 ID、内容版本和片段位置。

先验证 `/search` 能召回文档，再通过 `/chat` 检查 Agent 是否按问题调用了知识库工具。

<a id="mcp"></a>

## MCP 外部数据接入

项目使用官方 `mcp==1.26.0` Python SDK，支持 Streamable HTTP 和 stdio 客户端。连接、工具发现及调用由 `tooling/mcp_client.py` 管理，工具执行继续经过缓存、超时、熔断和参数校验。

### 默认外部服务

[config/mcp_servers.json](config/mcp_servers.json) 配置了 Microsoft Learn 公开技术文档服务：

```json
{
  "servers": [
    {
      "name": "mslearn",
      "enabled": true,
      "transport": "streamable_http",
      "url": "https://learn.microsoft.com/api/mcp",
      "allowed_tools": ["microsoft_docs_search", "microsoft_docs_fetch"],
      "agents": ["general", "technical"],
      "read_only": true,
      "timeout_s": 25,
      "cache_ttl": 300,
      "max_result_chars": 12000
    }
  ]
}
```

General、Technical Agent 可以按需查询该服务；Billing、Escalation 不获得这两个外部工具。服务说明见 [Microsoft Learn MCP](https://learn.microsoft.com/en-us/training/support/mcp)。

查看 `GET /integrations/mcp` 确认连接状态，再用 `POST /integrations/mcp/call` 独立测试工具：

```json
{
  "name": "mcp_mslearn_microsoft_docs_search",
  "arguments": {"query": "Azure Python authentication troubleshooting"}
}
```

此调试接口不经过 LLM。成功后可在聊天中提问，并用轨迹里的 `source=external_mcp`、`mcp_server`、`mcp_tool` 确认实际调用。

### 配置其他服务

- HTTP 服务填写 `name`、`url`、工具白名单和允许使用的 Agent。
- 需要认证时，通过 `headers_env` 将请求头映射到服务端环境变量，例如 `{"Authorization":"PRIVATE_MCP_AUTH_HEADER"}`；该变量的值填写完整的 `Bearer ...`。
- stdio 服务使用 `transport: "stdio"`、`command`、`args`，所需可执行程序必须存在于应用运行环境；附加环境变量通过 `env_vars` 显式配置。
- `read_only=true` 时，工具还必须声明只读且未声明破坏性操作；缺少相应声明的工具不会开放。

外部服务只收到调用参数，不自动收到聊天历史、画像或模型 Key。连接失败会标记断开并重试，本地工具仍可使用。返回内容超出上限会标记 `truncated=true`，预览不能当成完整原文。


连接地址为 **`http://localhost:8000/mcp/`**，保留末尾斜杠。当前提供：

- 工具：`search_local_knowledge(query, top_k)`。

这是标准 MCP 入口；`/integrations/mcp/*` 是外部客户端的 REST 调试接口。使用自定义域名时配置 `MCP_ALLOWED_HOSTS`，涉及浏览器来源时配置 `MCP_ALLOWED_ORIGINS`；启用接口鉴权后，MCP 客户端也需要发送访问 Token。

<a id="skills"></a>

## Skills 业务规则

内置通用客服、技术支持和账单处理三类规则，位于 `skills/<名称>/SKILL.md`。修改业务处理步骤、升级条件或回复约束时，可直接编辑对应文件。

```bash
# 重新加载规则
curl -X POST http://localhost:8000/skills/reload

# 查看加载结果与解析错误
curl http://localhost:8000/skills
```

规则通过 Agent 类型和关键词匹配后注入，受字符预算限制；把关键约束放在前面。文件格式与字段说明见 [Skills 使用说明](skills/README.md)。Skills 用于处理规范，业务事实和产品资料更适合放入知识库。

<a id="evaluation"></a>

## 评测与监控

### 提交评测任务

`POST /eval/run` 返回 **HTTP 202 和 `job_id`**。不传请求体时使用内置用例，也可提供自己的用例：

```json
{
  "intent_cases": [
    {
      "message": "登录时提示401",
      "expected_intent": "technical_login"
    }
  ],
  "dialog_cases": [
    {
      "question": "登录返回401该怎么处理？",
      "reference_answer": "检查登录状态和令牌有效期，过期后重新登录，不收集密码或完整令牌。",
      "evidence": "演示登录指南规定：先检查登录状态和令牌有效期。"
    }
  ]
}
```

通过 `GET /eval/jobs/{job_id}` 轮询：

```text
queued -> running -> completed
                  -> failed / cancelled
```

`completed`、`total` 字段表示进度；最终报告在 `result` 内。**任务执行完成不等于所有评分成功**，还要检查报告：

| 报告字段 | 含义 |
|---|---|
| `status` | `complete`、`partial`、`failed` 或 `empty`。 |
| `valid_total` / `evaluation_errors` | 有效用例数与评分执行失败数。 |
| `pass_rate` | 按有效用例计算；有效数为 0 时不能理解为正常的业务通过率。 |
| `results[].metadata.judge_error` | 评分失败原因；失败样本的 `scores` 为空，不以 0.5 代替失败。 |
| `suite_id` / `regression_note` | 回归比较使用的评测集合标识及跳过原因。 |

`intent_cases[].context.history` 可以提供历史消息；多轮对话用 `dialog_cases[].turns`。省略某一类用例时使用该类内置用例，明确传 `[]` 则跳过该类。字段结构以 Swagger 为准。

### Judge 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `EVAL_JUDGE_MODEL` | 对话模型 | 仍使用相同的服务地址和模型 Key。 |
| `EVAL_JUDGE_MAX_TOKENS` | 4096 | 输出预算，截断重试时可加倍，最大 8192。 |
| `EVAL_JUDGE_TIMEOUT_S` | 45 | 每次评分请求超时，单位秒。 |
| `EVAL_BASELINE_PATH` | Compose 中为 `/app/data/eval/baseline.json` | 回归基线文件。 |

每条评分最多请求两次，401/403 等鉴权错误立即失败。解析支持代码围栏和前后说明文字，但缺字段、非法分值或不完整输出仍会记为失败。只有相同 `suite_id` 的完整评测参与基线比较；比较包含通过率，检测到回归的报告不会替换有效基线。

对话评分默认注入 5 个 Few-shot 校准示例，覆盖有依据的完整回答、诚实说明能力边界、笼统回答、编造操作成功及遗漏多轮已知信息。示例包含四维分数和评分依据，定义在 `evaluation/few_shot.py`；它们仅进入 Judge 提示词，不传给被测 Agent、不计入测试用例数量，也不新增模型请求。输入 token 会略有增加，Few-shot 用于校准评分尺度，不用于缩短评测时间。

每条对话结果记录 `judge_few_shot_version` 和 `judge_few_shot_count`。示例内容及版本纳入 `suite_id`，修改后首次完整评测会建立新的比较基线，不与旧评分标准直接比较。内置示例中的业务资料仅用于对应示例，当前用例仍以其参考答案、核验资料和对话历史为依据。

评测使用独立的 Orchestrator、统计、工具缓存和熔断状态，但共享真实知识库和外部服务，会消耗模型及外部接口额度。没有参考答案或证据的质量评分属于模型判断，应配合人工检查。

在线对话评测报告的 `scope=orchestrator`，覆盖编排、会话字段合并和工具调用；HTTP 鉴权、真实记忆生命周期及幂等回执由 API 回归和存储集成检查验证。内置对话用例提供参考答案和核验资料。自定义用例可设置 `expected_agent`、`expected_intent`、`required_tools`、`expected_entities`，对最后一轮进行确定性检查；执行失败、部分成功或检查未通过时，Judge 高分也不能使该轮通过。

评测 Agent 不获得可能修改数据的工具。生产部署的知识库和记忆服务连接失败时中止启动；新空库需要管理员导入实际业务文档。

### 在线监控与调用轨迹

| 接口 | 用途 |
|---|---|
| `GET /monitor` | 在线运行统计。 |
| `GET /metrics` | Prometheus 指标，抓取配置在 `config/prometheus.yml`。 |
| `GET /trace/tool/{request_id}` | 查看一次聊天的路由和工具轨迹。 |
| `GET /trace/tools?limit=20` | 查看最近的聊天轨迹。 |


聊天轨迹脱敏后存入 Redis，保留 7 天，最近列表最多 200 条；专业 Agent 失败回退时保留两次尝试的工具轨迹及回退事件。

`agent_latency_ms` 使用毫秒桶，`chat_latency_ms` 按 HTTP 状态码记录请求总耗时；将 `PROMETHEUS_PORT` 设为 0 可关闭独立指标端口，`/metrics` 仍提供业务指标。

<a id="development"></a>

## 本地开发与测试

推荐 Python 3.12，与 Docker 镜像一致。在项目根目录创建虚拟环境并安装依赖：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows 可使用 `py -3.12 -m venv .venv`，然后执行 `.venv\Scripts\Activate.ps1`。

只用 Docker 启动存储服务：

```bash
docker compose up -d redis chromadb
```

原生 Python 进程无法通过 Docker 服务名访问存储，在 `.env` 中改成宿主机地址：

```dotenv
REDIS_URL=redis://:你的Redis密码@localhost:6379/0
CHROMA_HOST=localhost
CHROMA_PORT=8001
CHROMA_PERSIST_DIRECTORY=./data/chroma
```

Redis URL 中的密码应与实际密码一致。回到 Docker 部署时，把 ChromaDB 地址恢复为 `chromadb:8000`。

启动 API：

```bash
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

还可执行 `python api/main.py --cli` 进行终端交互。CLI 用于直接调试编排和工具；HTTP 鉴权、请求回执、异步评测等接口行为应通过 API 验证。

### 执行检查

```bash
# 测试依赖单独安装，不需要加到生产镜像中。
python -m pip install -r requirements-dev.txt
python -m pytest -q

# 已配置本地 Redis / ChromaDB 连接后运行集成检查。
python tests/verify_request_concurrency.py
python tests/verify_chat_atomic.py
```

集成检查使用临时测试数据并清理。单元测试通过不代表模型平台鉴权成功，部署后仍需验证真实 `/chat`；在线 Embedding 和外部 MCP 分别按前文确认调用来源和工具结果。

<a id="deployment"></a>

## 部署与数据管理

### API 访问鉴权

按 [.env.example](.env.example) 及 [鉴权实现](api/main.py) 中的实际配置名设置部署模式和访问 Token。生产模式须配置用户与管理员的 Token 映射，客户端使用 Authorization: Bearer <访问Token> 请求接口。

用独立随机值替换示例中的 Token，可通过 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成。生产模式缺少或配置无效时启动失败。客户端发送：


- 普通用户可访问聊天、共享知识查询、Skills 列表和本地 MCP；聊天身份来自 Token 中的 `user_id`。
- 管理员可访问评测、轨迹、知识库写入、监控、集成调试等接口。
- `/health` 无需认证；启用鉴权后不公开 Agent 明细。
- 配套前端在连接设置填写 Python API 访问 Token，仅保存在当前页面内存，刷新后需要重新填写。

启用鉴权后，直接打开 `/docs` 也需要认证头。可用支持设置请求头的 HTTP 客户端测试接口；不要把模型 Key 当作 API 访问 Token。

根目录 Compose 的端口绑定 `127.0.0.1`。对外访问应通过配置 HTTPS 的反向代理；Prometheus 抓取 `/metrics` 需要管理员凭据，可在抓取配置中通过 `authorization.type: Bearer` 和只读挂载的 `authorization.credentials_file` 提供。

### 数据保存位置

| 数据 | 根目录 Compose 中的位置 |
|---|---|
| 近期消息、摘要、回执、聊天轨迹 | Redis 命名卷 `redis-data`。 |
| 知识库、情景记忆和画像 | ChromaDB 命名卷 `chromadb-data`。 |
| ChromaDB 本地降级数据 | 项目 `data/chroma/` 挂载到 `/app/data/chroma`。 |
| 评测基线 | 项目 `data/eval/`。 |
| Skills 与 MCP 配置 | 项目 `skills/` 和 `config/mcp_servers.json`。 |
| Prometheus 时序数据 | 命名卷 `prometheus-data`。 |

常用管理命令：

```bash
docker compose ps
docker compose down
```

`down` 停止并移除容器，保留命名卷；`down -v` 会删除命名卷及其中数据，不能当作普通重启命令。备份应覆盖实际使用的数据卷和项目挂载目录；远端 ChromaDB 不可达时的本地降级数据不会自动合并回远端。

### 当前运行边界

- 推荐单应用进程部署。评测任务、路由状态和工具缓存都在进程内，不支持多 worker 共享。
- 评测最多同时保留 8 个未完成任务，串行执行，单任务执行上限 1800 秒，保留最近 50 个任务。应用重启后任务状态丢失，需要重新提交。
- 知识库写入锁是进程内锁，多副本同时导入需要另行协调；ChromaDB 的更新和删除不是跨进程原子事务。
- 重试回执避免已完成聊天重复写入，但无法保证进程崩溃前后的模型调用恰好执行一次。
- 当前知识库是共享库，用户会话隔离不等于知识库按租户隔离。接入真实业务数据时，应按实际访问范围扩展权限模型。

<a id="faq"></a>

## 常见问题

| 现象 | 检查与处理 |
|---|---|
| 容器构建失败或长时间停留在下载 | 查看构建日志，区分镜像拉取、Python 依赖安装和 ONNX 模型下载；首次构建需要这些网络资源。 |
| 健康检查正常，但聊天失败 | 查看应用日志和模型服务响应，核对 Key、Anthropic 兼容地址、模型 ID、额度及工具调用支持。 |
| 修改 `.env` 后不生效 | 重新创建应用容器；`restart` 只重启原容器。确认编辑的是 Python 项目的 `.env`。 |
| Redis 认证失败 | 检查 Redis 启动密码与应用 `REDIS_URL` 是否一致。改变密码后同时重新创建 Redis 和应用。 |
| Embedding 的 `online_configured=false` | 检查 Key 是否为空、模式是否为 `local`、容器是否重新创建。 |
| Embedding 返回 `http_401` / `http_403` | 检查是否使用硅基流动 Key，以及账号对当前模型的权限。 |
| Embedding 返回 `http_429`、超时或 `ValueError` | 分别检查限流、网络、响应向量格式；服务会本地降级。等待冷却和缓存过期后，用新问题验证。 |
| 知识库导入成功但召回异常 | 先检查片段数并独立调用 `/search`；核对文档内容、文档身份及实际连接的 ChromaDB 地址。 |
| MCP 工具没有出现 | 查看 `/integrations/mcp`、网络和白名单；核对 Agent 范围及只读声明。修改配置后重启应用。 |
| 评测只有任务 ID，没有分数 | 使用 `/eval/jobs/{job_id}` 轮询，在任务完成后读取 `result`。 |
| 评测失败较多 | 查看 `judge_error_type`、`judge_stop_reason`、`judge_attempts`；区分鉴权、超时、截断、JSON 格式和评分字段错误。 |
| 评测任务返回 404 / 提交返回 429 | 分别表示任务不存在或已过期、未完成队列已满；重启会清空任务状态，避免重复提交堆积。 |
| 端口或容器名冲突 | 检查是否同时启动了根目录和前端目录两套 Compose，停止不使用的一套。 |

<a id="documentation"></a>

## 项目结构与详细文档

```text
api/main.py                    HTTP API、应用初始化、CLI 入口
agents/agent_orchestrator.py    Agent 执行、主辅路由和回复合成
agents/tools.py                Agent 工具封装
core/intent_recognizer.py      三路意图识别与领域融合
core/intent_embedding.py       在线向量、本地降级与模板缓存
core/skill_loader.py           Skills 加载和匹配
core/access.py                 API 身份与角色校验
memory/conversation_memory.py  Redis / ChromaDB 分层记忆
tooling/                       工具执行与标准 MCP
  tool_manager.py              缓存、超时、熔断、参数校验
  knowledge_base.py            知识库导入、切片与检索
  mcp_client.py                外部 MCP 客户端
monitor/performance_monitor.py 运行统计与 Prometheus 指标
evaluation/evaluator.py        意图评测、Judge 与回归基线
evaluation/few_shot.py         Judge 评分校准示例
evaluation/jobs.py             异步评测任务队列
config/                        MCP、Nginx、Prometheus 配置
skills/                        业务处理规范
tests/                         单元测试与集成检查
wiki/                          使用、架构与代码文档
.env.example                   环境变量模板
docker-compose.yml             后端及依赖部署
```

| 文档 | 适合阅读的内容 |
|---|---|
| [完整使用指南](wiki/完整使用指南.md) | 功能操作与使用细节。 |
| [业务流程说明](wiki/业务流程说明.md) | 一次请求从接收到响应的处理过程。 |
| [技术亮点](wiki/技术亮点.md) | 设计选择、实现方式和适用边界。 |
| [重点代码](wiki/重点代码.md) | 核心模块的代码阅读说明。 |
| [Skills 使用说明](skills/README.md) | 自定义业务规则的格式与加载方式。 |
| [文档中心](wiki/文档中心.md) | 功能设计、架构和运维文档入口。 |

具体日期的改动记录见项目根目录的更新说明；本 README 介绍当前项目能力、配置和使用方式。

## 可审批售后工单


项目新增可持久化退款工单，将“生成客服回答”扩展为“执行受控业务动作”。主要能力：

- SQLite保存订单、工单、退款结果和审计事件。
- `request_key`避免相同逻辑请求重复建单。
- `idempotency_key`与数据库唯一约束避免重复退款。
- 默认100元及以上退款进入人工审批。
- 写工具再次校验工单状态、审批人与已规划参数。
- 退款结果未知时先查询旧结果，再决定是否使用原幂等键重试。

主要接口：

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/tickets` | 创建退款工单 |
| `GET` | `/tickets/{ticket_id}` | 查看工单状态 |
| `POST` | `/tickets/{ticket_id}/details` | 补充订单信息 |
| `POST` | `/tickets/{ticket_id}/approve` | 人工审批 |
| `POST` | `/tickets/{ticket_id}/reject` | 人工拒绝 |
| `POST` | `/tickets/{ticket_id}/retry` | 安全恢复失败任务 |
| `GET` | `/tickets/{ticket_id}/audit` | 查看审计轨迹 |
| `GET` | `/orders/{order_id}` | 查看演示订单 |

完整代码说明和调用示例见 [退款工单设计](wiki/退款工单设计.md)。
