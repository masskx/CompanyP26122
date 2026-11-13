# Compute-server backend (算力服 FastAPI)

FastAPI + LangGraph service on the compute server of the MES environmental-monitoring
workstation (`docs/原型/打开工作站.html`). It does two things:

- **Predictive maintenance analysis.**
  - The main server publishes station data over MQTT (`maintenance/v1/...`); this service
    stores it in **Apache IoTDB**.
  - A timer on the main server calls `POST /internal/v1/maintenance/analyses`, and this service
    returns the structured result shown in the frontend's "AI 综合分析": overall judgement
    (综合判断), key evidence (关键依据), suggested actions (建议动作) and data limitations.
  - Rules decide; the model writes the explanation and chooses the primary action among the
    preset ones.
  - The main server owns work orders and pushes a copy here.
- **Assistant (智能小助手).** Four preset LangGraph workflows (monitoring report, system
  information, dose-rate assessment, all-station maintenance advice) plus free conversation over
  the knowledge base (**Milvus** + GLM embeddings) and read-only data tools. Conversations are
  stored in **openGauss**.

**Storage:**
- **openGauss:** only conversations (+ report records) and the work-order copy.
- **IoTDB:** the time series.
- **`data/stations.json`:** the station directory (thresholds etc.).

Details:
- [docs/maintenance-analysis.md](docs/maintenance-analysis.md): analysis interface, rules,
  actions, MQTT contract, demo scenario, points to confirm.
- [docs/assistant.md](docs/assistant.md): workflows, SSE events, **conversation history without
  roles**.
- [data/knowledge/README.md](data/knowledge/README.md): preparing the knowledge base.

---

## Project structure

```text
backend/
├── app/
│   ├── main.py            FastAPI app; routers under /api/v1 and /internal/v1
│   ├── config.py          all settings (from .env)
│   ├── deps.py            builds services once; get_current_user() (X-User-Id / X-Client-Id)
│   ├── api/               maintenance.py (internal), chat.py (assistant), stations.py, system.py
│   ├── maintenance/       analysis: rules.py, detect.py, actions.py, narrator.py, service.py,
│   │                      schemas.py (the §4.2 contract)
│   ├── agent/             assistant: graph.py (router + workflows), workflows.py, reports.py,
│   │                      tools.py, chat.py, prompts.py, llm.py
│   ├── rag/               knowledge base: embeddings, ingest (md/txt/pdf/docx), store, retriever,
│   │                      rerank
│   ├── stations/          station directory model / file store, station data reads
│   ├── ingestion/         telemetry_v1 parser, simulator, MQTT/file sources, pipeline + watermark
│   ├── timeseries/        IoTDB client
│   └── db/                openGauss: conversations, work-order copy, migrations
├── scripts/               init_db, simulate, ingest_mqtt, analyze, build_knowledge, check, run
├── data/
│   ├── stations.json      station directory (prototype codes; SYNTHETIC thresholds + simulation)
│   ├── maintenance/actions.json   versioned action catalogue
│   ├── knowledge/         sample corpus + manifest + README (how to prepare the real one)
│   └── mqtt/              example mapping for another vendor format
├── docs/                  maintenance-analysis.md, assistant.md
├── deploy/                docker-compose.yml (IoTDB, Milvus, openGauss, Mosquitto)
└── tests/
```

---

## Run it locally

Prerequisites: Python **3.12+**, and Docker for the bundled databases (or your own IoTDB /
Milvus / openGauss / MQTT). Run every command from **`backend/`**.

```bash
# 1. install (or: uv sync --extra dev --extra mqtt --extra docs)
python -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev,mqtt,docs]"

# 2. databases (skip if you use your own; then edit .env)
docker compose -f deploy/docker-compose.yml up -d

# 3. configuration (re-copy after pulling: settings changed)
cp .env.example .env                       # Windows: Copy-Item .env.example .env
```

Edit `.env` to choose the model:
- **Development:** `LLM_PROVIDER=deepseek` and `EMBEDDING_PROVIDER=zhipu`. The keys can come from
  the environment variables `DEEPSEEK_API` and `GLM_API_KEY`.
- **Local vLLM:** `LLM_PROVIDER=vllm`, `LLM_BASE_URL=http://<host>:8000/v1`, `LLM_MODEL=qwen3-14b`
  and `LLM_API_KEY`.

