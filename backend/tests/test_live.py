"""Real DeepSeek + Zhipu GLM (costs a little; sends only sample documents and synthetic data).

    LIVE_TESTS=1 pytest tests/test_live.py -s
"""

import os

import pytest

from app.config import Settings
from app.db.chat_store import MemoryChatStore
from app.deps import build_resources
from app.rag.embeddings import GLMEmbeddings
from app.rag.ingest import build
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeStore
from app.stations.store import MemoryStationStore

from .conftest import KNOWLEDGE, STATIONS

pytestmark = [pytest.mark.anyio, pytest.mark.skipif(os.environ.get("LIVE_TESTS") != "1",
                                                    reason="set LIVE_TESTS=1")]


@pytest.fixture
def live_settings():
    if not (os.environ.get("DEEPSEEK_API") or os.environ.get("DEEPSEEK_API_KEY")) or \
            not os.environ.get("GLM_API_KEY"):
        pytest.skip("DEEPSEEK_API and GLM_API_KEY required")
    return Settings(_env_file=None, llm_provider="deepseek", embedding_provider="zhipu",
                    timeseries_backend="memory", stations_file=STATIONS,
                    analysis_schedule_enabled=False)


def test_glm_embedding(live_settings):
    emb = GLMEmbeddings.from_settings(live_settings)
    a, b, c = emb.embed_documents(["1 mGy/h 等于 1000 µGy/h", "毫希每小时与微希每小时的换算",
                                   "今天午饭吃什么"])
    cos = lambda x, y: sum(i * j for i, j in zip(x, y, strict=True))  # noqa: E731 (unit vectors)
    assert len(a) == 1024 and cos(a, b) > cos(a, c)


async def test_agent_end_to_end(live_settings, store, stations, tmp_path):
    pytest.importorskip("milvus_lite")
    kb = KnowledgeStore(str(tmp_path / "live.db"), "live_kb", index_type="FLAT")
    emb = GLMEmbeddings.from_settings(live_settings)
    build(KNOWLEDGE, kb, emb)
    r = build_resources(live_settings, timeseries=store,
                        stations=MemoryStationStore(stations), chat_store=MemoryChatStore(),
                        retriever=Retriever(kb, emb, top_k=4, min_score=0.3))
    for question, tool, kind in [
        ("85 nGy/h 换算成 µGy/h 是多少？请给出依据。", "search_knowledge", "knowledge"),
        ("ST01 站 D02 探测器最近6小时剂量率趋势如何？", "get_telemetry", "data"),
    ]:
        out = await r.chat.ask("live-test", question, None)
        m = out["message"]
        print(f"\nQ: {question}\nA: {m['content'][:300]}\ntools={m['tools_used']} "
              f"citations={[(c['ref'], c['kind'], c['title']) for c in m['citations']]}")
        assert m["status"] == "complete" and tool in m["tools_used"] or \
            any(t.startswith("get_") for t in m["tools_used"])
        assert any(c["kind"] == kind for c in m["citations"])
    kb.close()
