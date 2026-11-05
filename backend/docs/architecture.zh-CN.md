# 算力服后端架构参考（中文）

> 本地安装、配置和启动步骤见 [README.md](../README.md)（英文）。本文档说明各模块设计和运维要点。

实现 [设计文档](../../docs/项目规划/设计文档.md) §4 中算力服与 MES 主后端之间的内部接口。只接受主后端调用；用户鉴权、权限和用户端通信由其他团队负责。

## 当前状态

| 能力 | 状态 |
| --- | --- |
| 服务令牌认证、请求 ID、结构化日志、统一错误格式 | 已实现 |
| 数据采集：模拟数据 / MQTT 订阅 → 解析 → 校验 → 批量写入 IoTDB | 已实现（真实 MQTT 报文格式待定，当前使用临时格式） |
| `GET /internal/v1/equipment/health` 健康概览 | 已实现（确定性规则） |
| 维护分析创建 / 查询 / SSE 事件流 / `Last-Event-ID` 断线恢复 / `Idempotency-Key` | 已实现（确定性规则 + 可选模型说明） |
| 大模型接入（DeepSeek API 测试 / 本地 vLLM 生产，OpenAI 兼容接口） | 已实现：维护分析说明、智能助手 |
| 智能助手：模型自主决定是否检索知识库 / 查询设备数据，流式回答，带引用 | 已实现 |
| 知识库：离线构建脚本（GLM embedding → Milvus），版本化、原子切换、回滚 | 已实现 |
| 会话持久化（openGauss）：用户、会话、消息、引用、反馈 | 已实现 |
| 用户身份：可信网关头 / JWT 断言 / 开发模式，会话按用户隔离 | 已实现（真实认证待客户端接入） |
| `/health/live`、`/health/ready`、`/health/model` | 已实现 |
| rerank、维护分析结果持久化、结果回写 MES | 未实现 |

## 存储

各类存储都通过 `storage/base.py` 中的接口访问，业务代码不依赖具体产品，选型确定后只需新增实现：

| 数据 | 接口 | 当前实现 | 状态 |
| --- | --- | --- | --- |
| 设备时序数据 | `TimeSeriesStore` | `iotdb`（默认）、`memory`（仅测试） | 已确定：Apache IoTDB |
| 助手用户、会话、消息、引用、反馈 | `ConversationStore` | `opengauss`、`memory`（仅测试） | 已确定：openGauss |
| 知识切片与向量 | `MilvusKnowledgeStore` | Milvus 2.6（测试可用 Milvus Lite） | 已确定：Milvus |
| 维护分析任务与事件 | `AnalysisStore` | `memory` | 重启后数据丢失，后续可迁入 openGauss |
| 设备主数据 | `EquipmentCatalog` | JSON 文件 | 待 MES 主数据接口 |

> IoTDB 是时序数据库，不是向量数据库。RAG 所需的向量库需要单独选型。

IoTDB 适配器使用树模型，路径约定（需与数据写入方确认）：`<IOTDB_ROOT_PATH>.<site_id>.<equipment_id>.<metric>`，时间戳按毫秒精度。算力服只需要只读账号。

## 大模型

DeepSeek API 与 vLLM 都提供 OpenAI 兼容的 `/chat/completions` 接口，切换只需修改配置，不改代码：

| 阶段 | 配置 |
| --- | --- |
| 开发/测试 | `LLM_PROVIDER=deepseek`、`LLM_API_KEY=...`（默认 `https://api.deepseek.com`、`deepseek-chat`） |
| 生产 | `LLM_PROVIDER=vllm`、`LLM_BASE_URL=http://127.0.0.1:8000/v1`、`LLM_MODEL=<served-model-name>`、`LLM_MODEL_PRECISION=W8A8` |
| 不使用模型 | `LLM_PROVIDER=none`（默认，仅规则文本） |

