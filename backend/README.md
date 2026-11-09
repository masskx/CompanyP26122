# Radiation monitoring AI backend

FastAPI + LangGraph backend for the nuclear-plant MES workstation:

- **Fixed monitoring stations**: station / device / dose-rate / communication / battery /
  shelter data stored in **Apache IoTDB** (simulated now, the real MQTT or MES feed later).
- A **daily analysis** at 00:30: deterministic rules find the abnormal intervals of the
  previous day, one record per problem device (and per station battery). The language model
  only writes the explanation from the rules' evidence. Results go to **openGauss** and are
  read by the frontend through GET APIs. Design and data contract:
  [docs/station-analysis.md](docs/station-analysis.md).
- An **AI assistant** built with **LangGraph**. The model decides on its own when to search the
  technical **knowledge base** (Milvus + GLM embeddings) or query station data and analysis
  results, and cites its sources.
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
│   ├── api/               HTTP routes: analysis.py, stations.py, chat.py, system.py
│   ├── agent/             LangGraph assistant
│   │   ├── graph.py         StateGraph: agent ⇄ ToolNode
│   │   ├── tools.py         @tool functions: knowledge search, station data, analysis results
│   │   ├── chat.py          one chat turn: history → graph (streaming) → persistence
│   │   ├── llm.py           ChatOpenAI for DeepSeek / vLLM
│   │   └── prompts.py       system prompt
│   ├── rag/               knowledge base
│   │   ├── embeddings.py    GLM embedding-3 as a LangChain Embeddings
│   │   ├── ingest.py        offline build: files → LangChain splitters → GLM → Milvus
│   │   ├── store.py         versioned Milvus collections + alias
│   │   └── retriever.py     query-time search
│   ├── stations/          data contract (model.py), station_config store, station data reads
│   ├── analysis/          daily analysis
│   │   ├── rules.py         detection rules (pure functions): intervals, gaps, stuck, battery
│   │   ├── analyzer.py      one station + one day -> records / events / comparisons / context
│   │   ├── explain.py       evidence summary, LLM explanation + validation, template, confidence
│   │   ├── batch.py         batches (idempotent, revisions, partial) + 00:30 scheduler
│   │   ├── store.py         openGauss / memory storage of batches, records, events
│   │   └── models.py        API contract (Batch, Record, Event, Explanation)
│   ├── timeseries/        IoTDB client
│   ├── ingestion/         message formats, station simulator, MQTT/file sources, pipeline
│   └── db/                openGauss pool, conversation store, migrations
├── scripts/               init_db, simulate, analyze, ingest_mqtt, build_knowledge, check, run
├── docs/station-analysis.md  design, data contract, rules, decisions to confirm
├── data/
│   ├── stations.json      station configuration seed (+ simulation profiles; synthetic)
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
# 4. openGauss tables + station configuration + IoTDB series (admin DSN only on the first run)
python scripts/init_db.py --admin-dsn "host=127.0.0.1 port=15432 dbname=postgres user=gaussdb password=Gauss@Local2026"

# 5. simulated station data: 48 h history (yesterday holds the scripted anomalies), then live
python scripts/simulate.py                 # keep it running; --hours 200 --no-live for comparisons

# 6. analyse yesterday once by hand (the API also does it every night at 00:30)
python scripts/analyze.py --show

# 7. vector database for the assistant (sample corpus in data/knowledge)
python scripts/build_knowledge.py

# 8. start the API (another terminal)
python scripts/run.py                      # http://127.0.0.1:8000/docs

