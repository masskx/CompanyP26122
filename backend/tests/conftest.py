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

from app.config import BASE_DIR, Settings
from app.db.chat_store import MemoryChatStore
from app.deps import build_resources
from app.ingestion.parsers import SimulatorFormat
from app.ingestion.pipeline import Pipeline
from app.ingestion.simulator import SimulatedSource
from app.main import create_app
from app.timeseries.devices import DeviceCatalog
from app.timeseries.iotdb import MemoryTimeSeries

DEVICES = BASE_DIR / "data" / "devices.json"
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
def settings() -> Settings:
    return Settings(_env_file=None, timeseries_backend="memory", devices_file=DEVICES)


@pytest.fixture(scope="session")
def catalog() -> DeviceCatalog:
    return DeviceCatalog.load(DEVICES)


@pytest.fixture
async def store(catalog) -> MemoryTimeSeries:
    """48 h of simulated data for the 7 sample devices."""
    ts = MemoryTimeSeries()
    now = datetime.now(UTC)
    source = SimulatedSource(catalog.all(), backfill_hours=48, realtime=False, seed=42,
                             clock=lambda: now)
    await Pipeline(source, SimulatorFormat(), ts, batch_size=5000).run()
    return ts


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
def make_client(settings, catalog, store):
    created = []

    async def make(responses: list | None = None, retriever=None, chat_store=None):
        llm = FakeChatModel(responses=list(responses), calls=[]) if responses is not None \
            else None
        resources = build_resources(settings, timeseries=store, catalog=catalog, llm=llm,
                                    retriever=retriever, chat_store=chat_store or
                                    MemoryChatStore())
        app = create_app(settings, resources)
        app.state.resources = resources
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://t/api/v1")
        created.append(client)
        return client, llm

    yield make


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events
