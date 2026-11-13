"""Conversation store contract: memory always, openGauss when OPENGAUSS_TEST_DSN points at a
dedicated test database (its chat tables are emptied)."""

import os

import pytest

from app.db.chat_store import Citation, MemoryChatStore, NewMessage

pytestmark = pytest.mark.anyio
DSN = os.environ.get("OPENGAUSS_TEST_DSN")


@pytest.fixture(params=["memory", "opengauss"])
async def chat_store(request):
    if request.param == "memory":
        yield MemoryChatStore()
        return
    if not DSN:
        pytest.skip("OPENGAUSS_TEST_DSN not set")
    import psycopg

    from app.db.chat_store import PgChatStore
    from app.db.migrate import migrate

    await migrate(DSN)
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as c:
        await c.execute("DELETE FROM chat_message")
        await c.execute("DELETE FROM chat_session")
    s = PgChatStore(DSN)
    yield s
    await s.close()


async def test_sessions_messages_and_scoping(chat_store):
    s = await chat_store.create_session("alice", "标题")
    cite = Citation(ref=1, kind="knowledge", title="剂量率单位与换算", doc_id="dose-rate-units",
                    kb_version="v1", score=0.9)
    await chat_store.add_message("alice", s.session_id, NewMessage(role="user", content="问"))
    m = await chat_store.add_message("alice", s.session_id, NewMessage(
        role="assistant", content="答 [1]", citations=[cite], model="deepseek:deepseek-chat",
        kb_version="v1", tool_calls=[{"name": "search_knowledge", "args": {"query": "q"}}]))
    await chat_store.add_message("alice", s.session_id, NewMessage(
        role="assistant", content="", status="error", error="boom"))

    msgs = await chat_store.messages("alice", s.session_id, 10)
    assert [x.seq for x in msgs] == [1, 2, 3]
    assert msgs[1].citations == [cite] and msgs[1].tools_used == ["search_knowledge"]
    assert msgs[2].status == "error" and msgs[2].content == ""
    assert [x.seq for x in await chat_store.messages("alice", s.session_id, 10, before_seq=3)] \
        == [1, 2]
    assert (await chat_store.get_session("alice", s.session_id)).message_count == 3

    assert await chat_store.get_session("bob", s.session_id) is None
    assert await chat_store.messages("bob", s.session_id, 10) == []
    assert await chat_store.list_sessions("bob", 10, 0) == []
    with pytest.raises(KeyError):
        await chat_store.add_message("bob", s.session_id, NewMessage(role="user", content="x"))
    assert not await chat_store.set_feedback("bob", m.message_id, 1, None)
    assert await chat_store.set_feedback("alice", m.message_id, -1, "不准确")
    assert (await chat_store.messages("alice", s.session_id, 10))[1].feedback == -1

    assert (await chat_store.rename_session("alice", s.session_id, "新")).title == "新"
    assert await chat_store.delete_session("alice", s.session_id)
    assert await chat_store.get_session("alice", s.session_id) is None
    assert await chat_store.list_sessions("alice", 10, 0) == []


async def test_workflow_and_claim(chat_store):
    s = await chat_store.create_session("client:abc", "报表")
    await chat_store.set_workflow("client:abc", s.session_id, "monitoring_report")
    await chat_store.set_workflow("client:abc", s.session_id, "system_info")  # first one wins
    got = await chat_store.get_session("client:abc", s.session_id)
    assert got.workflow == "monitoring_report"
    assert await chat_store.reassign("client:abc", "zhang.san") == 1
    assert await chat_store.get_session("client:abc", s.session_id) is None
    assert (await chat_store.get_session("zhang.san", s.session_id)).workflow == \
        "monitoring_report"
