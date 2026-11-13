import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime

import httpx
import pytest
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.agent.reports import MemoryReportStore
from app.config import BASE_DIR, Settings
from app.db.chat_store import MemoryChatStore
from app.db.work_orders import MemoryWorkOrderStore
from app.deps import build_resources
from app.ingestion.parsers import TelemetryV1Format
from app.ingestion.pipeline import Pipeline
from app.ingestion.simulator import SimulatedSource
from app.main import create_app
from app.stations.model import Station
from app.stations.store import StationStore, load_file
from app.timeseries.iotdb import MemoryTimeSeries

STATIONS = BASE_DIR / "data" / "stations.json"
KNOWLEDGE = BASE_DIR / "data" / "knowledge"

_KEYS = ("DEEPSEEK_API", "DEEPSEEK_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY", "LLM_API_KEY",
         "EMBEDDING_API_KEY", "LLM_PROVIDER", "EMBEDDING_PROVIDER", "OPENGAUSS_DSN",
         "MILVUS_URI", "API_KEY")


@pytest.fixture(autouse=True)
def _isolate_env(request, monkeypatch):
    """Tests never pick up keys or servers configured on the developer's machine."""
    if request.module.__name__.endswith("test_live"):
        return
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, timeseries_backend="memory", stations_file=STATIONS,
                    reports_dir=tmp_path / "reports", analysis_watermark_wait_seconds=0.3,
                    analysis_watermark_poll_ms=50, assistant_intent_model=False)


@pytest.fixture(scope="session")
def stations() -> list[Station]:
    return load_file(STATIONS)


async def asimulate(stations: list[Station], hours: float, now: datetime | None = None
                    ) -> MemoryTimeSeries:
    ts = MemoryTimeSeries()
    now = now or datetime.now(UTC)
    source = SimulatedSource(stations, backfill_hours=hours, realtime=False, seed=42,
                             clock=lambda: now)
    pipe = Pipeline(source, TelemetryV1Format(), ts, batch_size=20_000)
    await pipe.run()
    ts.watermark = pipe.watermark  # type: ignore[attr-defined]
    ts.now = now  # type: ignore[attr-defined]
    return ts


def simulate(stations: list[Station], hours: float, now: datetime | None = None
             ) -> MemoryTimeSeries:
    return asyncio.run(asimulate(stations, hours, now))


@pytest.fixture(scope="session")
def store(stations) -> MemoryTimeSeries:
    """26 h of simulated telemetry_v1 data for the 18 stations (the scenario lies in the last
    24 h). Shared by the whole session: tests must not write to it."""
    return simulate(stations, 26)


class FakeEmbeddings(Embeddings):
    """Character n-gram hashing; texts sharing words get similar vectors."""

    def __init__(self, dimensions: int = 128, model: str = "fake-glm") -> None:
        self.dimensions, self.model = dimensions, model
        self.calls = 0

    @property
    def spec(self) -> dict:
        return {"model": self.model, "dimensions": self.dimensions}

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        out = []
        for t in texts:
            v = [0.0] * self.dimensions
            for g in list(t) + [t[i:i + 2] for i in range(len(t) - 1)]:
                if g.strip():
                    v[int(hashlib.md5(g.encode()).hexdigest(), 16) % self.dimensions] += 1
            n = math.sqrt(sum(x * x for x in v)) or 1
            out.append([x / n for x in v])
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def tool_call(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


class FakeChatModel(BaseChatModel):
    """Scripted chat model usable inside LangGraph (supports bind_tools and streaming)."""

    responses: list = []
    calls: list = []
    tools_bound: bool = False

    @property
    def _llm_type(self) -> str:
        return "fake"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools_bound": True})  # shares responses/calls

    def _next(self, messages) -> AIMessage:
        self.calls.append({"messages": messages, "tools": self.tools_bound})
        msg = self.responses.pop(0) if self.responses else AIMessage(content="(no more)")
        return msg.model_copy(update={"usage_metadata": {"input_tokens": 10,
                                                         "output_tokens": 5,
                                                         "total_tokens": 15}})

    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _stream(self, messages, stop=None, run_manager=None, **kw):
        msg = self._next(messages)
        if msg.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[
                {"name": c["name"], "args": json.dumps(c["args"], ensure_ascii=False),
                 "id": c["id"], "index": i} for i, c in enumerate(msg.tool_calls)],
                usage_metadata=msg.usage_metadata))
            return
        text = msg.content
        step = max(1, len(text) // 3)
        for i in range(0, len(text), step):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=text[i:i + step]))
            if run_manager:
                run_manager.on_llm_new_token(chunk.text, chunk=chunk)
            yield chunk
        yield ChatGenerationChunk(message=AIMessageChunk(content="",
                                                         usage_metadata=msg.usage_metadata))


@pytest.fixture
def make_resources(settings, stations, store):
    def make(responses: list | None = None, retriever=None, chat_store=None, timeseries=None,
             work_orders=None, settings_=None):
        llm = FakeChatModel(responses=list(responses), calls=[]) if responses is not None \
            else None
        return build_resources(settings_ or settings, timeseries=timeseries or store, llm=llm,
                               retriever=retriever, chat_store=chat_store or MemoryChatStore(),
                               stations=StationStore(stations),
                               work_orders=work_orders or MemoryWorkOrderStore(),
                               reports=MemoryReportStore()), llm

    return make


@pytest.fixture
def make_client(settings, make_resources):
    created = []

    async def make(responses: list | None = None, retriever=None, chat_store=None, **kw):
        resources, llm = make_resources(responses, retriever, chat_store, **kw)
        app = create_app(resources.settings, resources)
        app.state.resources = resources
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://t/api/v1")
        client.resources = resources
        created.append(client)
        return client, llm

    yield make


def parse_sse(text: str, keep_seq: bool = False) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            data = json.loads(lines["data"])
            if not keep_seq:
                data.pop("seq", None)
            events.append((lines["event"], data))
    return events