```bash
# 4. openGauss tables (admin DSN only on the first run) + IoTDB series
python scripts/init_db.py --admin-dsn "host=127.0.0.1 port=15432 dbname=postgres user=gaussdb password=Gauss@Local2026"

# 5. simulated station data in the main server's MQTT contract (48 h, then live)
python scripts/simulate.py                 # keep it running (or --hours 26 --no-live)

# 6. try the analysis the main server will call
python scripts/analyze.py --all            # one line per station
python scripts/analyze.py --site AS06      # findings, actions, explanation

# 7. knowledge base (sample corpus; see data/knowledge/README.md)
python scripts/build_knowledge.py

# 8. API
python scripts/run.py                      # http://127.0.0.1:8000/docs

# 9. everything end to end (another terminal)
python scripts/check.py --api http://127.0.0.1:8000
```

With the frontend: start the Go `main_server` with
`COMPUTE_SERVICE_BASE_URL=http://127.0.0.1:8000` (its `/api/v1/assistant/chat` proxies to this
service), then `npm run dev` in `frontend/`.

**Already ran the previous version?**
1. Pull, then re-copy `.env.example` to `.env`; MQTT topics, parser and settings changed.
2. Run `pip install -e ".[dev,mqtt,docs]"`.
3. Run `python scripts/init_db.py`. Migration 003 drops the version-0.3 tables
   (`station_config`, `analysis_*`) and adds `maintenance_work_order`, `assistant_report` and
   `chat_session.workflow`.
4. Replace the old IoTDB data, whose layout changed: run
   `python scripts/simulate.py --purge --yes`, then `python scripts/init_db.py --only iotdb`,
   then `python scripts/simulate.py`.
5. If you recreated `data/stations.json` yourself, move your copy away before pulling. The
   repository now ships the prototype's directory.

The six tables of the very first version (`app_users`, `chat_sessions`, `chat_messages`,
`message_citations`, `message_feedback`, `schema_migrations`) are unused and can be dropped.

**Without any database:** set `TIMESERIES_BACKEND=memory`, `OPENGAUSS_DSN=` and
`DEMO_SIMULATION=true` in `.env`, then run `python scripts/run.py`.

---

## Configuration

Everything is in `.env`; `.env.example` documents every setting.

| Group | Main settings |
| --- | --- |
| Access | `COMPUTE_SERVICE_TOKEN` (Bearer, main server), `API_KEY` (X-API-Key, current Go proxy); both empty = open |
| IoTDB | `IOTDB_HOST`, `IOTDB_PORT`, `IOTDB_USER`, `IOTDB_PASSWORD`, `IOTDB_ROOT` |
| openGauss | `OPENGAUSS_DSN` (empty = in memory) |
| MQTT | `MQTT_HOST`, `MQTT_TOPICS` (`maintenance/v1/#`), `MQTT_CLIENT_ID` (persistent session), `INGEST_PARSER=telemetry_v1` |
| LLM | `LLM_PROVIDER` = `none` / `deepseek` / `vllm`, `LLM_BASE_URL` (or `VLLM_BASE_URL`), `LLM_MODEL` (or `VLLM_MODEL_NAME`), `LLM_API_KEY` (or `DEEPSEEK_API`) |
| Analysis | `ANALYSIS_WATERMARK_WAIT_SECONDS`, `MAINTENANCE_*` versions / narrator, rule parameters `ANALYSIS_*`, `MES_API_BASE_URL` (history) |
| Assistant | `ASSISTANT_INTENT_MODEL`, `REPORTS_DIR`, `SSE_HEARTBEAT_SECONDS` |
| Knowledge | `EMBEDDING_PROVIDER`, `EMBEDDING_*`, `MILVUS_URI`, `KB_*`, `RERANK_*` |

DeepSeek and the Zhipu API are public services. They are refused when `APP_ENV=production`;
production uses the local vLLM and a private embedding service. vLLM must allow tool calling
(`--enable-auto-tool-choice --tool-call-parser hermes` for Qwen3).

---

## API

