# 算力服内部 API（FastAPI）

实现 [设计文档](../docs/项目规划/设计文档.md) §4 中算力服与 MES 主后端之间的内部接口。只接受主后端调用；用户鉴权、权限和用户端通信由其他团队负责。

## 当前状态

| 能力 | 状态 |
| --- | --- |
| 服务令牌认证、请求 ID、结构化日志、统一错误格式 | 已实现 |
| 数据采集：模拟数据 / MQTT 订阅 → 解析 → 校验 → 批量写入 IoTDB | 已实现（真实 MQTT 报文格式待定，当前使用临时格式） |
| `GET /internal/v1/equipment/health` 健康概览 | 已实现（确定性规则） |
| 维护分析创建 / 查询 / SSE 事件流 / `Last-Event-ID` 断线恢复 / `Idempotency-Key` | 已实现（确定性规则 + 可选模型说明） |
| 大模型接入（DeepSeek API 测试 / 本地 vLLM 生产，OpenAI 兼容接口） | 已实现，用于维护分析说明 |
| `POST /internal/v1/assistant/chat` | 仅记录会话并返回 `knowledge_base_unavailable`；RAG 与模型尚未接入 |
| `/health/live`、`/health/ready`、`/health/model` | 已实现 |
| embedding、向量检索、rerank、助手生成、结果回写 MES | 未实现 |

## 存储

各类存储都通过 `storage/base.py` 中的接口访问，业务代码不依赖具体产品，选型确定后只需新增实现：

| 数据 | 接口 | 当前实现 | 状态 |
| --- | --- | --- | --- |
| 设备时序数据 | `TimeSeriesStore` | `iotdb`（默认）、`memory`（仅测试） | 已确定：Apache IoTDB |
| 维护分析任务与事件 | `AnalysisStore` | `memory` | 关系库未定，重启后数据丢失 |
| 助手会话与消息 | `SessionStore` | `memory` | 数据库未定，重启后数据丢失 |
| 知识向量 | — | 无 | 向量库未定 |
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
IOTDB_TEST_HOST=127.0.0.1 MQTT_TEST_HOST=127.0.0.1 pytest tests/test_integration.py
```

未设置这些变量时自动跳过。已在 IoTDB 2.0.11（standalone）和 Mosquitto 上验证。

## 判定阈值

规则引擎不内置任何限值。指标的预警/报警限值在设备目录中配置（`thresholds`）；**未配置阈值的指标（如目前的辐射剂量率）一律判为 `insufficient_data` 并列入数据缺口，不做推测。** 判定规则见 `src/industrial_ai/analytics/rules.py` 顶部注释。`confidence` 是数据覆盖度（可判定指标占比 × 平均数据完整率），不是模型概率。

## 本地运行

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # 生产采集进程使用 MQTT 时：pip install -e ".[mqtt]"
cp .env.example .env              # 填写 COMPUTE_SERVICE_TOKEN
industrial-ai-collector run       # 模拟数据 -> IoTDB（另开终端）
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
├── core/           # 配置、日志、错误模型、服务认证
├── integrations/   # 大模型客户端（OpenAI 兼容：DeepSeek / vLLM）
├── schemas/        # Pydantic 数据模型（与设计文档 §4 接口对应）
├── storage/        # 存储接口、内存实现、IoTDB 适配、设备目录
├── analytics/      # 统计与确定性判定规则
├── services/       # 健康概览、维护分析、模型说明、助手
└── workers/        # 后台分析任务执行
```
