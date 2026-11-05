"""Live tests against the real DeepSeek and Zhipu GLM APIs (cost money, need internet).

Run explicitly:
    LIVE_PROVIDER_TESTS=1 pytest tests/test_live_providers.py -s

Keys come from DEEPSEEK_API / DEEPSEEK_API_KEY and GLM_API_KEY / ZHIPU_API_KEY. Only the
sample knowledge documents (descriptions of this system) and synthetic questions are sent.
"""

import os
from pathlib import Path

import pytest

from industrial_ai.agent.runner import AgentDelta, AgentFinal, AgentRunner
from industrial_ai.agent.tools import KnowledgeSearchTool, ToolContext
from industrial_ai.core.config import Settings
from industrial_ai.core.identity import Principal
from industrial_ai.integrations.embedding_client import EmbeddingClient, EmbeddingConfig
from industrial_ai.integrations.llm_client import LLMClient, LLMConfig
from industrial_ai.knowledge.builder import build_knowledge_base
from industrial_ai.knowledge.milvus_store import MilvusKnowledgeStore
from industrial_ai.knowledge.retriever import KnowledgeRetriever
from industrial_ai.schemas.analysis import ModelInfo

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get("LIVE_PROVIDER_TESTS") != "1",
                       reason="set LIVE_PROVIDER_TESTS=1 to call the real APIs"),
]

SAMPLE = Path(__file__).parent.parent / "examples" / "knowledge" / "manifest.json"


def _settings() -> Settings:
    return Settings(_env_file=None, llm_provider="deepseek", embedding_provider="zhipu",
                    embedding_model="embedding-3", embedding_dimensions=1024)


@pytest.fixture
def deepseek():
    if not (os.environ.get("DEEPSEEK_API") or os.environ.get("DEEPSEEK_API_KEY")):
        pytest.skip("DEEPSEEK_API not set")
    return LLMClient(LLMConfig.from_settings(_settings()))


@pytest.fixture
def glm():
    if not (os.environ.get("GLM_API_KEY") or os.environ.get("ZHIPU_API_KEY")):
        pytest.skip("GLM_API_KEY not set")
    return EmbeddingClient(EmbeddingConfig.from_settings(_settings()))


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True)) / (
        sum(x * x for x in a) ** 0.5 * sum(y * y for y in b) ** 0.5)


async def test_glm_embedding_dimensions_and_similarity(glm):
    a, b, c = await glm.embed(["剂量率单位换算：1 mSv/h 等于 1000 µSv/h",
                               "毫希沃特每小时和微希沃特每小时怎么换算",
                               "今天食堂的午饭是什么"])
    assert len(a) == len(b) == len(c) == 1024
    assert _cos(a, b) > _cos(a, c)
    await glm.aclose()


async def test_deepseek_streams_and_calls_tools(deepseek):
    tools = [{"type": "function", "function": {
        "name": "search_knowledge_base", "description": "检索站内知识库",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}}]
    items = [i async for i in deepseek.stream_chat(
        [{"role": "system", "content": "专业问题必须先调用 search_knowledge_base 检索。"},
         {"role": "user", "content": "请依据知识库说明 mSv/h 与 µSv/h 的换算关系。"}],
        tools=tools)]
    result = items[-1]
    assert result.tool_calls and result.tool_calls[0].name == "search_knowledge_base"
    await deepseek.aclose()


async def test_live_agent_rag_end_to_end(deepseek, glm, tmp_path):
    """Real GLM embeddings into Milvus Lite + real DeepSeek agent: a cited answer."""
    store = MilvusKnowledgeStore(str(tmp_path / "live.db"), alias="live_kb", index_type="FLAT")
    report = await build_knowledge_base(SAMPLE, store, glm, chunk_chars=800, chunk_overlap=120)
    assert report.embedding == {"model": "embedding-3", "dimensions": 1024}
    retriever = KnowledgeRetriever(store, glm, pinned_version=None, top_k=4, min_score=0.3,
                                   allowed_classifications=["public", "internal"])
    runner = AgentRunner(deepseek, [KnowledgeSearchTool(retriever)],
                         model_info=ModelInfo(provider="deepseek", name="deepseek-chat",
                                              precision="api", revision="unknown"))
    events = [e async for e in runner.run([], "85 nSv/h 换算成 µSv/h 是多少？请给出依据。",
                                          ToolContext(Principal("test", "live-user")))]
    final = events[-1]
    assert isinstance(final, AgentFinal)
    print("\nANSWER:", final.content)
    print("CITATIONS:", [(c.ref, c.doc_id, c.section) for c in final.citations])
    assert any(isinstance(e, AgentDelta) for e in events)
    assert "search_knowledge_base" in final.tools_used
    assert "0.085" in final.content
    assert any(c.doc_id == "dose-rate-units" for c in final.citations)
    store.close()
    await deepseek.aclose()
    await glm.aclose()