- **DeepSeek 是公网服务**：`APP_ENV=production` 时拒绝启动；测试阶段只能使用模拟或经批准脱敏的数据，不得发送真实电站数据。
- 健康等级始终由规则决定。模型只改写风险说明和建议措辞，输出经 Schema 校验，且不得删除或降低规则建议的优先级、不得引用分析范围外的设备；校验失败重试一次，仍失败则保留规则文本，结果中 `model` 为 `null`。
- vLLM 下默认关闭 Qwen3 思考模式（`LLM_ENABLE_THINKING=false`）；任何 `<think>` 内容都会被丢弃，不展示给用户。
- 日志只记录耗时和 token 数，不记录提示词和模型输出。

## 智能助手（Agent + RAG）

```text
客户端 → MES 主后端（鉴权）→ POST /internal/v1/assistant/chat（服务令牌 + 用户身份）
          ↓
  AgentRunner：模型带工具流式推理，自主决定是否调用
     ├─ search_knowledge_base → 查询向量化（GLM，与建库同一模型/维度）→ Milvus 检索（按密级、角色过滤）
     ├─ get_equipment_health  → IoTDB + 规则判定
     └─ get_device_readings   → IoTDB 统计
          ↓
  回答流式返回（stage / delta / citation / result / error / done），用户消息与回答、引用写入 openGauss
```

- **不对每个问题都检索**：知识检索是一个工具，由模型根据问题和上下文决定是否调用、用什么检索语句、检索几次；寒暄或已有依据的追问直接回答。最后一步不再提供工具，保证必有回答。
- **证据与引用**：工具结果按全局编号 [1][2]… 提供给模型；回答中引用到的编号转为 `citation`（知识：文档、章节、页码、知识版本、相似度；数据：设备、时间范围）。检索无结果时模型被要求明确说明“未找到依据”。
- **版本一致**：一次回答内第一次检索确定知识版本，后续检索固定使用同一版本；回答记录 `knowledge_index_version`。
- **思维链不外露**：Qwen3 默认关闭思考模式，流中出现的 `<think>` 内容会被过滤。
- **历史**：每轮把最近 `ASSISTANT_HISTORY_MESSAGES` 条消息（仅正文，不含工具中间结果）作为上下文；工具调用轨迹单独存档用于审计。
- **客户端断开**：已生成的内容以 `interrupted` 状态保存。
- vLLM 需开启工具调用（Qwen3：`--enable-auto-tool-choice --tool-call-parser hermes`）；DeepSeek 使用 `deepseek-chat`。

### 知识库构建（离线）

知识库不提供上传、删除或索引管理接口，由运维通过独立命令维护：

```bash
industrial-ai-kb checksum corpus/manifest.json --write   # 准备发布：写入各文件 SHA-256（需人工复核）
industrial-ai-kb build corpus/manifest.json              # 校验 → 解析 → 切片 → embedding → 写入 Milvus → 自检 → 切换别名
industrial-ai-kb list                                    # 所有版本及当前生效版本
industrial-ai-kb activate <version>                      # 切换/回滚（原子别名切换）
industrial-ai-kb search "问题" --roles radiation_protection   # 检索调试
industrial-ai-kb drop <version>                          # 删除非生效版本
```

- **清单**（示例见 `examples/knowledge/manifest.json`）：版本号、每个文档的 `doc_id`、路径、标题、来源、文档版本、生效日期、密级 `classification`、访问范围 `access_scope`（`all` 或角色名）和 `sha256`。任何文件校验不符即中止，不会写入。
- **每个版本一个集合** `<KB_ALIAS>__<version>`，别名 `<KB_ALIAS>` 指向生效版本；构建失败自动删除半成品，生效版本不受影响；旧版本保留用于回滚。
- **embedding 一致性**：模型名和维度写入集合属性 `kb.meta`。查询时若与当前 `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` 不一致，检索被拒绝（`/health/ready` 显示 `incompatible`），避免在不同向量空间中检索。更换模型或维度必须重新构建版本。
- **格式**：Markdown（按标题保留章节路径）、纯文本；PDF 需 `pip install -e ".[pdf]"`（保留页码）。切片按句子打包（`KB_CHUNK_CHARS`、`KB_CHUNK_OVERLAP`），向量化时附加“标题 / 章节”。
- **GLM embedding-3**：维度 256/512/1024/2048（默认配置 1024），单次请求最多 64 条、3072 tokens；批次同时按条数和字符数限制。
- **示例语料**只描述本系统自身规则和单位换算，用于联调，不构成电站技术规程。

