"""Assistant end to end through the HTTP API: agent-driven retrieval, streaming, citations,
persistence, history, and per-user isolation. The model is a scripted OpenAI-compatible
stream; the knowledge base is Milvus Lite built from examples/knowledge."""

import json
from pathlib import Path

import httpx
import pytest

from industrial_ai.api.deps import build_container
from industrial_ai.api.main import create_app
from industrial_ai.knowledge.builder import build_knowledge_base
from industrial_ai.knowledge.milvus_store import MilvusKnowledgeStore
from industrial_ai.knowledge.retriever import KnowledgeRetriever
from industrial_ai.storage.conversations import InMemoryConversationStore

from .conftest import TOKEN
from .fakes import FakeEmbedder, ScriptedLLM, text_reply, tool_reply

pytestmark = pytest.mark.anyio

SAMPLE = Path(__file__).parent.parent / "examples" / "knowledge" / "manifest.json"
ALICE = {"Authorization": f"Bearer {TOKEN}", "X-User-Id": "alice", "X-User-Roles": "operator"}
BOB = {"Authorization": f"Bearer {TOKEN}", "X-User-Id": "bob"}


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines()
                      if not line.startswith(":"))
        if fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


@pytest.fixture
async def kb(tmp_path):
    pytest.importorskip("milvus_lite", reason="Milvus Lite is not available on Windows")
    store = MilvusKnowledgeStore(str(tmp_path / "kb.db"), alias="kb", index_type="FLAT")
    embedder = FakeEmbedder()
    await build_knowledge_base(SAMPLE, store, embedder, chunk_chars=200, chunk_overlap=30)
    embedder.calls.clear()  # only count query-time embeddings in tests
    retriever = KnowledgeRetriever(store, embedder, pinned_version=None, top_k=3,
                                   min_score=0.05,
                                   allowed_classifications=["public", "internal"])
    yield retriever, embedder
    store.close()


@pytest.fixture
async def make_client(settings, catalog, timeseries):
    containers = []

    async def make(llm: ScriptedLLM | None, retriever=None, store=None):
        container = build_container(
            settings, catalog=catalog, timeseries=timeseries, retriever=retriever,
            conversations=store or InMemoryConversationStore(),
            llm=llm.client() if llm else None)
        containers.append(container)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(
            container=container)), base_url="http://t")

    yield make
    for c in containers:
        await c.aclose()


async def chat(client, headers, message, session_id=None):
    body = {"message": message, **({"session_id": session_id} if session_id else {})}
    resp = await client.post("/internal/v1/assistant/chat", json=body, headers=headers)
    return resp, parse_sse(resp.text) if resp.status_code == 200 else []


async def test_greeting_answers_without_retrieval(kb, make_client):
    retriever, embedder = kb
    llm = ScriptedLLM(text_reply("您好，我可以帮您查询设备状态和技术资料。"))
    client = await make_client(llm, retriever)
    resp, events = await chat(client, ALICE, "你好")

    assert resp.status_code == 200
    kinds = [k for k, _ in events]
    assert kinds[0] == "stage" and kinds[-2:] == ["result", "done"]
    assert "".join(d["text"] for k, d in events if k == "delta") == \
        "您好，我可以帮您查询设备状态和技术资料。"
    result = events[-2][1]
    assert result["citations"] == [] and result["tools_used"] == []
    assert embedder.calls == []  # the model chose not to search; no retrieval happened
    offered = {t["function"]["name"] for t in llm.requests[0]["tools"]}
    assert offered == {"search_knowledge_base", "get_equipment_health", "get_device_readings"}