**Main server → compute server** (`/internal/v1`, service token):

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/internal/v1/maintenance/analyses` | synchronous analysis (design doc §4.2) |
| PUT | `/internal/v1/maintenance/work-orders/{event_id}` | push a work order / published result / review (copy) |
| GET | `/internal/v1/maintenance/work-orders[/{event_id}]` | read the copy |
| POST | `/internal/v1/assistant/chat` | assistant SSE (same as `/api/v1/chat`) |

**Assistant** (`/api/v1`; the Go proxy calls `/api/v1/chat`): `POST /chat`,
`GET /assistant/workflows`, `GET /sessions`, `GET|PATCH|DELETE /sessions/{id}`,
`GET /sessions/{id}/messages`, `POST /messages/{id}/feedback`, `GET /reports`,
`GET /reports/{id}/download`, `POST /sessions/claim`. See [docs/assistant.md](docs/assistant.md).

**Data and health:**
- `GET /api/v1/health`: IoTDB, openGauss, stations, MQTT watermark, knowledge base, LLM and
  rule / catalogue versions.
- `GET /api/v1/stations`, `/stations/{id}` (latest values), `/stations/{id}/telemetry`.
- `GET /api/v1/knowledge`.

---

## MQTT: simulator now, the main server later

```text
main server publisher (later) / scripts/simulate.py --target mqtt
   -> Mosquitto -> scripts/ingest_mqtt.py -> IoTDB (+ watermark)
scripts/simulate.py (default) writes straight to IoTDB
```

**The contract is the design document's:**
- topic `maintenance/v1/{site_id}/{equipment_id}/telemetry`, QoS 1;
- each message has a `message_id`, a monotonically increasing `source_sequence` and points with
  quality flags.

The simulator publishes exactly this. When the main server publishes it too, nothing here
changes: run `python scripts/ingest_mqtt.py` with `MQTT_HOST` and `MQTT_CLIENT_ID` set.

**If the real messages look different:**
- describe them with a mapping file (`INGEST_PARSER=mapping`, see `data/mqtt/`); or
- write a parser class (`INGEST_PARSER=module:Class`).

Test against captured messages without storing anything:
`python scripts/ingest_mqtt.py --file captured.jsonl --dry-run`.

---

## Testing

```bash
pytest                                          # no services needed
IOTDB_TEST_HOST=127.0.0.1 MQTT_TEST_HOST=127.0.0.1 MILVUS_TEST_URI=http://127.0.0.1:19530 OPENGAUSS_TEST_DSN="host=127.0.0.1 port=15432 dbname=industrial_ai_test user=app_ai password=AppAi@Local2026" pytest
LIVE_TESTS=1 pytest tests/test_live.py -s       # real DeepSeek + GLM
```

Use a dedicated openGauss test database. Tests write chat rows, work orders and reports named
`tst-*`, and remove them afterwards.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `station file not found: data/stations.json` | run commands from `backend/`; pull the latest version (the file is in the repository) |
| analysis `503 watermark_not_reached` | the ingester has not written up to the requested `mqtt_watermark`; check `ingest_mqtt.py` and `/health` → `mqtt_watermark` |
| every analysis `insufficient_data` | no data in the window: run `simulate.py` (or check the MQTT feed) |
| `insufficient_data` with `config.dose_thresholds_missing` | the station has no `dose_l1` / `dose_l2` in `data/stations.json` |
| `invalid configuration: DeepSeek needs LLM_API_KEY ...` | set the key, or `LLM_PROVIDER=none` |
| `/health` → knowledge `unavailable: ... no active version` | run `python scripts/build_knowledge.py` |
| knowledge `... built with {...}, queries use {...}` | embedding model or dimensions changed: rebuild |
| openGauss `permission denied for schema public` / `database ... does not exist` | the first run of `init_db.py` needs `--admin-dsn` |
| openGauss `compatibility is 'A'` | the database must use `DBCOMPATIBILITY 'PG'` (`init_db.py` creates it so) |
| chat `503 llm_not_configured` | `LLM_PROVIDER=none` |
| Windows: `ProactorEventLoop` error | start with `python scripts/run.py`, not plain `uvicorn` |
| Windows: `unknown ANALYSIS_TIMEZONE` | `pip install tzdata` (installed with the project on Windows) |

## Before production

- Use the local vLLM model and a private embedding service.
- Set `COMPUTE_SERVICE_TOKEN` and `API_KEY`, and restrict the network to the main server.
- Change every password from the Docker stack, and enable MQTT authentication and TLS.
- Replace the synthetic thresholds in `data/stations.json` with the main server's configuration.
- Confirm the points in [docs/maintenance-analysis.md](docs/maintenance-analysis.md#to-confirm).
- Forward `X-Client-Id` (or the logged-in `X-User-Id`) through the main server, so each user
  sees only their own conversations.