> **数据外发**：`EMBEDDING_PROVIDER=zhipu`（智谱公网 API）和 `LLM_PROVIDER=deepseek` 会把文本发送到公网服务，`APP_ENV=production` 时拒绝启动。核电技术文档在使用公网 API 建库前须经批准；生产环境应使用私有化部署的 GLM embedding（`EMBEDDING_PROVIDER=local`）和本地 vLLM。

## 会话存储（openGauss）

表结构见 `src/industrial_ai/storage/migrations/0001_conversations.sql`：`app_users`（按 `issuer + subject` 唯一）、`chat_sessions`（软删除、归档）、`chat_messages`（会话内序号、状态、模型、知识版本、token、工具轨迹）、`message_citations`、`message_feedback`。所有查询都带 `user_id` 条件，用户无法访问他人会话（返回 404）。

初始化（以 openGauss 7.0 验证）：

```sql
-- 以管理员在 postgres 库执行（每条单独执行）
CREATE USER app_ai PASSWORD '***';
CREATE DATABASE industrial_ai DBCOMPATIBILITY 'PG' OWNER app_ai;
-- 在 industrial_ai 库执行
CREATE SCHEMA app_ai AUTHORIZATION app_ai;
```

```bash
industrial-ai-db migrate    # 幂等；兼容模式不是 PG 时拒绝执行
```

注意事项：
- 必须使用 `DBCOMPATIBILITY 'PG'`：默认的 `A`（Oracle）模式会把空字符串存为 NULL。
- 初始用户（omm）不能远程连接；应用使用普通用户，`pg_hba.conf` 使用 md5 认证（标准 libpq/psycopg 不支持 openGauss 的 sha256 认证）。
- 非属主用户默认不能在 `public` 下建表，因此为应用用户创建同名 schema（默认 `search_path` 为 `"$user", public`）。
- SQL 只使用 PostgreSQL 9.2 语法（openGauss 报告的版本），不使用 `ON CONFLICT`、自增标识列等新特性。

## 用户身份

助手相关接口先校验服务令牌（调用方必须是 MES 主后端），再确定终端用户；未通过服务令牌的请求中的用户信息一律不采信。

| `USER_IDENTITY_MODE` | 用户来源 | 适用 |
| --- | --- | --- |
| `trusted_gateway`（默认） | 主后端转发的 `X-User-Id` / `X-User-Name` / `X-User-Roles` | 当前设计：主后端负责鉴权 |
| `jwt` | `X-User-Assertion` 中由客户端身份系统签发的 JWT，校验签名、`iss`、`aud`、`exp` | 客户端提供令牌后推荐：即使转发方被冒用也无法伪造用户 |
| `dev` | `X-User-Id` 或 `DEV_DEFAULT_USER`，不校验 | 仅开发；生产环境拒绝启动 |

用户以 `(issuer, subject)` 映射为内部 `user_id`，切换身份来源不会混淆不同来源的同名用户。用户角色用于知识库 `access_scope` 过滤。`GET /internal/v1/users/me` 返回当前解析出的用户，便于客户端联调。接入真实认证时只需切换 `USER_IDENTITY_MODE` 并配置密钥，接口和数据结构不变。

