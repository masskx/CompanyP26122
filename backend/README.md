# industrial-ai backend — local setup guide

FastAPI backend for the nuclear-plant MES workstation: equipment health and predictive
maintenance over radiation dose-rate data, and an AI assistant that decides on its own when
to query equipment data (Apache IoTDB) or search the technical knowledge base (Milvus), with
conversations stored in openGauss.

This guide takes you from a fresh download to a fully working system on your own machine,
using **simulated** dose-rate data, and explains how to switch to the **real MQTT feed**
later without touching the API, the IoTDB layout or the agent.

> 中文架构与运维参考：[docs/architecture.zh-CN.md](docs/architecture.zh-CN.md)

---

## Contents

- [How the pieces fit together](#how-the-pieces-fit-together)
- [Quick start (about 15 minutes)](#quick-start-about-15-minutes)
- [1. Databases: configure, initialise, verify](#1-databases-configure-initialise-verify)
- [2. FastAPI backend: install, configure, run, verify](#2-fastapi-backend-install-configure-run-verify)
- [3. Simulated data for local testing](#3-simulated-data-for-local-testing)
- [4. Switching to the real MQTT feed](#4-switching-to-the-real-mqtt-feed)
- [5. Scripts and command-line tools](#5-scripts-and-command-line-tools)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Before production](#before-production)
- [Project layout](#project-layout)

---

## How the pieces fit together

```text
  simulated devices ──┐                         (scripts/simulate_data.py)
  real devices (later)┤
                      ▼
                 MQTT broker ─────► ingestion: parse → validate → batch ─────► Apache IoTDB
                                    (scripts/mqtt_ingest.py)                   root.mes.<site>.<equipment>.dose_rate
                                                                                  │
 MES main backend ──HTTPS──► FastAPI backend (scripts/run_api.py) ◄───────────────┘
  (auth, users)              ├─ equipment health / maintenance analyses (rules)
                             └─ AI assistant (agent) ──► tools: equipment data (IoTDB)
                                    │                          knowledge search (Milvus ◄─ GLM embeddings)
                                    ├─► LLM: DeepSeek API (dev) / local vLLM (prod)
                                    └─► openGauss: users, sessions, messages, citations, feedback

 Offline: technical documents ──► industrial-ai-kb build (GLM embeddings) ──► Milvus
```

| Process | Started with | Needs |
| --- | --- | --- |
| API server | `python scripts/run_api.py` | IoTDB, openGauss; optionally Milvus, LLM, embedding |
| Simulated data | `python scripts/simulate_data.py` | IoTDB (or MQTT broker with `--target mqtt`) |
| MQTT ingestion | `python scripts/mqtt_ingest.py` | MQTT broker, IoTDB |
| Knowledge build | `python scripts/init_databases.py --milvus --sample-knowledge` or `industrial-ai-kb build` | Milvus, embedding API |

The processes are independent and only meet in the databases, so each can be started,
stopped or replaced on its own.

---

## Quick start (about 15 minutes)

**Prerequisites**
- Python **3.11 or newer** (tested with 3.13), `git`
- Docker with Compose v2 (for the bundled database stack), about 6 GB free RAM.
  Already running your own IoTDB / Milvus / openGauss / MQTT? Skip step 3 and see
  [section 1](#1-databases-configure-initialise-verify).
- Linux, macOS or Windows. Windows commands are shown where they differ.

All commands run from the **`backend/`** directory. Commands that set variables inline
(`NAME=value command`) are for bash/zsh; in PowerShell set them first with
`$env:NAME="value"`.

### Step 1 — Get the code

```bash
git clone -b claude/youthful-lamport-39blpl https://github.com/masskx/CompanyP26122.git
cd CompanyP26122/backend
```

(The backend lives on the `claude/youthful-lamport-39blpl` branch until it is merged; after
that, clone without `-b`. A downloaded ZIP of that branch works the same way.)

### Step 2 — Create a virtual environment and install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev,mqtt]"
```

### Step 3 — Start the databases (Docker)

```bash
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml ps
```

Wait until `iotdb` and `milvus` show `(healthy)` (about one minute). This starts:

| Service | Host port | Login (local only) |
| --- | --- | --- |
| Apache IoTDB 2.0.11 | 6667 | `root` / `root` |
| Milvus 2.6.26 (standalone) | 19530 | none |
| openGauss 7.0.0-RC3 | **15432** | admin `gaussdb` / `Gauss@Local2026` |
| Mosquitto 2.0 (MQTT) | 1883 | anonymous |

### Step 4 — Create your configuration

```bash
cp .env.example .env               # Windows: Copy-Item .env.example .env
```

The defaults already point at the Docker stack. Change `COMPUTE_SERVICE_TOKEN` to any
long random string.

### Step 5 — Initialise the databases

```bash
python scripts/init_databases.py --all --opengauss-admin-dsn "host=127.0.0.1 port=15432 dbname=postgres user=gaussdb password=Gauss@Local2026"
```

Expected output:

```text
== IoTDB  127.0.0.1:6667  root=root.mes
  [OK]   database root.mes; time series created=7 existing=0
== openGauss
  [OK]   bootstrap: created user app_ai, created database industrial_ai, created schema app_ai
  [OK]   migrations applied: 0001_conversations
== Milvus  http://127.0.0.1:19530  alias=nuclear_kb
  [OK]   connected; versions=[] active=None
Done.
```

The admin DSN is only needed the first time; later runs are just
`python scripts/init_databases.py --all`.

### Step 6 — Check every connection

```bash
python scripts/check_connections.py --mqtt
```

### Step 7 — Generate simulated data (terminal 1)

```bash
python scripts/simulate_data.py
```

It writes 48 hours of history into IoTDB (a few seconds), then keeps adding live readings
every 30–60 s until you press **Ctrl+C**.

### Step 8 — Start the API (terminal 2)

```bash
python scripts/run_api.py
```

Open <http://127.0.0.1:8000/docs> for the interactive API documentation.

### Step 9 — Run the end-to-end test (terminal 3)

```bash
python scripts/smoke_test.py
```

Without AI keys the assistant steps are skipped and you should see:

```text
== 3. Equipment health (IoTDB data + rules)
           counts: {'normal': 4, 'watch': 1, 'inspect': 1, 'insufficient_data': 1}
  [OK]   7 devices have data
== 4. Maintenance analysis (SSE)
  [OK]   status=completed health_level=inspect
SMOKE TEST PASSED
```

### Step 10 — Turn on the AI assistant (DeepSeek + GLM)

1. Provide the keys as environment variables (`DEEPSEEK_API` or `DEEPSEEK_API_KEY`, and
   `GLM_API_KEY`), or put them in `.env` as `LLM_API_KEY=` / `EMBEDDING_API_KEY=`.
   For example `export GLM_API_KEY=...` (Linux/macOS) or `$env:GLM_API_KEY="..."` (PowerShell).
2. In `.env` set:
   ```ini
   LLM_PROVIDER=deepseek
   EMBEDDING_PROVIDER=zhipu
   ```
3. Build the sample knowledge base (sends the four sample documents to the GLM API):
   ```bash
   python scripts/init_databases.py --milvus --sample-knowledge
   ```
4. Restart the API (Ctrl+C in terminal 2, then `python scripts/run_api.py`) and run
   `python scripts/smoke_test.py` again. Steps 5 and 6 now check that the agent queried
   IoTDB for a device question and searched Milvus (with a citation) for a knowledge
   question.

> DeepSeek and the Zhipu GLM API are public internet services. Use only simulated data
> and approved documents with them. They are refused when `APP_ENV=production`.

---

## 1. Databases: configure, initialise, verify

All connection settings live in **`.env`** (created from `.env.example`; every variable is
documented there). Nothing else needs editing. Environment variables set in your shell
override `.env`.

### 1.1 Apache IoTDB (time series)

| Variable | Default | Meaning |
| --- | --- | --- |
| `TIMESERIES_BACKEND` | `iotdb` | `memory` runs without IoTDB (data lost on restart) |
| `IOTDB_HOST`, `IOTDB_PORT` | `127.0.0.1`, `6667` | RPC endpoint |
| `IOTDB_USER`, `IOTDB_PASSWORD` | `root`, `root` | login |
| `IOTDB_ROOT_PATH` | `root.mes` | database; series are `<root>.<site_id>.<equipment_id>.<metric>` |

**Storage layout** (tree model, identical for simulated and real data):

```text
root.mes.AS01.`AS01-ISO-RM-01`.dose_rate     DOUBLE, µSv/h, millisecond timestamps
```

**Initialise:** `python scripts/init_databases.py --iotdb` creates the database and one
`DOUBLE` series (GORILLA encoding, LZ4) for every metric of every device in
`EQUIPMENT_CATALOG_PATH`. IoTDB also creates series automatically on first write, so this
step matters mainly when automatic schema creation is disabled on your server.

**Verify:**

```bash
python scripts/check_connections.py --no-paid
docker exec industrial-ai-iotdb /iotdb/sbin/start-cli.sh -h 127.0.0.1 -p 6667 -u root -pw root -e "select last dose_rate from root.mes.AS01.\`AS01-ISO-RM-02\`"
```

### 1.2 openGauss (conversations)

| Variable | Default | Meaning |
| --- | --- | --- |
| `SESSION_STORE_BACKEND` | `opengauss` | `memory` keeps conversations in RAM only |
| `OPENGAUSS_DSN` | `host=127.0.0.1 port=15432 dbname=industrial_ai user=app_ai password=AppAi@Local2026` | application login (libpq format, keep the quotes in `.env`) |
| `OPENGAUSS_POOL_MIN` / `_MAX` | `1` / `10` | connection pool |

**Initialise (automatic):** with an admin login, the script creates the application user,
the database and its schema, then applies the migrations. It is safe to run repeatedly.

```bash
python scripts/init_databases.py --opengauss --opengauss-admin-dsn "host=<host> port=<port> dbname=postgres user=<admin> password=<admin-password>"
```

**Initialise (manual, e.g. when a DBA prepares the database):**

```sql
-- as an administrator, in the postgres database (run each statement separately)
CREATE USER app_ai PASSWORD 'AppAi@Local2026';
CREATE DATABASE industrial_ai DBCOMPATIBILITY 'PG' OWNER app_ai;
-- then in the industrial_ai database
CREATE SCHEMA app_ai AUTHORIZATION app_ai;
```

followed by `python scripts/init_databases.py --opengauss` (or `industrial-ai-db migrate`).
Tables: `app_users`, `chat_sessions`, `chat_messages`, `message_citations`,
`message_feedback`, `schema_migrations`
(`src/industrial_ai/storage/migrations/0001_conversations.sql`).

openGauss specifics (all handled by the bootstrap; important if you prepare it by hand):
- **`DBCOMPATIBILITY 'PG'` is required.** The default `A` mode stores empty strings as NULL;
  the migration refuses such a database.
- The **initial user (`omm`) cannot log in remotely**. Use a normal user with **md5**
  authentication in `pg_hba.conf` (standard PostgreSQL drivers do not support openGauss's
  sha256 method). The Docker image's `gaussdb` user works as the admin.
- Ordinary users cannot create tables in `public`, hence the schema named after the user.

**Verify:**

```bash
python scripts/check_connections.py --no-paid
docker exec -u omm industrial-ai-opengauss bash -lc "gsql -d industrial_ai -p 5432 -c 'SELECT title, message_count FROM app_ai.chat_sessions'"
```

### 1.3 Milvus (knowledge base)

| Variable | Default | Meaning |
| --- | --- | --- |
| `MILVUS_URI` | `http://127.0.0.1:19530` | empty disables knowledge search |
| `MILVUS_TOKEN`, `MILVUS_DB_NAME` | unset, `default` | if authentication / databases are used |
| `KB_ALIAS` | `nuclear_kb` | alias pointing at the active knowledge version |
| `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | `none`, `embedding-3`, `1024` | GLM embedding used for **both** building and querying |

There is nothing to create up front: each knowledge **version** becomes its own collection
(`nuclear_kb__<version>`) when you build it, and the alias is switched to it atomically.

```bash
python scripts/init_databases.py --milvus --sample-knowledge   # example corpus
industrial-ai-kb checksum path/to/manifest.json --write          # prepare your own corpus
industrial-ai-kb build path/to/manifest.json                     # build + activate a version
industrial-ai-kb list                                            # versions, active flag
industrial-ai-kb activate <version>                              # switch / roll back
industrial-ai-kb search "剂量率单位换算" --top-k 5                # debug retrieval
```

A manifest lists every document with its SHA-256, classification and access scope (see
`examples/knowledge/manifest.json`). Supported formats: Markdown, text, and PDF after
`pip install -e ".[pdf]"`.

**Changing the embedding model or dimensions requires rebuilding the knowledge base.** Each
collection records the model it was built with, and the API refuses to search a collection
built with a different one (`/health/ready` then shows `knowledge_base.status = incompatible`).

**Verify:** `python scripts/check_connections.py` shows the active version and whether its
embedding matches your configuration.

### 1.4 MQTT broker

`MQTT_HOST`, `MQTT_PORT`, `MQTT_TOPICS` (JSON list), `MQTT_USERNAME`, `MQTT_PASSWORD`,
`MQTT_TLS`, `MQTT_CA_FILE`, `MQTT_QOS`, `MQTT_CLIENT_ID`, `MQTT_PERSISTENT_SESSION`.
Check with `python scripts/check_connections.py --mqtt`. The bundled Mosquitto allows
anonymous access and is for local development only.

### 1.5 Using your own servers instead of Docker

Edit the host, port and credentials in sections 2–5 of `.env`, run
`python scripts/init_databases.py --all` (plus `--opengauss-admin-dsn` for a new
openGauss), then `python scripts/check_connections.py`. Nothing in the code refers to the
Docker stack.

---

## 2. FastAPI backend: install, configure, run, verify

### 2.1 Install

```bash
pip install -e ".[dev,mqtt]"     # development: tests, linting, MQTT
pip install -e ".[mqtt]"         # runtime only, with MQTT ingestion
pip install -e ".[pdf]"          # PDF documents in the knowledge base
```

Core dependencies: FastAPI, uvicorn, pydantic, httpx, apache-iotdb, pymilvus 2.6,
psycopg 3, PyJWT.

### 2.2 Configure

Everything is in `.env`; see `.env.example` for the full list. The essentials:

| Area | Variables |
| --- | --- |
| API access | `COMPUTE_SERVICE_TOKEN` (required; requests without it get 401) |
| Users | `USER_IDENTITY_MODE` = `trusted_gateway` (default) / `jwt` / `dev` |
| LLM | `LLM_PROVIDER` = `none` / `deepseek` / `vllm`, `LLM_API_KEY` or `DEEPSEEK_API`, `LLM_BASE_URL`, `LLM_MODEL` |
| Embedding | `EMBEDDING_PROVIDER` = `none` / `zhipu` / `local`, `EMBEDDING_API_KEY` or `GLM_API_KEY` |
| Databases | sections 2–4 of `.env` |

Switching from DeepSeek to your local model later is configuration only:

```ini
LLM_PROVIDER=vllm
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=<name given to vllm serve --served-model-name>
LLM_MODEL_PRECISION=W8A8
```

(vLLM must run with tool calling enabled; for Qwen3:
`--enable-auto-tool-choice --tool-call-parser hermes`.)

### 2.3 Start

```bash
python scripts/run_api.py                       # http://127.0.0.1:8000
python scripts/run_api.py --reload              # restart on code changes
python scripts/run_api.py --host 0.0.0.0        # reachable from other machines
```

`run_api.py` is equivalent to
`uvicorn --factory industrial_ai.api.main:app_factory`, plus the event-loop setting
openGauss needs on Windows. Stop it with Ctrl+C.

### 2.4 Verify

- `python scripts/smoke_test.py`: health → identity → IoTDB data → analysis → assistant →
  knowledge search → history.
- <http://127.0.0.1:8000/docs>: interactive OpenAPI documentation (disabled in production).
- `curl http://127.0.0.1:8000/health/ready` lists every component's status.

Every `/internal/v1` request needs the service token. Assistant endpoints also need the end
user, which in the default mode the MES backend sends as headers:

```bash
TOKEN=local-dev-token-change-me
curl -s "http://127.0.0.1:8000/internal/v1/equipment/health?site_id=AS01" -H "Authorization: Bearer $TOKEN"
curl -N http://127.0.0.1:8000/internal/v1/assistant/chat -H "Authorization: Bearer $TOKEN" -H "X-User-Id: alice" -H "Content-Type: application/json" -d '{"message": "AS01-ISO-RM-02 最近6小时剂量率情况如何？"}'
curl -s http://127.0.0.1:8000/internal/v1/assistant/sessions -H "Authorization: Bearer $TOKEN" -H "X-User-Id: alice"
```

### 2.5 API overview

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health/live`, `/health/ready`, `/health/model` | liveness, component status, model info |
| GET | `/internal/v1/equipment/health?site_id=&category=` | health classification of every device |
| POST | `/internal/v1/maintenance/analyses` | start an analysis (`202`, `Idempotency-Key` supported) |
| GET | `/internal/v1/maintenance/analyses/{id}/events` | SSE progress and result (`Last-Event-ID` to resume) |
| GET | `/internal/v1/maintenance/analyses/{id}` | stored result |
| POST | `/internal/v1/assistant/chat` | ask (SSE: `stage`, `delta`, `citation`, `result`, `error`, `done`) |
| POST / GET | `/internal/v1/assistant/sessions` | create / list the user's sessions |
| GET / PATCH / DELETE | `/internal/v1/assistant/sessions/{id}` | view / rename or archive / delete |
| GET | `/internal/v1/assistant/sessions/{id}/messages` | history with citations |
| POST | `/internal/v1/assistant/messages/{id}/feedback` | `{"rating": 1}` or `-1` |
| GET | `/internal/v1/users/me` | the user as resolved from the request |

User identity: the service token is checked first; only then is the user taken from the
trusted headers (`trusted_gateway`) or from a signed JWT (`jwt`, recommended once the client
issues tokens). Sessions are always scoped to that user; another user's session returns
404. See `.env.example` section 9.

---

## 3. Simulated data for local testing

### 3.1 What is simulated

Seven synthetic monitoring points at site `AS01`, defined in
`examples/simulation/radiation_scenario.json`. That file is both the simulation config and
the equipment catalog. The model includes a stable background level, counting noise, slow
drift and daily variation, plus random and scripted events: spikes, sustained rises, rain
washout, communication outages and frozen sensors. Scripted events make the last 24 hours
cover every health level:

| Device | Expected health | Why |
| --- | --- | --- |
| AS01-ISO-RM-01, RM-05, ENV-01, ENV-02 | normal | background only (RM-05 has a frozen-sensor episode the rules do not flag yet) |
| AS01-ISO-RM-02 | inspect | 40-minute rise above the alarm level |
| AS01-ISO-RM-04 | watch | 30-minute rise above the warning level |
| AS01-ISO-RM-03 | insufficient_data | 5.5-hour outage, 77% completeness |

> All baselines, events and thresholds in the scenario are invented for testing. They are not
> plant data, regulatory limits or alarm set-points.

### 3.2 Yes: a Python script inserts it into IoTDB

`scripts/simulate_data.py` produces exactly the messages the real MQTT feed will deliver,
sends them through the same parser → validator → batch → IoTDB writer, and stores them in
the same series. Three ways to run it:

| Mode | Command | Use when |
| --- | --- | --- |
| Straight into IoTDB | `python scripts/simulate_data.py` | everyday development |
| Through the MQTT broker | `python scripts/mqtt_ingest.py` (terminal A) + `python scripts/simulate_data.py --target mqtt` (terminal B) | testing the full MQTT path as it will run in production |
| No databases at all | `TIMESERIES_BACKEND=memory SESSION_STORE_BACKEND=memory MILVUS_URI= API_EMBEDDED_COLLECTOR=true python scripts/run_api.py` | quick demos: data and conversations live inside the API process and are lost on exit |

### 3.3 Start, stop and control

```bash
python scripts/simulate_data.py                                # 48 h history, then live until Ctrl+C
python scripts/simulate_data.py --backfill-hours 24 --no-realtime   # history only, then exit
python scripts/simulate_data.py --duration 30                  # stop after 30 minutes
python scripts/simulate_data.py --seed 7                       # different (but reproducible) data
python scripts/simulate_data.py --target stdout --backfill-hours 0.1 --no-realtime   # just print
python scripts/simulate_data.py --scenario my_scenario.json    # your own devices/events
python scripts/simulate_data.py --purge --yes                  # delete the simulated series
```

- **Stop** with Ctrl+C (or SIGTERM). Buffered points are flushed first and the final count is
  printed, e.g. `written=33910 rejected={} write_failures=0`.
- Re-running with the same seed rewrites the same timestamps, so restarts do not duplicate
  data.
- Keep at least 24 hours of history. With less, most devices show `insufficient_data`,
  because the default health window is 24 h and needs 80% completeness.
- To change devices, baselines or events, copy the scenario file, edit it, and point both
  `SIMULATOR_SCENARIO_PATH` and `EQUIPMENT_CATALOG_PATH` at the copy.

### 3.4 Verify the data and the agent

1. **Data is in IoTDB:** `python scripts/check_connections.py --no-paid` shows point counts and
   the latest timestamp per device.
2. **Rules see it:** the equipment health counts match the table above
   (`python scripts/smoke_test.py`, step 3).
3. **The agent analyses it** (needs an LLM, Quick start step 10): `smoke_test.py` step 5 asks
   about AS01-ISO-RM-02 and checks the agent called a data tool. Try your own questions in
   `/docs` → `POST /internal/v1/assistant/chat`, for example:
   - `AS01 站哪些监测点需要关注？`
   - `AS01-ISO-RM-03 为什么数据不足？`
   - `剂量率 mSv/h 和 µSv/h 怎么换算？` (knowledge base)

   The `stage` events show which tool the agent chose, and the `result` lists `tools_used`
   and `citations`.

---

## 4. Switching to the real MQTT feed

### 4.1 What changes and what doesn't

```text
MessageSource ──(topic, payload)──► MessageParser ──Measurement──► Validator ──► batch ──► IoTDBWriter
simulator | mqtt | file replay      provisional_json | mapping | your class
```

Only the **source** and the **parser** know where messages come from and what they look
like. Every parser produces the same `Measurement(site_id, equipment_id, metric, timestamp,
value in µSv/h)`, so the IoTDB layout, the API endpoints, the rules and the AI agent are
untouched by the switch.

### 4.2 Step by step

1. **Capture a few real messages** into a JSON Lines file, one
   `{"topic": "...", "payload": {...}}` per line (see `examples/mqtt/captured.example.jsonl`).
2. **Describe the format** in a mapping file (no code needed in most cases). Start from
   `examples/mqtt/mapping.example.json`, which handles an invented vendor format with a
   readings list, nSv/h units, ISO timestamps, a status field and vendor device IDs.
3. **Test offline**; nothing is written:
   ```bash
   python scripts/mqtt_ingest.py --from-file captured.jsonl --dry-run --parser mapping --parser-config my_mapping.json
   ```
   It prints every measurement that would be stored, then counts per rejection reason:
   ```text
   {"site_id": "AS01", "equipment_id": "AS01-ISO-RM-01", "metric": "dose_rate", "timestamp": "2026-11-05T10:00:00+08:00", "value": 0.512}
   stats: {"received": 5, "written": 3, "rejected": {"device_status": 1, "unmapped_device": 1, "bad_topic": 1}, ...}
   ```
4. **Test against the live broker**, still without writing:
   ```bash
   python scripts/mqtt_ingest.py --dry-run --host <broker> --topics "plant/#" --parser mapping --parser-config my_mapping.json
   ```
5. **Register the real equipment.** Create a catalog file shaped like
   `examples/equipment_catalog.example.json` with the real site/equipment IDs, and set
   `EQUIPMENT_CATALOG_PATH` to it. Add thresholds when the plant provides them; until then
   those metrics report `insufficient_data` rather than guessing.
6. **Configure `.env`:**
   ```ini
   COLLECTOR_SOURCE=mqtt
   COLLECTOR_PARSER=mapping
   COLLECTOR_PARSER_CONFIG=config/my_mapping.json
   MQTT_HOST=<broker>
   MQTT_TOPICS=["plant/#"]
   MQTT_USERNAME=...
   MQTT_PASSWORD=...
   MQTT_TLS=true
   MQTT_CLIENT_ID=industrial-ai-ingest
   MQTT_PERSISTENT_SESSION=true
   ```
7. **Run ingestion** and stop running `simulate_data.py`:
   ```bash
   python scripts/mqtt_ingest.py
   ```
   Optionally remove the simulated series with `python scripts/simulate_data.py --purge --yes`.

### 4.3 Mapping file reference

| Key | Meaning |
| --- | --- |
| `topic_pattern` | regex for accepted topics; named groups `site_id`, `device`, `metric` can supply values |
| `records_path` | dotted path to a list of readings inside one message (`null` = the payload is one reading) |
| `fields.device` / `site_id` / `timestamp` / `value` / `unit` / `status` / `metric` | dotted paths inside a reading (`data.doseRate`, `readings.0.value`) |
| `timestamp_format` | `auto`, `epoch_ms`, `epoch_s`, `iso8601` |
| `default_unit` | unit when the message has none (`nSv/h`, `uSv/h`, `µSv/h`, `mSv/h`, `Sv/h`) |
| `default_metric` | metric name when none is given (`dose_rate`) |
| `ok_status` | status values that count as valid; other values are rejected as `device_status` |
| `device_map` | vendor device ID → `{site_id, equipment_id[, metric]}` |
| `unmapped_devices` | `reject` (default) or `passthrough` (use site from topic, equipment = device ID) |

### 4.4 Handling differences between simulated and real data

| Difference | Where it is handled |
| --- | --- |
| Topic structure | `topic_pattern` |
| Field names / nesting | `fields.*` dotted paths |
| Several readings per message | `records_path`; one bad reading does not discard the others |
| Units (nSv/h, mSv/h, …) | converted to µSv/h by the parser |
| Timestamp formats / time zones | `timestamp_format`; stored as UTC milliseconds |
| Vendor device IDs | `device_map` |
| Fault / maintenance status | `ok_status` |
| Unknown devices | rejected and counted (`unmapped_device`, `unknown_equipment`), never silently stored |
| Bad values, future timestamps | validator (`bad_value`, `future_timestamp`) |
| Additional metrics (e.g. temperature) | add them to the equipment catalog; `metric` from topic or field |

For formats a mapping cannot express (binary payloads, compressed batches, …) write a parser
class and point `COLLECTOR_PARSER` at it; no change inside this package is needed:

```python
# mycompany/plant_parser.py
from industrial_ai.collector.models import Measurement, RawMessage, RejectedMessage

class PlantParser:
    def __init__(self, settings):
        ...

    def parse(self, message: RawMessage) -> list[Measurement]:
        # decode message.payload; raise RejectedMessage("bad_payload", "...") to reject
        return [Measurement(site_id=..., equipment_id=..., metric="dose_rate",
                            timestamp=..., value=...)]  # value in µSv/h
```

```ini
COLLECTOR_PARSER=mycompany.plant_parser:PlantParser
```

### 4.5 Reliability of the ingestion process

- QoS 1 with `MQTT_PERSISTENT_SESSION=true` and a fixed `MQTT_CLIENT_ID`: the broker keeps
  messages while the ingester restarts.
- Lost broker connections reconnect automatically with exponential backoff.
- If IoTDB is down, batches are retried with backoff and kept in memory up to
  `COLLECTOR_MAX_BUFFER` points (oldest dropped beyond that, and counted).
- Statistics are logged every minute and printed on exit (`received`, `written`, `rejected`
  by reason, `write_failures`, `dropped`).
- To run it as a service, use your process manager (systemd, Windows service, container)
  with `python scripts/mqtt_ingest.py` from `backend/`. SIGTERM stops it cleanly.

---

## 5. Scripts and command-line tools

| Script (`backend/scripts/`) | Purpose |
| --- | --- |
| `init_databases.py` | IoTDB schema, openGauss bootstrap + migrations, Milvus check / sample knowledge |
| `check_connections.py` | verify IoTDB, openGauss, Milvus, embedding, LLM, MQTT |
| `simulate_data.py` | simulated dose-rate data → IoTDB / MQTT / stdout; purge |
| `mqtt_ingest.py` | MQTT → IoTDB ingestion; `--dry-run`, `--from-file` |
| `run_api.py` | start the FastAPI server (cross-platform) |
| `smoke_test.py` | end-to-end check of a running server |

All scripts accept `--env-file` (default `.env`) and `--help`. Installed command-line tools:

| Command | Purpose |
| --- | --- |
| `industrial-ai-kb` | knowledge base: `checksum`, `build`, `list`, `activate`, `drop`, `search` |
| `industrial-ai-db migrate` | apply openGauss migrations |
| `industrial-ai-collector run` / `publish-sim` | collector using `COLLECTOR_SOURCE` from `.env` |

---

## Testing

```bash
pytest                    # unit tests; Milvus tests run on embedded Milvus Lite (not on Windows)
ruff check .
```

Integration tests against real services run when these are set (use a **dedicated**
openGauss test database; its tables are emptied):

```bash
IOTDB_TEST_HOST=127.0.0.1 MQTT_TEST_HOST=127.0.0.1 MILVUS_TEST_URI=http://127.0.0.1:19530 OPENGAUSS_TEST_DSN="host=127.0.0.1 port=15432 dbname=industrial_ai_test user=app_ai password=AppAi@Local2026" pytest
```

Live tests against DeepSeek and GLM (cost a little; send only sample documents):

```bash
LIVE_PROVIDER_TESTS=1 pytest tests/test_live_providers.py -s
```

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `port is already allocated` on `docker compose up` | another service uses 6667 / 19530 / 15432 / 1883; stop it or change the host port in `deploy/docker-compose.yml` and `.env` |
| `invalid configuration: ... LLM_API_KEY (or DEEPSEEK_API_KEY / DEEPSEEK_API) is required` | provider enabled without a key; set the key or the provider back to `none` |
| openGauss `permission denied for schema public` | the schema named after the app user is missing; re-run init with `--opengauss-admin-dsn` |
| openGauss `Forbid remote connection with initial user` | do not use `omm`; use `gaussdb` (Docker) or another admin |
| `database compatibility is 'A'` | recreate the database with `DBCOMPATIBILITY 'PG'` |
| IoTDB `connection refused` | container still starting (wait for `healthy`) or wrong `IOTDB_HOST`/`PORT` |
| Most devices `insufficient_data` | less than ~20 h of data in the last 24 h: run `simulate_data.py` with the default 48 h history |
| `/health/ready` → `knowledge_base: incompatible` | the index was built with another embedding model/dimensions: rebuild with the current settings |
| chat returns `503 llm_unavailable` | `LLM_PROVIDER=none`, or the model service is unreachable (`check_connections.py`) |
| `401 unauthorized` / `user_identity_required` | missing `Authorization: Bearer <COMPUTE_SERVICE_TOKEN>` / missing `X-User-Id` |
| Windows: `Psycopg cannot use the 'ProactorEventLoop'` | start the API with `python scripts/run_api.py`, not plain `uvicorn` |
| Windows: `milvus-lite` not found | expected; Milvus Lite has no Windows build, so those tests are skipped |
| `aiomqtt is not installed` | `pip install -e ".[mqtt]"` |

---

## Before production

- Public APIs (DeepSeek, Zhipu GLM) and `USER_IDENTITY_MODE=dev` are **refused when
  `APP_ENV=production`**. Use the local vLLM model and a privately deployed GLM embedding
  (`EMBEDDING_PROVIDER=local`).
- Replace every password and token from the local stack; configure Mosquitto with
  authentication and TLS; restrict the API to the MES backend's address.
- Prefer `USER_IDENTITY_MODE=jwt` once the client application issues signed user tokens.
- Maintenance-analysis results are still kept in memory (conversations are persisted).

---

## Project layout

```text
backend/
├── .env.example            all settings, documented (copy to .env)
├── pyproject.toml          dependencies and command-line tools
├── deploy/                 docker-compose.yml for IoTDB, Milvus, openGauss, Mosquitto
├── scripts/                init, checks, simulation, MQTT ingestion, API start, smoke test
├── examples/
│   ├── simulation/         simulation scenario (= equipment catalog of the simulated devices)
│   ├── knowledge/          sample knowledge corpus + manifest
│   ├── mqtt/               example mapping file + captured messages for the real-feed workflow
│   └── equipment_catalog.example.json
├── src/industrial_ai/
│   ├── api/                FastAPI app, dependency wiring, routers
│   ├── agent/              agent loop and read-only tools
│   ├── analytics/          statistics and deterministic health rules
│   ├── collector/          sources (simulator, MQTT, file), parsers, validator, writers
│   ├── core/               settings, logging, errors, service token, user identity
│   ├── integrations/       LLM client (DeepSeek / vLLM), GLM embedding client
│   ├── knowledge/          manifest, loaders, chunking, Milvus store, builder, retriever
│   ├── services/           equipment health, maintenance, assistant
│   ├── storage/            IoTDB, openGauss (+ migrations), catalog, in-memory stores
│   └── workers/            background analysis runner
├── tests/
└── docs/architecture.zh-CN.md
```