# 9. verify everything end to end (another terminal)
python scripts/check.py --api http://127.0.0.1:8000
```

`check.py` verifies each service, then calls the API: health, stations, telemetry, the analysis
status, a batch request (polled until done), records and a record detail, a question about
yesterday's results (the agent must use a data tool), a knowledge question (it must search
Milvus and cite a document), and saved history.

**Already initialised your databases with the previous version?** Run
`python scripts/init_db.py` once more. It applies migration `002_station_analysis`
(`station_config`, `analysis_batch`, `analysis_record`, `analysis_event`), inserts the stations
of `data/stations.json`, and creates the new IoTDB series. The old demo series (`AS01…`) no
longer match any station: delete them with `python scripts/simulate.py --purge --yes` (this
removes everything under `IOTDB_ROOT`), then run `python scripts/init_db.py --only iotdb` and
`simulate.py`. The conversation tables are unchanged.

---

## Configuration

Everything is in `.env`; `.env.example` documents every setting. The main groups:

| Group | Settings |
| --- | --- |
| IoTDB | `IOTDB_HOST`, `IOTDB_PORT`, `IOTDB_USER`, `IOTDB_PASSWORD`, `IOTDB_ROOT` |
| openGauss | `OPENGAUSS_DSN` (empty = stations from `STATIONS_FILE`, results and conversations in memory) |
| Analysis | `ANALYSIS_TIMEZONE`, `ANALYSIS_RUN_AT` (00:30), `ANALYSIS_SCHEDULE_ENABLED`, `ANALYSIS_VERSION`, `ANALYSIS_CATCHUP_DAYS`, `ANALYSIS_MAX_ATTEMPTS`, rule parameters (`ANALYSIS_CONSECUTIVE_SAMPLES`, `ANALYSIS_GAP_INTERVALS`, `ANALYSIS_STUCK_MINUTES`, `ANALYSIS_FAULT_LEVEL`, …), `ANALYSIS_USE_LLM` |
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
| GET | `/health` | status of IoTDB, openGauss, stations, scheduler, knowledge base, LLM |
| GET | `/knowledge` | knowledge-base versions (read-only) |
| GET | `/analysis/status` | schedule, yesterday's batch, latest batch, running batches |
| GET | `/analysis/batches?date_from=&date_to=&status=` | batch list |
| GET | `/analysis/batches/{id}?include_snapshot=` | one batch (poll this after POST) |
| POST | `/analysis/batches` `{"date": null, "force": false}` | run / re-run a finished day (202 + `poll`, or 200 with the existing batch) |
| GET | `/analysis/records?date=&batch_id=&station_id=&device_id=&equipment_type=&level=` | abnormal records, most severe first (default: the latest day) |
| GET | `/analysis/records/{id}?include_values=true` | record with events, statistics, rules, comparisons, context, interval samples |
| GET | `/stations` | station configuration and device directory |
| GET | `/stations/{id}` | one station with the latest value of every series |
| GET | `/stations/{id}/telemetry?device_id=&metric=&start=&end=&interval=` | raw points, or avg/min/max/count per `interval` seconds |
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
event: tool      data: {"name": "get_analysis_results", "args": {"station_id": "ST01"}}
event: delta     data: {"text": "昨天 ST01 有"}                   (repeated)
event: citation  data: {"ref": 1, "kind": "data", "title": "...", ...}
event: done      data: {"message": {...stored assistant message...}}
event: error     data: {"code": "agent_failed", "message": "..."}
```

With `"stream": false` you get one JSON reply: `{"session_id": ..., "message": {...}}`.

```bash
curl -N http://127.0.0.1:8000/api/v1/chat -H "Content-Type: application/json" -d '{"message": "昨天哪些设备需要检修？"}'
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
  or a source, queries station data for status or trend questions and the stored analysis
  results for "which devices have problems" questions, and answers greetings and follow-ups
  directly. After `AGENT_MAX_TOOL_ROUNDS` rounds the model is called
  without tools, so every question gets an answer.
- **Tools** (`app/agent/tools.py`, all read-only):

  | Tool | What it does |
  | --- | --- |
  | `search_knowledge` | searches Milvus |
  | `list_stations` | station configuration, thresholds, devices |
  | `get_station_status` | latest values of a station's devices, battery and shelter |
  | `get_telemetry` | aggregated curve for trend questions |
  | `get_analysis_results` | records of the latest (or a given) daily analysis |
  | `get_analysis_record` | one record's events, rules, comparisons, context |

- **Citations.** Tool results are numbered `[1]`, `[2]`, … across one answer. The sources the
  answer refers to are returned as `citation` records and stored with the message, together
  with the knowledge version, model, token counts and the tool calls made.
- **Grounding.** The system prompt requires a source for every fact, an explicit "no basis
  found" when nothing is found, and no device-control instructions. Levels always come
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

## Daily analysis

```text
00:30  scheduler -> batch (date, version) -> per station: IoTDB samples -> rules -> records/events
       -> explanation (LLM for abnormal records, template for missing data) -> openGauss