## 助手接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/internal/v1/assistant/chat` | 提问（无 `session_id` 时新建会话），SSE 流式返回；响应头 `X-Session-Id` |
| `POST` | `/internal/v1/assistant/sessions` | 新建会话 |
| `GET` | `/internal/v1/assistant/sessions?limit=&before=&include_archived=` | 当前用户的会话列表（按更新时间倒序，游标分页） |
| `GET` / `PATCH` / `DELETE` | `/internal/v1/assistant/sessions/{session_id}` | 查看 / 重命名、归档 / 软删除 |
| `GET` | `/internal/v1/assistant/sessions/{session_id}/messages?limit=&before_seq=` | 历史消息（含引用），向前翻页 |
| `POST` | `/internal/v1/assistant/messages/{message_id}/feedback` | 反馈 `{"rating": 1 或 -1, "comment": "..."}` |
| `GET` | `/internal/v1/users/me` | 当前用户 |

SSE 事件：`stage`（阶段，如 `retrieving_knowledge`，附检索语句）、`delta`（文本增量）、`citation`、`result`（完整消息）、`error`（`llm_unavailable` 等）、`done`。启动服务后 `/docs` 提供完整 OpenAPI 文档（生产环境关闭）。

## 数据采集（MQTT → IoTDB）

```text
数据源 ──(topic, payload)──▶ 解析器 ──Measurement──▶ 校验 ──▶ 批量 ──▶ 写入
simulator | mqtt            provisional_json | <真实格式>                iotdb | memory
```

- **数据源**：`simulator` 生成与 MQTT 报文完全相同的 `(topic, payload)`；`mqtt` 订阅 Broker（断线指数退避重连，可选 TLS、持久会话）。
- **解析器**：唯一了解报文格式的组件。剂量率统一换算为 µSv/h（支持 nSv/h、µSv/h、mSv/h、Sv/h），时间戳支持毫秒/秒和带时区的 ISO-8601。
- **校验**：设备是否在目录中、数值有限且非负、时间戳不超前；被拒报文按原因计数，不会中断采集。
- **写入**：按数量或时间批量写入 IoTDB（自动建序列，DOUBLE 类型）；写入失败保留批次并退避重试，缓冲区有上限，超出时丢弃最旧数据并计数。

临时报文格式（模拟器使用，真实格式确定前有效）：

```text
topic:   <COLLECTOR_TOPIC_PREFIX>/<site_id>/<equipment_id>/<metric>     例：sim/radiation/AS01/AS01-ISO-RM-01/dose_rate
payload: {"ts": 1767225600000, "value": 0.512, "unit": "uSv/h", "status": "ok"}
```

### 运行方式

| 场景 | 命令 |
| --- | --- |
| 部署形态：模拟设备 → MQTT → 采集 → IoTDB | `industrial-ai-collector publish-sim`（模拟设备）+ `COLLECTOR_SOURCE=mqtt industrial-ai-collector run` + API |
| 无 MQTT：模拟器直接写 IoTDB | `industrial-ai-collector run`（`COLLECTOR_SOURCE=simulator`）+ API |
| 无任何外部服务（开发） | `TIMESERIES_BACKEND=memory API_EMBEDDED_COLLECTOR=true uvicorn ...` |

采集进程与 API 进程相互独立，只通过 IoTDB 交换数据；`/health/ready` 中可查看内嵌采集器的统计。

### 模拟数据

`examples/simulation/radiation_scenario.json` 同时是设备目录和模拟配置（7 个模拟监测点：5 个隔离室区域 γ 剂量率、2 个厂界环境 γ 站）。模型包含本底、计数统计噪声（相对噪声随剂量率升高而减小）、缓慢漂移、日变化，以及随机/脚本事件：尖峰、剂量率抬升、降雨冲刷（环境站）、通信中断、数值冻结。脚本事件使最近 24 小时依次覆盖 `normal`、`watch`、`inspect`、`insufficient_data`。

> **场景中的本底值、事件和阈值全部为虚构，仅用于联调测试，不是电站数据、法规限值或报警设定值。** 默认随机种子固定，结果可复现。

### 接入真实 MQTT 时需要改动的部分

