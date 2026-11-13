# Assistant (智能小助手)

## Preset workflows (LangGraph)

The prototype's assistant opens with four modules (`docs/原型/打开工作站.html`). Each one is a
LangGraph workflow (`app/agent/graph.py`, `app/agent/workflows.py`). Free questions go to the
general agent.

| id | Module | What code does | What the model does |
| --- | --- | --- | --- |
| `monitoring_report` | 生成监测报表 | parameters → hourly / daily statistics → Excel with 报表说明, 监测统计 and 数据质量 sheets → report record | extracts parameters, summarises the report |
| `system_info` | MES 系统信息 | station directory + latest values → online / offline / H1 / H2 / fault / on-battery counts | summarises, highlights stations needing attention |
| `dose_assessment` | 剂量率评估 | station + period → mean / min / max / completeness for this period, the same period last month and last year; missing periods marked "无历史数据" (never 0) | extracts station and period, narrates the comparison |
| `maintenance_advice` | 全站维修建议 | maintenance rules for every station (the same rules as the analysis API) + work-order copy → stations ranked inspect > watch > insufficient_data | narrates priorities with the primary actions |
| `general` | free conversation | read-only tools: stations, telemetry, maintenance check, work orders, knowledge search | ReAct agent with citations |

**Routing**, in this order:
1. an explicit `"workflow"` in the request (the entry card clicked);
2. keywords;
3. a short model classification that sees the recent conversation (`ASSISTANT_INTENT_MODEL`);
4. otherwise `general`.

Each workflow costs at most 2 model calls (parameters + narration). Statistics are never
produced by the model.

Facts are cited `[n]`, as in the general agent.

**Context budget:** the local Qwen3-14B has a 16k context. The history is limited by
`AGENT_HISTORY_MESSAGES`, and the workflow facts are compact JSON.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/v1/chat` (also `/internal/v1/assistant/chat`) | `{"message", "session_id"?, "workflow"?, "stream": true}` |
| GET | `/api/v1/assistant/workflows` | the four modules (id, title, description) |
| GET | `/api/v1/sessions` | the user's conversations, newest first, with `workflow` and `message_count` |
| GET / PATCH / DELETE | `/api/v1/sessions/{id}` | open / rename / delete (soft) |
| GET | `/api/v1/sessions/{id}/messages?limit=&before_seq=` | messages with citations, paged backwards |
| POST | `/api/v1/messages/{id}/feedback` | 👍 / 👎 |
| GET | `/api/v1/reports` | the user's generated reports |
| GET | `/api/v1/reports/{id}/download` | the Excel file (only the owner) |
| POST | `/api/v1/sessions/claim` | `{"client_id"}`: move a browser's conversations to the logged-in user |

**SSE events.** Every payload has a monotonically increasing `seq`, which is also the SSE `id`.
A `: ping` comment is sent every `SSE_HEARTBEAT_SECONDS`.

| Event | Payload |
| --- | --- |
| `session` | `{session_id, user_message_id}` |
| `stage` | `{stage: intent \| data \| generating, label, workflow?}`, e.g. 识别意图 → 工作流：生成监测报表 → 统计数据并生成 Excel → 生成回答 |
| `tool` | `{name, args}` (general agent) |
| `result` | `{type: report, report_id, title, file_name, download_url, row_count, size_bytes}`, `{type: system_info, counts}`, `{type: maintenance_advice, counts}`, `{type: dose_assessment, sites, window}` |
| `delta` | `{text}` |
| `citation` | `{ref, kind, title, source, ...}` |
| `done` | `{message, workflow}` |
| `error` | `{code, message}` |

The current frontend parser ignores unknown events, so `stage` and `result` don't break it. To
use them, the frontend can show `stage.label` as progress and a download button for
`result.type == "report"`.

## Conversation history without roles

**Goal.** Each person sees only their own conversations, before login exists, and nothing is
lost when login arrives.

**Who owns a conversation** (`get_current_user` in `app/deps.py`):

| Header | Owner key | When |
| --- | --- | --- |
| `X-User-Id` | `<id>` | the main server knows the user (after login; the proxy already forwards this header) |
| `X-Client-Id` | `client:<id>` | no login: a random ID the browser creates once and keeps in `localStorage` |
| none | `anonymous` | shared; acceptable only for tests |

The headers identify the user; they don't authenticate them. The service token / API key
authenticates the caller (the main server). Every session, message, feedback and report query
is filtered by the owner key, so another owner gets 404.

**Storage** (openGauss, conversation data only):
- `chat_session (session_id, user_id, title, workflow, message_count, created_at, updated_at,
  deleted_at)`
- `chat_message (... seq, role, content, status, citations, tool_calls, model, kb_version,
  tokens, latency, feedback)`
- `assistant_report (report_id, user_id, session_id, title, params, file_name, size, rows,
  created_at)`

The title is the first question (rename with `PATCH`). `workflow` records which module started
the conversation, and is null for free chat.

**How the frontend should show it** (no roles, so no admin view):
- **History panel** (the prototype's "最近对话"): `GET /sessions?limit=20&offset=..`, grouped
  by `updated_at` into 今天 / 昨天 / 近 7 天 / 更早.
  - Each item shows the title, a small tag for `workflow` (报表 / 系统信息 / 剂量率评估 / 维修建议),
    and the time.
  - Actions: rename and delete.
- **Opening a conversation:** `GET /sessions/{id}/messages?limit=50`, older messages loaded with
  `before_seq`.
  - Assistant messages show their citations.
  - Failed or interrupted answers are marked by `status`.
  - Continue the conversation by sending `session_id` with the next message.
- **Reports:** a "我的报表" list from `GET /reports`, re-downloadable at any time. The report
  card in the conversation comes from the `result` event.
- **New conversation:** omit `session_id` (or `POST /sessions`). Clicking a module card sends
  its `workflow`.

**Changes needed outside this backend:**
- **Frontend:** create and keep `client_id` in `localStorage`, and send it on every call. Today
  the Go proxy forwards only `X-User-Id`, so either send the browser ID as `X-User-Id`, or
  extend `main_server` to forward `X-Client-Id`; the second is cleaner, because then the claim
  can tell browser and user apart.
- **When login arrives:** the main server sets `X-User-Id` from its session. The frontend calls
  `POST /sessions/claim {"client_id": "<the localStorage id>"}` once after the first login,
  which moves that browser's conversations and reports to the user. Later, the user table and
  roles (`X-User-Roles`) can add rules such as an auditor seeing everyone's history, without
  changing the tables.
- **Retention:** conversations are soft-deleted. A retention job (e.g. 180 days for `client:`
  owners) can be added later.
