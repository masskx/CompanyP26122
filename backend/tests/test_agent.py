"""LangGraph agent through the HTTP API with a scripted model: tool decisions, streaming,
citations, persistence, history and per-user sessions."""


import pytest
from langchain_core.messages import AIMessage

from app.rag.ingest import build
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeStore

from .conftest import KNOWLEDGE, FakeEmbeddings, parse_sse, tool_call

pytestmark = pytest.mark.anyio
ALICE, BOB = {"X-User-Id": "alice"}, {"X-User-Id": "bob"}


@pytest.fixture
def retriever(tmp_path):
    pytest.importorskip("milvus_lite", reason="Milvus Lite has no Windows build")
    store = KnowledgeStore(str(tmp_path / "kb.db"), "kb", index_type="FLAT")
    emb = FakeEmbeddings()
    build(KNOWLEDGE, store, emb, chunk_size=300, overlap=40)
    yield Retriever(store, emb, top_k=3, min_score=0.05)
    store.close()


async def test_answers_directly_without_tools(make_client, retriever):
    client, llm = await make_client([AIMessage(content="您好，我可以查询设备数据和技术资料。")],
                                    retriever=retriever)
    emb_calls_before = retriever.embeddings.calls
    r = await client.post("/chat", json={"message": "你好"}, headers=ALICE)
    events = parse_sse(r.text)
    kinds = [k for k, _ in events]
    assert kinds[0] == "session" and kinds[-1] == "done" and "tool" not in kinds
    text = "".join(d["text"] for k, d in events if k == "delta")
    assert text == "您好，我可以查询设备数据和技术资料。"
    assert retriever.embeddings.calls == emb_calls_before  # model chose not to search
    assert llm.calls[0]["tools"] is True  # tools were offered


async def test_knowledge_search_with_citation(make_client, retriever):
    client, llm = await make_client([
        tool_call("search_knowledge", {"query": "mGy/h 与 µGy/h 换算关系"}),
        AIMessage(content="1 mGy/h 等于 1000 µGy/h [1]。"),
    ], retriever=retriever)
    r = await client.post("/chat", json={"message": "mGy/h 和 µGy/h 怎么换算？"}, headers=ALICE)
    events = parse_sse(r.text)
    assert ("tool", {"name": "search_knowledge", "args": {"query": "mGy/h 与 µGy/h 换算关系"}}) \
        in events
    cites = [d for k, d in events if k == "citation"]
    assert cites[0]["ref"] == 1 and cites[0]["doc_id"] == "dose-rate-units"
    done = events[-1][1]["message"]
    assert done["kb_version"] == "sample_2026_11_09" and done["tools_used"] == ["search_knowledge"]
    tool_msg = llm.calls[1]["messages"][-1]
    assert tool_msg.type == "tool" and "[1]" in tool_msg.content


async def test_station_tool_reads_iotdb(make_client):
    client, llm = await make_client([
        tool_call("get_station_status", {"station_id": "AS01"}),
        AIMessage(content="AS01 最新数据见 [1]。"),
    ])
    r = await client.post("/chat", json={"message": "AS01 现在怎么样", "stream": False},
                          headers=ALICE)
    msg = r.json()["message"]
    assert msg["citations"][0]["kind"] == "data" and msg["citations"][0]["device_id"] == "AS01"
    result = llm.calls[1]["messages"][-1].content
    assert '"battery_voltage"' in result and '"dose_rate_avg"' in result
    # Without a knowledge base the search tool is not offered and the prompt says so.
    assert "知识库当前不可用" in llm.calls[0]["messages"][0].content


async def test_maintenance_and_work_order_tools(make_client):
    client, llm = await make_client([
        tool_call("run_maintenance_check", {"station_id": "AS06"}, "c1"),
        tool_call("get_work_orders", {"station_id": "AS06"}, "c2"),
        AIMessage(content="AS06 建议检修 [1]，暂无工单 [2]。"),
    ])
    out = await client.post("/chat", json={"message": "AS06 要不要检修", "stream": False},
                            headers=ALICE)
    assert out.json()["message"]["tools_used"] == ["get_work_orders", "run_maintenance_check"]
    check = llm.calls[1]["messages"][-1].content
    assert '"health_level":"inspect"' in check and "BACKUP-RUNTIME-DROP" in check
    assert "没有工单记录" in llm.calls[2]["messages"][-1].content


async def test_tool_rounds_are_capped(make_client, settings):
    loops = [tool_call("list_stations", {}, f"c{i}") for i in range(settings.agent_max_tool_rounds)]
    client, llm = await make_client([*loops, AIMessage(content="结论 [1]。")])
    r = await client.post("/chat", json={"message": "x", "stream": False}, headers=ALICE)
    assert r.json()["message"]["content"] == "结论 [1]。"
    assert llm.calls[-1]["tools"] is False  # final call without tools forces an answer


async def test_history_and_user_scoping(make_client):
    client, llm = await make_client([AIMessage(content="第一次回答。"),
                                     AIMessage(content="第二次回答。")])
    first = (await client.post("/chat", json={"message": "第一个问题", "stream": False},
                               headers=ALICE)).json()
    sid = first["session_id"]
    await client.post("/chat", json={"message": "追问", "session_id": sid, "stream": False},
                      headers=ALICE)
    sent = [(m.type, m.content) for m in llm.calls[1]["messages"][1:]]
    assert sent == [("human", "第一个问题"), ("ai", "第一次回答。"), ("human", "追问")]

    assert [s["session_id"] for s in (await client.get("/sessions", headers=ALICE)).json()] == [sid]
    assert (await client.get("/sessions", headers=BOB)).json() == []
    assert (await client.get(f"/sessions/{sid}/messages", headers=BOB)).status_code == 404
    r = await client.post("/chat", json={"message": "hi", "session_id": sid}, headers=BOB)
    assert r.status_code == 404
    msgs = (await client.get(f"/sessions/{sid}/messages", headers=ALICE)).json()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    # anonymous when no header
    assert (await client.get("/sessions")).json() == []


async def test_session_management_and_feedback(make_client):
    client, _ = await make_client([AIMessage(content="好的。")])
    out = (await client.post("/chat", json={"message": "问", "stream": False},
                             headers=ALICE)).json()
    sid, mid = out["session_id"], out["message"]["message_id"]
    assert (await client.patch(f"/sessions/{sid}", json={"title": "新标题"},
                               headers=ALICE)).json()["title"] == "新标题"
    assert (await client.post(f"/messages/{mid}/feedback", json={"rating": 1},
                              headers=ALICE)).status_code == 204
    assert (await client.post(f"/messages/{mid}/feedback", json={"rating": 1},
                              headers=BOB)).status_code == 404
    assert (await client.delete(f"/sessions/{sid}", headers=ALICE)).status_code == 204
    assert (await client.get(f"/sessions/{sid}", headers=ALICE)).status_code == 404


async def test_llm_not_configured(make_client):
    client, _ = await make_client(None)
    r = await client.post("/chat", json={"message": "hi"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "llm_not_configured"


async def test_model_failure_is_stored_as_error(make_client):
    client, llm = await make_client([])
    llm.responses.append(None)  # FakeChatModel will fail on None
    r = await client.post("/chat", json={"message": "x"}, headers=ALICE)
    events = parse_sse(r.text)
    assert events[-1][0] == "error"
    sid = events[0][1]["session_id"]
    msgs = (await client.get(f"/sessions/{sid}/messages", headers=ALICE)).json()
    assert [m["status"] for m in msgs] == ["complete", "error"]
