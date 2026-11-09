# SWFH-26-122Agent

本项目面向核电站 MES 工作站，在现有监测总览之上提供设备预测性维护和智能助手两条业务线。算力服负责预测分析、完整 RAG 服务和模型推理；用户端与主服务器之间的鉴权、通信协议和接入页面由其他团队负责。

## 项目文档

- [需求文档](./docs/项目规划/需求文档.md)：项目目标、范围、功能需求、业务规则、非功能需求和验收标准。
- [设计文档](./docs/项目规划/设计文档.md)：总体架构、数据流、内部接口、RAG 服务、存储、部署和测试设计。

## 当前基线

- 设备预测性维护：设备健康概览、隔离室和备电分析、证据与维护建议。
- 智能助手：固定知识库、embedding、向量检索、rerank、只读业务工具和引用回答。
- 知识库：预先整理、版本化离线发布，不提供用户上传或在线管理界面。
- 模型：Qwen3-30B-A3B W8A8，昇腾 Atlas 300I A2 64 GB 单卡首版验证。

接口合同、部署手册、模型清单、压测报告、知识发布规范和 RAG 运维说明将在后续迭代中补充。

## 开发

```bash
# 首次克隆
git clone https://cnb.cool/SWFH-26-122/SWFH-26-122Agent.git
cd SWFH-26-122Agent
```

## 项目结构

后端的安装、配置、启动和接口说明见 [backend/README.md](./backend/README.md)。

```text
SWFH-26-122Agent/
<<<<<<< HEAD
│
├── backend/                       # Python 后端
│   ├── pyproject.toml             # Python 依赖及项目配置
│   ├── .env.example               # 环境变量示例
│   │
│   ├── src/
│   │   └── industrial_ai/
│   │       ├── __init__.py
│   │       │
│   │       ├── api/               # ① FastAPI 接口层
│   │       │   ├── main.py
│   │       │   ├── deps.py
│   │       │   └── routers/
│   │       │       ├── devices.py
│   │       │       ├── telemetry.py
│   │       │       ├── analysis.py
│   │       │       ├── alerts.py
│   │       │       └── websocket.py
│   │       │
│   │       ├── core/              # ② 全局配置
│   │       │   ├── config.py
│   │       │   ├── logging.py
│   │       │   └── security.py
│   │       │
│   │       ├── collector/         # ③ MQTT 数据采集
│   │       │   ├── main.py
│   │       │   ├── mqtt_client.py
│   │       │   ├── parser.py
│   │       │   ├── validator.py
│   │       │   └── writer.py
│   │       │
│   │       ├── storage/           # ④ 数据持久化
│   │       │   ├── iotdb_client.py
│   │       │   ├── telemetry_repo.py
│   │       │   └── metadata_repo.py
│   │       │
│   │       ├── analytics/         # ⑤ 时序数据算法
│   │       │   ├── statistics.py
│   │       │   ├── anomaly.py
│   │       │   ├── forecasting.py
│   │       │   └── rules.py
│   │       │
│   │       ├── agent/             # ⑥ LangGraph 智能体
│   │       │   ├── graph.py
│   │       │   ├── state.py
│   │       │   ├── nodes/
│   │       │   │   ├── fetch_data.py
│   │       │   │   ├── analyze.py
│   │       │   │   └── report.py
│   │       │   ├── tools/
│   │       │   │   ├── query_iotdb.py
│   │       │   │   ├── detect_anomaly.py
│   │       │   │   └── predict_trend.py
│   │       │   └── prompts/
│   │       │       └── analysis.txt
│   │       │
│   │       ├── services/          # ⑦ 业务逻辑层
│   │       │   ├── device_service.py
│   │       │   ├── analysis_service.py
│   │       │   └── alert_service.py
│   │       │
│   │       ├── workers/           # ⑧ 后台分析任务
│   │       │   └── analysis_worker.py
│   │       │
│   │       └── schemas/           # ⑨ Pydantic 数据模型
│   │           ├── telemetry.py
│   │           ├── device.py
│   │           └── analysis.py
│   │
│   └── tests/
│       ├── test_mqtt_parser.py
│       ├── test_iotdb.py
│       ├── test_analytics.py
│       └── test_api.py
│
├── frontend/                      # 独立前端页面
│   ├── package.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── App.tsx
│       ├── pages/
│       │   └── MonitorPage.tsx
│       ├── components/
│       │   ├── RealtimeChart.tsx
│       │   ├── DeviceStatus.tsx
│       │   ├── AlertList.tsx
│       │   └── AIAnalysisPanel.tsx
│       ├── api/
│       │   ├── client.ts
│       │   └── monitoring.ts
│       └── hooks/
│           └── useTelemetry.ts
│
├── deploy/                        # 部署管理
│   ├── docker-compose.yml
│   ├── Dockerfile.backend
│   └── nginx.conf
│
├── scripts/                       # 开发辅助工具
│   ├── mqtt_simulator.py
│   └── init_iotdb.py
│
├── docs/                          # 项目文档
│   ├── architecture.md
│   ├── mqtt-protocol.md
│   ├── api-contract.md
│   └── electron-integration.md
│
├── .gitignore
└── README.md
```cla

设备预测性维护这部分，
我让大模型去分析判断
=======
├── README.md
├── docs/
│   └── 项目规划/                 # 需求文档、设计文档
└── backend/                      # FastAPI + LangGraph 后端
    ├── pyproject.toml            # 依赖与项目配置
    ├── .env.example              # 全部配置项说明（复制为 .env）
    ├── README.md                 # 本地部署与使用说明
    ├── app/
    │   ├── main.py               # FastAPI 应用入口
    │   ├── config.py             # 配置（读取 .env）
    │   ├── deps.py               # 服务装配；get_current_user（登录接入点）
    │   ├── api/                  # 接口：chat（对话/会话）、devices（设备/数据）、system（健康检查）
    │   ├── agent/                # LangGraph 智能体：graph、tools、chat、llm（DeepSeek/vLLM）、prompts
    │   ├── rag/                  # 知识库：GLM embedding、离线构建、Milvus 版本化存储、检索
    │   ├── timeseries/           # IoTDB 访问、设备清单与规则判定、设备数据服务
    │   ├── ingestion/            # 报文解析、模拟数据、MQTT/文件数据源、入库管道
    │   └── db/                   # openGauss 会话存储与迁移脚本
    ├── scripts/                  # init_db、build_knowledge、simulate、ingest_mqtt、check、run
    ├── data/
    │   ├── devices.json          # 设备清单（含模拟参数）
    │   ├── knowledge/            # 示例知识库语料与 manifest
    │   └── mqtt/                 # 真实 MQTT 报文映射示例
    ├── deploy/                   # docker-compose：IoTDB、Milvus、openGauss、Mosquitto
    └── tests/
```
>>>>>>> 77cb90787459c1b132cb0a9ced030eedd3c490a4
