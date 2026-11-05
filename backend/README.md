# Radiation monitoring AI backend

FastAPI + LangGraph backend for the nuclear-plant MES workstation:

- **Dose-rate data** from radiation monitors, stored in **Apache IoTDB** (simulated now, real
  MQTT feed later), with REST endpoints and rule-based health assessment.
- An **AI assistant** built with **LangGraph**. The model decides on its own when to search the
  technical **knowledge base** (Milvus + GLM embeddings) or query device data, and cites its
  sources.
- **Conversations** stored in **openGauss**, keyed by user, ready for the client's login and
  user table.

---

## Project structure

```text
backend/
├── app/
│   ├── main.py            FastAPI app (routers, lifespan)
│   ├── config.py          all settings (from .env)
│   ├── deps.py            builds services once; get_current_user() (no login yet)
│   ├── api/               HTTP routes: chat.py, devices.py, system.py
│   ├── agent/             LangGraph assistant
│   │   ├── graph.py         StateGraph: agent ⇄ ToolNode
│   │   ├── tools.py         @tool functions: knowledge search + device data
│   │   ├── chat.py          one chat turn: history → graph (streaming) → persistence
│   │   ├── llm.py           ChatOpenAI for DeepSeek / vLLM
│   │   └── prompts.py       system prompt
│   ├── rag/               knowledge base
│   │   ├── embeddings.py    GLM embedding-3 as a LangChain Embeddings
│   │   ├── ingest.py        offline build: files → LangChain splitters → GLM → Milvus
│   │   ├── store.py         versioned Milvus collections + alias
│   │   └── retriever.py     query-time search
│   ├── timeseries/        IoTDB client, device catalog + rules, device service
│   ├── ingestion/         message formats, simulator, MQTT/file sources, pipeline
│   └── db/                openGauss conversation store + migrations
├── scripts/               init_db, build_knowledge, simulate, ingest_mqtt, check, run
├── data/
│   ├── devices.json       device catalog (+ simulation profiles)
│   ├── knowledge/         sample corpus + manifest.json
│   └── mqtt/              example mapping for the real MQTT format
├── deploy/                docker-compose.yml (IoTDB, Milvus, openGauss, Mosquitto)
└── tests/
```

---

## Quick start

Prerequisites: Python **3.12+**, and Docker for the bundled databases (or your own
IoTDB / Milvus / openGauss / MQTT). Run every command from **`backend/`**.

```bash
# 1. install
python -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev,mqtt]"

# 2. databases (skip if you use your own; then edit .env)
docker compose -f deploy/docker-compose.yml up -d

# 3. configuration
cp .env.example .env                       # Windows: Copy-Item .env.example .env
```

Edit `.env` to turn on the AI. Keys can also come from the environment variables
`DEEPSEEK_API` and `GLM_API_KEY`:

```ini
LLM_PROVIDER=deepseek
EMBEDDING_PROVIDER=zhipu
```

```bash
# 4. openGauss tables + IoTDB series (admin DSN only on the first run)
python scripts/init_db.py --admin-dsn "host=127.0.0.1 port=15432 dbname=postgres user=gaussdb password=Gauss@Local2026"

# 5. vector database: build the knowledge base (sample corpus in data/knowledge)
python scripts/build_knowledge.py

# 6. simulated dose-rate data: 48 h history, then live readings (keep it running)
python scripts/simulate.py

# 7. start the API (another terminal)
python scripts/run.py                      # http://127.0.0.1:8000/docs

# 8. verify everything end to end (another terminal)
python scripts/check.py --api http://127.0.0.1:8000
```

`check.py` verifies each service, then calls the API: health, overview, assessment, telemetry,
a device question (the agent must use a data tool), a knowledge question (it must search
Milvus and cite a document), and saved history.

**Already initialised your databases with the previous version?** The IoTDB layout is
unchanged, so existing data keeps working. openGauss gets new tables (`chat_session`,
`chat_message`, `app_migrations`): run `python scripts/init_db.py` once. The old tables
(`app_users`, `chat_sessions`, `chat_messages`, `message_citations`, `message_feedback`,
`schema_migrations`) are unused and can be dropped.

---

## Configuration

Everything is in `.env`; `.env.example` documents every setting. The main groups:

| Group | Settings |
| --- | --- |
| IoTDB | `IOTDB_HOST`, `IOTDB_PORT`, `IOTDB_USER`, `IOTDB_PASSWORD`, `IOTDB_ROOT` |
| openGauss | `OPENGAUSS_DSN` (empty = conversations in memory) |
| LLM | `LLM_PROVIDER` = `none` / `deepseek` / `vllm`, `LLM_API_KEY` (or `DEEPSEEK_API`), `LLM_BASE_URL`, `LLM_MODEL` |
| Knowledge | `EMBEDDING_PROVIDER` = `none` / `zhipu` / `local`, `EMBEDDING_API_KEY` (or `GLM_API_KEY`), `EMBEDDING_DIMENSIONS`, `MILVUS_URI`, `KB_NAME` |
| Ingestion | `MQTT_*`, `INGEST_PARSER`, `INGEST_MAPPING_FILE` |
| Access | `API_KEY` (optional shared key; empty = open) |

