# 算力服内部 API（FastAPI）

实现 [设计文档](../docs/项目规划/设计文档.md) §4 中算力服与 MES 主后端之间的内部接口。只接受主后端调用；用户鉴权、权限和用户端通信由其他团队负责。

## 当前状态

| 能力 | 状态 |
| --- | --- |
| 服务令牌认证、请求 ID、结构化日志、统一错误格式 | 已实现 |
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

## 判定阈值

规则引擎不内置任何限值。指标的预警/报警限值在设备目录中配置（`thresholds`）；**未配置阈值的指标（如目前的辐射剂量率）一律判为 `insufficient_data` 并列入数据缺口，不做推测。** 判定规则见 `src/industrial_ai/analytics/rules.py` 顶部注释。`confidence` 是数据覆盖度（可判定指标占比 × 平均数据完整率），不是模型概率。

## 本地运行

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # 填写 COMPUTE_SERVICE_TOKEN；无 IoTDB 时设 TIMESERIES_BACKEND=memory
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
├── core/           # 配置、日志、错误模型、服务认证
├── integrations/   # 大模型客户端（OpenAI 兼容：DeepSeek / vLLM）
├── schemas/        # Pydantic 数据模型（与设计文档 §4 接口对应）
├── storage/        # 存储接口、内存实现、IoTDB 适配、设备目录
├── analytics/      # 统计与确定性判定规则
├── services/       # 健康概览、维护分析、模型说明、助手
└── workers/        # 后台分析任务执行
```