async def test_model_decides_to_search_and_cites(kb, make_client):
    retriever, _ = kb
    llm = ScriptedLLM(
        tool_reply("search_knowledge_base", {"query": "mSv/h 与 µSv/h 换算关系"}),
        text_reply("1 mSv/h 等于 1000 µSv/h [1]。"),
    )
    client = await make_client(llm, retriever)
    resp, events = await chat(client, ALICE, "毫希每小时和微希每小时怎么换算？")

    stages = [d["stage"] for k, d in events if k == "stage"]
    assert "retrieving_knowledge" in stages
    citations = [d for k, d in events if k == "citation"]
    assert len(citations) == 1 and citations[0]["ref"] == 1
    assert citations[0]["kind"] == "knowledge" and citations[0]["doc_id"] == "dose-rate-units"
    result = events[-2][1]
    assert result["knowledge_index_version"] == "sample_2026_11_05"
    assert result["tools_used"] == ["search_knowledge_base"]
    assert result["model"]["provider"] == "deepseek"

    # The second model call received the numbered tool result.
    tool_msg = llm.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["content"].count("[1]") == 1
    assert llm.requests[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == \
        "search_knowledge_base"

    # Persisted with citations, retrievable by the same user.
    session_id = resp.headers["x-session-id"]
    page = (await client.get(f"/internal/v1/assistant/sessions/{session_id}/messages",
                             headers=ALICE)).json()
    assert [m["role"] for m in page["items"]] == ["user", "assistant"]
    assert page["items"][1]["citations"][0]["chunk_id"].startswith("dose-rate-units#")


async def test_data_tool_and_no_hit_handling(kb, make_client):
    retriever, _ = kb
    llm = ScriptedLLM(
        tool_reply("get_device_readings", {"site_id": "S1", "equipment_id": "ROOM-HOT",
                                           "metric": "m1", "hours": 2}),
        text_reply("ROOM-HOT 最近 2 小时出现连续超限 [1]。"),
    )
    client = await make_client(llm, retriever)
    _, events = await chat(client, ALICE, "ROOM-HOT 最近怎么样？")
    cite = next(d for k, d in events if k == "citation")
    assert cite["kind"] == "data" and cite["equipment_id"] == "ROOM-HOT"
    tool_text = llm.requests[1]["messages"][-1]["content"]
    assert tool_text.startswith("设备读数统计：\n\n[1] room hot m1 读数\n")
    body = json.loads(tool_text.rsplit("\n", 1)[1])
    assert body["health_level"] == "inspect" and body["max"] == 95.0


async def test_role_filtering_reaches_the_agent(kb, make_client):
    retriever, _ = kb
    query = {"query": "受限文档 radiation_protection 角色 过滤"}
    for headers, visible in ((ALICE, False),
                             ({**ALICE, "X-User-Roles": "radiation_protection"}, True)):
        llm = ScriptedLLM(tool_reply("search_knowledge_base", query), text_reply("答复。"))
        client = await make_client(llm, retriever)
        await chat(client, headers, "受限文档说了什么？")
        tool_result = llm.requests[1]["messages"][-1]["content"]
        assert ("受限访问示例文档" in tool_result) is visible


async def test_history_is_sent_on_follow_up(make_client):
    llm = ScriptedLLM(text_reply("第一次回答。"), text_reply("第二次回答。"))
    client = await make_client(llm)
    resp, _ = await chat(client, ALICE, "第一个问题")
    session_id = resp.headers["x-session-id"]
    await chat(client, ALICE, "追问", session_id)
    roles_contents = [(m["role"], m["content"]) for m in llm.requests[1]["messages"][1:]]
    assert roles_contents == [("user", "第一个问题"), ("assistant", "第一次回答。"),
                              ("user", "追问")]
    # No knowledge base configured -> search tool not offered, prompt says so.
    assert "知识库当前未接入" in llm.requests[0]["messages"][0]["content"]
    assert "search_knowledge_base" not in {t["function"]["name"] for t in llm.requests[0]["tools"]}


async def test_sessions_are_isolated_per_user(make_client):
    client = await make_client(ScriptedLLM(text_reply("好的。")))
    resp, _ = await chat(client, ALICE, "Alice 的问题")
    session_id = resp.headers["x-session-id"]

    listed = (await client.get("/internal/v1/assistant/sessions", headers=ALICE)).json()
    assert [s["session_id"] for s in listed["items"]] == [session_id]
    assert listed["items"][0]["title"] == "Alice 的问题"
    assert (await client.get("/internal/v1/assistant/sessions", headers=BOB)).json()["items"] == []
    for method, path in (("GET", f"/sessions/{session_id}"),
                         ("GET", f"/sessions/{session_id}/messages"),
                         ("DELETE", f"/sessions/{session_id}")):
        r = await client.request(method, f"/internal/v1/assistant{path}", headers=BOB)
        assert r.status_code == 404
    r, _ = await chat(client, BOB, "冒用会话", session_id)
    assert r.status_code == 404


async def test_session_crud_and_feedback(make_client):
    client = await make_client(ScriptedLLM(text_reply("答。")))
    created = (await client.post("/internal/v1/assistant/sessions", json={"title": "新会话"},
                                 headers=ALICE)).json()
    sid = created["session_id"]
    _, events = await chat(client, ALICE, "问", sid)
    message_id = events[-1][1]["message_id"]

    patched = await client.patch(f"/internal/v1/assistant/sessions/{sid}",
                                 json={"title": "改名", "archived": True}, headers=ALICE)
    assert patched.json()["title"] == "改名" and patched.json()["archived"]
    fb = f"/internal/v1/assistant/messages/{message_id}/feedback"
    assert (await client.post(fb, json={"rating": 1}, headers=ALICE)).status_code == 204
    assert (await client.post(fb, json={"rating": 1}, headers=BOB)).status_code == 404
    assert (await client.delete(f"/internal/v1/assistant/sessions/{sid}",
                                headers=ALICE)).status_code == 204
    assert (await client.get(f"/internal/v1/assistant/sessions/{sid}",
                             headers=ALICE)).status_code == 404


async def test_model_failure_is_reported_and_persisted(make_client):
    store = InMemoryConversationStore()
    client = await make_client(ScriptedLLM(httpx.Response(500), httpx.Response(500)),
                               store=store)
    resp, events = await chat(client, ALICE, "问题")
    assert [k for k, _ in events][-2:] == ["error", "done"]
    assert events[-2][1]["code"] == "llm_unavailable"
    page = (await client.get(
        f"/internal/v1/assistant/sessions/{resp.headers['x-session-id']}/messages",
        headers=ALICE)).json()
    assert [(m["role"], m["status"]) for m in page["items"]] == [("user", "complete"),
                                                                 ("assistant", "error")]


async def test_requires_user_and_llm(make_client):
    client = await make_client(None)
    r, _ = await chat(client, {"Authorization": f"Bearer {TOKEN}"}, "hi")
    assert r.status_code == 401 and r.json()["error"]["code"] == "user_identity_required"
    r, _ = await chat(client, ALICE, "hi")
    assert r.status_code == 503 and r.json()["error"]["code"] == "llm_unavailable"


async def test_last_step_forces_an_answer(make_client, settings):
    # The model keeps asking for tools; on the final step no tools are offered.
    responses = [tool_reply("get_equipment_health", {"site_id": "S1"}, f"c{i}")
                 for i in range(settings.assistant_max_tool_steps - 1)]
    llm = ScriptedLLM(*responses, text_reply("综合结论 [1]。"))
    client = await make_client(llm)
    _, events = await chat(client, ALICE, "全站情况")
    assert events[-2][0] == "result"
    assert "tools" not in llm.requests[-1]