Switching to the local model later is configuration only:

```ini
LLM_PROVIDER=vllm
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=<--served-model-name>
```

vLLM must allow tool calling; for Qwen3 that is
`vllm serve ... --enable-auto-tool-choice --tool-call-parser hermes`. DeepSeek and the Zhipu
API are public services and are refused when `APP_ENV=production`.

---

## API

Base path `/api/v1`. Interactive documentation is at `/docs`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | status of IoTDB, openGauss, knowledge base, LLM |
| GET | `/knowledge` | knowledge-base versions (read-only) |
| GET | `/devices?site=&type=` | devices with latest readings |
| GET | `/devices/{id}` | one device |
| GET | `/devices/{id}/telemetry?start=&end=&interval=` | raw points, or avg/min/max/count per `interval` seconds |
| GET | `/devices/{id}/assessment?hours=24` | rule-based health level + statistics |
| GET | `/overview?site=&hours=24` | health level of every device |
| POST | `/chat` | ask the assistant (see below) |
| GET / POST | `/sessions` | the user's sessions / create one |
| GET / PATCH / DELETE | `/sessions/{id}` | view / rename / delete |
| GET | `/sessions/{id}/messages?limit=&before_seq=` | history with citations |
| POST | `/messages/{id}/feedback` | `{"rating": 1}` or `-1` |

**Chat.** `POST /api/v1/chat` with `{"message": "...", "session_id": null, "stream": true}`.
Omit `session_id` to start a new session. With `"stream": true` the response is
Server-Sent Events:

```text
event: session   data: {"session_id": "...", "user_message_id": "..."}
event: tool      data: {"name": "get_device_status", "args": {"device_id": "AS01-ISO-RM-02"}}
event: delta     data: {"text": "AS01-ISO-RM-02 最近"}            (repeated)
event: citation  data: {"ref": 1, "kind": "data", "title": "...", ...}
event: done      data: {"message": {...stored assistant message...}}
event: error     data: {"code": "agent_failed", "message": "..."}
```

With `"stream": false` you get one JSON reply: `{"session_id": ..., "message": {...}}`.

```bash
curl -N http://127.0.0.1:8000/api/v1/chat -H "Content-Type: application/json" -d '{"message": "AS01 站哪些设备需要关注？"}'
```

**Users (no login yet).** `get_current_user()` in `app/deps.py` reads the optional `X-User-Id`
header, or uses `anonymous`. This identifies the user but does not authenticate them. Every
session row stores `user_id`, and every query is scoped to it. When the client application
adds login, verify its token in that one function and return the id from the client's user
table; the routes and tables stay as they are.

---

## How the assistant works (LangGraph)

```text
START → agent ──(tool calls?)──► tools (ToolNode) ──► agent ──► … ──► END
             └──(answer)───────────────────────────────────────────►
```

- **`agent` node.** `ChatOpenAI.bind_tools(...)` with the tools below. LangChain handles the
  OpenAI function-calling format, including streamed tool-call fragments.
- **The model decides.** It searches the knowledge base when an answer needs technical facts
  or a source, queries device data for status, trend or anomaly questions, and answers
  greetings and follow-ups directly. After `AGENT_MAX_TOOL_ROUNDS` rounds the model is called
  without tools, so every question gets an answer.
- **Tools** (`app/agent/tools.py`, all read-only):

  | Tool | What it does |
  | --- | --- |
  | `search_knowledge` | searches Milvus |
  | `list_devices` | devices and their latest readings |
  | `get_site_overview` | health level of every device at a site |
  | `get_device_status` | statistics, thresholds and rule verdict for one device |
  | `get_telemetry` | aggregated curve for trend questions |

- **Citations.** Tool results are numbered `[1]`, `[2]`, … across one answer. The sources the
  answer refers to are returned as `citation` records and stored with the message, together
  with the knowledge version, model, token counts and the tool calls made.
- **Grounding.** The system prompt requires a source for every fact, an explicit "no basis
  found" when nothing is found, and no device-control instructions. Health levels always come
  from the rules, never from the model.
- **History.** The last `AGENT_HISTORY_MESSAGES` messages of the session are sent with each
  question. openGauss is the source of truth; LangGraph's Postgres checkpointer is not used,
  because it relies on PostgreSQL features openGauss does not reliably provide.

---

## Knowledge base (vector database)

Documents are prepared in advance and indexed offline. There is no upload API.

```bash
python scripts/build_knowledge.py                       # data/knowledge → new version, activated
python scripts/build_knowledge.py --corpus /path/docs   # your documents
python scripts/build_knowledge.py --dry-run             # show chunks only (no API calls)
python scripts/build_knowledge.py list                  # versions; which is active
python scripts/build_knowledge.py activate <version>    # switch / roll back instantly
python scripts/build_knowledge.py drop <version>
python scripts/build_knowledge.py search "剂量率单位换算"
```

- **Formats.** Markdown (split by headings, so sections are kept for citations), text, and
  PDF (`pip install -e ".[pdf]"`, page numbers kept). Text is split with LangChain splitters
  tuned for Chinese punctuation.