day    frontend -> GET /analysis/records (reads openGauss only)
```

The rules decide everything:
- **Dose rate** above L1 / L2 for 3 consecutive samples; escalation and recovery open new
  intervals.
- **Device-reported statuses**: H1 / H2 / fault / offline.
- **Data quality**: communication interruption, gaps longer than 3 sampling intervals, no data,
  frozen readings.
- **Battery**: voltage below the warning / alarm threshold, voltage drop while mains is on,
  slow recharge after mains returns.

Stations without thresholds get one configuration gap in the batch and no device records. Each
event carries:
- its statistics, rule and thresholds
- comparisons with the previous day, previous 7 days, same day last month and same day last
  year (or `unavailable` with a reason)
- context (AC power, rainfall, shelter)
- the samples of the interval

The language model receives only the evidence summary, never raw curves. Its output must cite
event IDs and use only numbers found in the evidence. If it fails validation it is retried
once; after that a rule-based template is used. Confidence is computed by code.

```bash
python scripts/analyze.py                  # yesterday (also: --date 2026-11-08, --force, --show, --no-llm)
curl -X POST http://127.0.0.1:8000/api/v1/analysis/batches -H "Content-Type: application/json" -d '{"force": true}'
curl http://127.0.0.1:8000/api/v1/analysis/records
```

[docs/station-analysis.md](docs/station-analysis.md) has the full rule list, the record and
event contract, the frontend field mapping, and the **decisions to confirm**.

---

## Simulated data and the real MQTT feed

```text
SimulatedSource / MqttSource / FileSource → parser → validate → batch → IoTDB
```

**Simulated data now.** `data/stations.json` defines three synthetic stations (30 s, 60 s and
5 min sampling) with simulation-only thresholds. Scripted events on yesterday produce every
kind of record:
- H1 twice, and an H1 → H2 escalation
- a frozen detector, an offline period plus a gap, a device fault
- a device with no data at all, a communication interruption
- battery under-voltage and slow recharge after a mains outage, and a voltage drop with mains on

The third station has no thresholds, so it produces a configuration gap. See the table in
[docs/station-analysis.md](docs/station-analysis.md#8-demo-scenario-datastationsjson-all-synthetic).

```bash
python scripts/simulate.py                      # history + live until Ctrl+C
python scripts/simulate.py --hours 200 --no-live  # 8 days: comparisons become available
python scripts/simulate.py --duration 30        # stop after 30 min
python scripts/simulate.py --target mqtt        # publish to the broker instead
python scripts/simulate.py --purge --yes        # delete ALL series under IOTDB_ROOT
```

For a demo without any database, set `TIMESERIES_BACKEND=memory`, `OPENGAUSS_DSN=` and
`DEMO_SIMULATION=true`, then run `python scripts/run.py`. The history is written first, then
the scheduler catches up on yesterday.

**Real MQTT / MES later.** Only the ingestion adapter changes. Storage, rules, API and agent
stay the same.

1. Capture a few real messages into a JSON Lines file (`{"topic": ..., "payload": ...}` per
   line).
2. Describe the format in a mapping file. Copy `data/mqtt/mapping.example.json`, which covers:
   - a topic regex and field paths
   - lists of readings per message
   - units (converted to µGy/h)
   - a vendor status map (normal / h1 / h2 / fault / offline; stored, never dropped)
   - a vendor-ID → station / device map (`_station` for station-level metrics)
3. Test offline, storing nothing:
   ```bash
   python scripts/ingest_mqtt.py --file captured.jsonl --dry-run --parser mapping --mapping my_mapping.json
   ```
4. Test against the broker, still storing nothing:
   ```bash
   python scripts/ingest_mqtt.py --dry-run --host <broker> --topics "plant/#" --parser mapping --mapping my_mapping.json
   ```
5. Put the real stations in `station_config`: edit `data/stations.json` and run
   `python scripts/init_db.py --update-stations`. Leave thresholds empty until the plant
   provides them.
6. Set `INGEST_PARSER=mapping`, `INGEST_MAPPING_FILE`, `MQTT_*` in `.env`, stop `simulate.py`,
   and run `python scripts/ingest_mqtt.py`.

For formats a mapping cannot describe, or an MES pull interface, write a class with
`parse(RawMessage) -> list[Measurement]` and set `INGEST_PARSER=yourmodule:YourParser`.

Ingestion reconnects automatically. With `MQTT_CLIENT_ID` set, the broker keeps a persistent
session, so messages wait while the ingester restarts. Writes are retried while IoTDB is down,
with a bounded buffer. Rejected messages are counted by reason (`unknown_device`, `bad_value`,
`unknown_status`, …) and printed on exit.

---

## Testing

```bash
pytest                                          # unit tests (no services needed)
IOTDB_TEST_HOST=127.0.0.1 MQTT_TEST_HOST=127.0.0.1 MILVUS_TEST_URI=http://127.0.0.1:19530 OPENGAUSS_TEST_DSN="host=127.0.0.1 port=15432 dbname=industrial_ai_test user=app_ai password=AppAi@Local2026" pytest
LIVE_TESTS=1 pytest tests/test_live.py -s       # real DeepSeek + GLM
```

Use a dedicated openGauss test database. The tests write chat rows, analysis rows dated
2001-01-02 and stations named `TST*`, and remove them afterwards. Create it with
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
| every device `no_data` in the analysis | no samples for yesterday in IoTDB: run `simulate.py` (48 h history), then `analyze.py --force` |
| no records at all; the batch shows `dose_thresholds` gaps | the stations have no `dose_l1` / `dose_l2` configured |
| batch `partial` with `TimeSeriesError` | IoTDB was unreachable during the run; it is retried, or re-run with `force` |
| `/analysis/records` returns `"batch": null` | no finished batch yet: wait for 00:30 or `POST /analysis/batches` |
| chat `503 llm_not_configured` | `LLM_PROVIDER=none` |
| Windows: `ProactorEventLoop` error | start with `python scripts/run.py`, not plain `uvicorn` |

## Before production

- Use the local vLLM model and a private GLM embedding (`EMBEDDING_PROVIDER=local`).
- Change every password from the Docker stack.
- Set `API_KEY` or restrict the network.
- Configure the MQTT broker with authentication and TLS.
- Replace `get_current_user()` with real authentication once the client provides login.
- Provide the real station threshold table and data source, and confirm the decisions listed in
  [docs/station-analysis.md](docs/station-analysis.md#9-decisions-to-confirm-and-refinements-made).
- Run one API instance (the daily scheduler runs in-process), and set the IoTDB retention to at
  least one year for year-on-year comparisons.
