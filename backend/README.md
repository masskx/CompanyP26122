# 算力服内部 API（FastAPI）

实现 [设计文档](../docs/项目规划/设计文档.md) §4 中算力服与 MES 主后端之间的内部接口。只接受主后端调用；用户鉴权、权限和用户端通信由其他团队负责。

## 当前状态

| 能力 | 状态 |
| --- | --- |
| 服务令牌认证、请求 ID、结构化日志、统一错误格式 | 已实现 |
| `GET /internal/v1/equipment/health` 健康概览 | 已实现（确定性规则） |
| 维护分析创建 / 查询 / SSE 事件流 / `Last-Event-ID` 断线恢复 / `Idempotency-Key` | 已实现（确定性规则） |
| `POST /internal/v1/assistant/chat` | 仅记录会话并返回 `knowledge_base_unavailable`；RAG 与模型尚未接入 |
| `/health/live`、`/health/ready`、`/health/model` | 已实现 |
| vLLM 解释、embedding、向量检索、rerank、结果回写 MES | 未实现 |

## 待定的存储选型

各类存储都通过 `storage/base.py` 中的接口访问，业务代码不依赖具体产品，选型确定后只需新增实现：

| 数据 | 接口 | 当前实现 | 状态 |
| --- | --- | --- | --- |
| 设备时序数据 | `TimeSeriesStore` | `memory`（默认）、`iotdb`（可选） | IoTDB 为候选，尚未决定 |
| 维护分析任务与事件 | `AnalysisStore` | `memory` | 关系库未定，重启后数据丢失 |
| 助手会话与消息 | `SessionStore` | `memory` | 数据库未定，重启后数据丢失 |
| 知识向量 | — | 无 | 向量库未定 |
| 设备主数据 | `EquipmentCatalog` | JSON 文件 | 待 MES 主数据接口 |

> IoTDB 是时序数据库，不是向量数据库。RAG 所需的向量库需要单独选型。

IoTDB 路径约定（需与数据写入方确认）：`<IOTDB_ROOT_PATH>.<site_id>.<equipment_id>.<metric>`，时间戳按毫秒精度。

## 判定阈值

规则引擎不内置任何限值。指标的预警/报警限值在设备目录中配置（`thresholds`）；**未配置阈值的指标（如目前的辐射剂量率）一律判为 `insufficient_data` 并列入数据缺口，不做推测。** 判定规则见 `src/industrial_ai/analytics/rules.py` 顶部注释。`confidence` 是数据覆盖度（可判定指标占比 × 平均数据完整率），不是模型概率。

## 本地运行

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"          # 使用 IoTDB 时：pip install -e ".[dev,iotdb]"
cp .env.example .env              # 填写 COMPUTE_SERVICE_TOKEN
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
├── schemas/        # Pydantic 数据模型（与设计文档 §4 接口对应）
├── storage/        # 存储接口、内存实现、IoTDB 适配、设备目录
├── analytics/      # 统计与确定性判定规则
├── services/       # 健康概览、维护分析、助手
└── workers/        # 后台分析任务执行
```