- **`manifest.json`** (optional) gives each document a title, source, classification,
  access scope and SHA-256; files that don't match their checksum are refused. Without a
  manifest every file is indexed and the version is a timestamp.
- **Versions.** Each build is its own collection (`<KB_NAME>__<version>`) and the alias
  `<KB_NAME>` is switched atomically. A failed build is removed; the active version is never
  touched.
- **Embedding consistency.** The model and dimensions are stored with each version. The API
  refuses to search a version built with a different `EMBEDDING_MODEL` /
  `EMBEDDING_DIMENSIONS`, so changing either means rebuilding.
- The sample corpus only describes this system and unit conversions. It is not plant
  documentation.

---

## Simulated data and the real MQTT feed

```text
SimulatedSource / MqttSource / FileSource → parser → validate → batch → IoTDB
```

**Simulated data now.** `data/devices.json` defines seven synthetic monitors with
simulation-only thresholds. Scripted events make the last 24 hours show every health level:
RM-02 inspect, RM-04 watch, RM-03 insufficient_data (a 5.5-hour outage), the rest normal.

```bash
python scripts/simulate.py                      # history + live until Ctrl+C
python scripts/simulate.py --hours 24 --no-live # history only
python scripts/simulate.py --duration 30        # stop after 30 min
python scripts/simulate.py --target mqtt        # publish to the broker instead
python scripts/simulate.py --purge --yes        # remove simulated series
```

For a demo without any database, set `TIMESERIES_BACKEND=memory`, `OPENGAUSS_DSN=` and
`DEMO_SIMULATION=true`, then run `python scripts/run.py`.

**Real MQTT later.** Only the parser changes. Storage, API and agent stay the same.

1. Capture a few real messages into a JSON Lines file (`{"topic": ..., "payload": ...}` per
   line).
2. Describe the format in a mapping file. Copy `data/mqtt/mapping.example.json`, which covers
   a topic regex, field paths, lists of readings per message, units (converted to µSv/h),
   accepted status values and a vendor-ID → `device_id` map.
3. Test offline, storing nothing:
   ```bash
   python scripts/ingest_mqtt.py --file captured.jsonl --dry-run --parser mapping --mapping my_mapping.json
   ```
4. Test against the broker, still storing nothing:
   ```bash
   python scripts/ingest_mqtt.py --dry-run --host <broker> --topics "plant/#" --parser mapping --mapping my_mapping.json
   ```
5. Put the real devices in `data/devices.json` (omit thresholds until the plant provides them;
   those metrics report `insufficient_data`).
6. Set `INGEST_PARSER=mapping`, `INGEST_MAPPING_FILE`, `MQTT_*` in `.env`, stop `simulate.py`,
   and run `python scripts/ingest_mqtt.py`.

For formats a mapping cannot describe, write a class with `parse(RawMessage) ->
list[Measurement]` and set `INGEST_PARSER=yourmodule:YourParser`.

Ingestion reconnects automatically. With `MQTT_CLIENT_ID` set, the broker keeps a persistent
session, so messages wait while the ingester restarts. Writes are retried while IoTDB is down,
with a bounded buffer. Rejected messages are counted by reason (`unknown_device`, `bad_value`,
`device_status`, …) and printed on exit.

---

## Testing

```bash
pytest                                          # unit tests (no services needed)
IOTDB_TEST_HOST=127.0.0.1 MQTT_TEST_HOST=127.0.0.1 MILVUS_TEST_URI=http://127.0.0.1:19530 OPENGAUSS_TEST_DSN="host=127.0.0.1 port=15432 dbname=industrial_ai_test user=app_ai password=AppAi@Local2026" pytest
LIVE_TESTS=1 pytest tests/test_live.py -s       # real DeepSeek + GLM
```

Use a dedicated openGauss test database; its chat tables are emptied. Create it with
`OPENGAUSS_DSN="...dbname=industrial_ai_test..." python scripts/init_db.py --admin-dsn ...`.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `invalid configuration: DeepSeek needs LLM_API_KEY ...` | set the key, or set `LLM_PROVIDER=none` |
| `/health` → knowledge `unavailable: ... no active version` | run `python scripts/build_knowledge.py` |
| knowledge `... built with {...}, queries use {...}` | embedding model/dimensions changed: rebuild |
| openGauss `permission denied for schema public` / `database ... does not exist` | first run of `init_db.py` needs `--admin-dsn` |
| openGauss `compatibility is 'A'` | the database must be created with `DBCOMPATIBILITY 'PG'` (`init_db.py` does this) |
| most devices `insufficient_data` | less than ~20 h of data in the last 24 h: run `simulate.py` (48 h history) |
| chat `503 llm_not_configured` | `LLM_PROVIDER=none` |
| Windows: `ProactorEventLoop` error | start with `python scripts/run.py`, not plain `uvicorn` |

## Before production

- Use the local vLLM model and a private GLM embedding (`EMBEDDING_PROVIDER=local`).
- Change every password from the Docker stack.
- Set `API_KEY` or restrict the network.
- Configure the MQTT broker with authentication and TLS.
- Replace `get_current_user()` with real authentication once the client provides login.
