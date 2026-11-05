"""ConversationStore contract, run against every implementation.

openGauss runs only when OPENGAUSS_TEST_DSN points at a dedicated, migrated test database
(tables are emptied before each test):

    OPENGAUSS_TEST_DSN="host=127.0.0.1 port=15432 dbname=industrial_ai user=app_ai password=..."
"""

import os

import pytest

from industrial_ai.core.identity import Principal
from industrial_ai.schemas.analysis import ModelInfo
from industrial_ai.schemas.assistant import Citation
from industrial_ai.storage.conversations import InMemoryConversationStore, NewMessage

pytestmark = pytest.mark.anyio
DSN = os.environ.get("OPENGAUSS_TEST_DSN")

ALICE = Principal(issuer="mes-main-backend", subject="alice", display_name="Alice")
BOB = Principal(issuer="mes-main-backend", subject="bob")


@pytest.fixture(params=["memory", "opengauss"])
async def store(request):
    if request.param == "memory":
        yield InMemoryConversationStore()
        return
    if not DSN:
        pytest.skip("OPENGAUSS_TEST_DSN not set")
    import psycopg

    from industrial_ai.storage.opengauss import OpenGaussConversationStore, migrate

    await migrate(DSN)
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        for table in ("message_feedback", "message_citations", "chat_messages",
                      "chat_sessions", "app_users"):
            await conn.execute(f"DELETE FROM {table}")
    s = OpenGaussConversationStore(DSN, min_size=1, max_size=4)
    yield s
    await s.close()


async def test_users_are_keyed_by_issuer_and_subject(store):
    a1 = await store.ensure_user(ALICE)
    a2 = await store.ensure_user(Principal(issuer="mes-main-backend", subject="alice"))
    other_idp = await store.ensure_user(Principal(issuer="client-idp", subject="alice"))
    assert a1.user_id == a2.user_id
    assert a2.display_name == "Alice"  # kept when not re-sent
    assert other_idp.user_id != a1.user_id


async def test_sessions_are_private(store):
    alice = await store.ensure_user(ALICE)
    bob = await store.ensure_user(BOB)
    s = await store.create_session(alice.user_id, "first")

    assert await store.get_session(bob.user_id, s.session_id) is None
    assert await store.list_sessions(bob.user_id, limit=10, before=None,
                                     include_archived=True) == []
    assert await store.update_session(bob.user_id, s.session_id, title="x",
                                      archived=None) is None
    assert await store.delete_session(bob.user_id, s.session_id) is False
    with pytest.raises(KeyError):
        await store.add_message(bob.user_id, NewMessage(session_id=s.session_id, role="user",
                                                        content="hi"))
    assert (await store.get_session(alice.user_id, s.session_id)).title == "first"


async def test_messages_citations_and_paging(store):
    alice = await store.ensure_user(ALICE)
    s = await store.create_session(alice.user_id, None)
    cite = Citation(ref=1, kind="knowledge", title="剂量率单位与换算", doc_id="dose-rate-units",
                    chunk_id="dose-rate-units#00001", section="2 换算关系", kb_version="v1",
                    score=0.91, snippet="1 mSv/h 等于 1000 µSv/h")
    model = ModelInfo(provider="vllm", name="qwen3", precision="W8A8", revision="r1")
    for i in range(3):
        await store.add_message(alice.user_id, NewMessage(s.session_id, "user", f"问题{i}"))
        await store.add_message(alice.user_id, NewMessage(
            s.session_id, "assistant", f"回答{i} [1]", citations=[cite],
            knowledge_index_version="v1", model=model, tools_used=["search_knowledge_base"],
            tool_trace=[{"tool": "search_knowledge_base", "arguments": {"query": "单位"}}],
            prompt_tokens=10, completion_tokens=5, latency_ms=120))
    await store.add_message(alice.user_id, NewMessage(s.session_id, "assistant", "",
                                                      status="error", error_code="llm_down"))

    page = await store.list_messages(alice.user_id, s.session_id, limit=3, before_seq=None)
    assert [m.seq for m in page] == [5, 6, 7]
    assert page[1].citations == [cite] and page[1].model == model
    assert page[1].tools_used == ["search_knowledge_base"]
    assert page[2].status == "error" and page[2].content == "" and page[2].error_code == "llm_down"
    older = await store.list_messages(alice.user_id, s.session_id, limit=10, before_seq=5)
    assert [m.seq for m in older] == [1, 2, 3, 4] and older[0].content == "问题0"
    assert (await store.get_session(alice.user_id, s.session_id)).message_count == 7


async def test_list_update_archive_delete(store):
    alice = await store.ensure_user(ALICE)
    s1 = await store.create_session(alice.user_id, "one")
    s2 = await store.create_session(alice.user_id, "two")
    await store.add_message(alice.user_id, NewMessage(s1.session_id, "user", "bump"))

    listed = await store.list_sessions(alice.user_id, limit=10, before=None,
                                       include_archived=False)
    assert [s.session_id for s in listed] == [s1.session_id, s2.session_id]  # most recent first
    page2 = await store.list_sessions(alice.user_id, limit=10, before=listed[0].updated_at,
                                      include_archived=False)
    assert [s.session_id for s in page2] == [s2.session_id]

    updated = await store.update_session(alice.user_id, s2.session_id, title="renamed",
                                         archived=True)
    assert updated.title == "renamed" and updated.archived
    assert [s.session_id for s in await store.list_sessions(
        alice.user_id, limit=10, before=None, include_archived=False)] == [s1.session_id]

    assert await store.delete_session(alice.user_id, s1.session_id)
    assert await store.get_session(alice.user_id, s1.session_id) is None
    assert await store.list_messages(alice.user_id, s1.session_id, limit=10,
                                     before_seq=None) == []


async def test_feedback_only_for_own_messages(store):
    alice = await store.ensure_user(ALICE)
    bob = await store.ensure_user(BOB)
    s = await store.create_session(alice.user_id, None)
    msg = await store.add_message(alice.user_id, NewMessage(s.session_id, "assistant", "答"))
    assert await store.add_feedback(alice.user_id, msg.message_id, 1, "有用")
    assert await store.add_feedback(alice.user_id, msg.message_id, -1, None)  # replaces
    assert not await store.add_feedback(bob.user_id, msg.message_id, 1, None)
    assert not await store.add_feedback(alice.user_id, "msg_" + "0" * 32, 1, None)