1. 在 `collector/parser.py` 新增解析类，实现 `parse(RawMessage) -> list[Measurement]`（无效报文抛出 `RejectedMessage`），注册到 `PARSERS`，设置 `COLLECTOR_PARSER`。
2. 设置 `COLLECTOR_SOURCE=mqtt` 及 `MQTT_HOST`、`MQTT_TOPICS`、认证和 TLS 参数。
3. 将 `EQUIPMENT_CATALOG_PATH` 指向真实设备清单，业务方提供阈值后填入 `thresholds`。
4. 运行集成测试确认。

数据源、校验、批量写入、IoTDB 读写、分析规则和 API 均无需修改。

### 集成测试

```bash
IOTDB_TEST_HOST=127.0.0.1 MQTT_TEST_HOST=127.0.0.1 \
MILVUS_TEST_URI=http://127.0.0.1:19530 \
OPENGAUSS_TEST_DSN="host=127.0.0.1 port=5432 dbname=industrial_ai_test user=app_ai password=***" \
pytest
```

未设置相应变量时自动跳过（Milvus 相关测试始终会在内嵌的 Milvus Lite 上运行）。

真实 DeepSeek / 智谱 GLM 接口测试（产生费用，只发送示例文档和合成问题）：

```bash
LIVE_PROVIDER_TESTS=1 pytest tests/test_live_providers.py -s
```

密钥读取：DeepSeek 为 `LLM_API_KEY`，或 `DEEPSEEK_API_KEY` / `DEEPSEEK_API`（仅在 `LLM_PROVIDER=deepseek` 时使用）；GLM 为 `EMBEDDING_API_KEY`，或 `GLM_API_KEY` / `ZHIPU_API_KEY`（仅在 `EMBEDDING_PROVIDER=zhipu` 时使用）。`OPENGAUSS_TEST_DSN` 必须指向专用测试库（每个测试前清空表）。已在 IoTDB 2.0.11、Mosquitto、Milvus 2.6.26 standalone、openGauss 7.0.0-RC3 上验证。

## 判定阈值

规则引擎不内置任何限值。指标的预警/报警限值在设备目录中配置（`thresholds`）；**未配置阈值的指标（如目前的辐射剂量率）一律判为 `insufficient_data` 并列入数据缺口，不做推测。** 判定规则见 `src/industrial_ai/analytics/rules.py` 顶部注释。`confidence` 是数据覆盖度（可判定指标占比 × 平均数据完整率），不是模型概率。

## 本地运行

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # 生产采集进程使用 MQTT 时：pip install -e ".[mqtt]"
cp .env.example .env              # 填写 COMPUTE_SERVICE_TOKEN
industrial-ai-collector run       # 模拟数据 -> IoTDB（另开终端）
industrial-ai-db migrate          # SESSION_STORE_BACKEND=opengauss 时
industrial-ai-kb build examples/knowledge/manifest.json   # 配置 EMBEDDING_* 与 MILVUS_URI 后
uvicorn --factory industrial_ai.api.main:app_factory --reload
```

```bash
pytest
ruff check .
```

未配置 `COMPUTE_SERVICE_TOKEN` 时，所有业务接口返回 503（拒绝访问）。

## 目录

```text
src/industrial_ai/
├── api/            # FastAPI 应用、依赖装配、路由（equipment / maintenance / assistant / health）
├── collector/      # 数据采集：数据源（模拟器 / MQTT）、解析、校验、批量写入、命令行
├── agent/          # 智能体循环与只读工具（知识检索、设备健康、设备读数）
├── core/           # 配置、日志、错误模型、服务认证、用户身份
├── integrations/   # 大模型客户端（DeepSeek / vLLM，流式 + 工具调用）、GLM embedding 客户端
├── knowledge/      # 知识清单、文档解析、切片、Milvus 版本化存储、离线构建、检索、命令行
├── schemas/        # Pydantic 数据模型（与设计文档 §4 接口对应）
├── storage/        # 存储接口、内存实现、IoTDB 适配、设备目录、openGauss 会话存储与迁移
├── analytics/      # 统计与确定性判定规则
├── services/       # 健康概览、维护分析、模型说明、助手
└── workers/        # 后台分析任务执行
```
