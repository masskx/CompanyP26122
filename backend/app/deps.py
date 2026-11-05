"""Builds every service from settings once at startup; routes get them via `get_resources`.

`get_current_user` is the single place that decides who the end user is. There is no login
yet: it returns the optional X-User-Id header (identification only, NOT authentication) or
"anonymous". When the client application adds login, verify its token here and return the
user's id from the client's user table; nothing else changes.
"""

import logging
import re
import secrets
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

from app.agent.chat import ChatService
from app.agent.graph import build_graph
from app.agent.llm import build_llm, model_label
from app.agent.tools import ALL_TOOLS, DATA_TOOLS
from app.config import Settings
from app.db.chat_store import ChatStore, MemoryChatStore, PgChatStore
from app.rag.embeddings import GLMEmbeddings
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeStore
from app.timeseries.devices import DeviceCatalog
from app.timeseries.iotdb import IoTDB, MemoryTimeSeries
from app.timeseries.service import DeviceService, TimeSeriesStore

logger = logging.getLogger(__name__)
_USER_ID = re.compile(r"^[\w.@:\-]{1,128}$")


@dataclass
class Resources:
    settings: Settings
    timeseries: TimeSeriesStore
    devices: DeviceService
    chat_store: ChatStore
    chat: ChatService
    retriever: Retriever | None = None
    knowledge: KnowledgeStore | None = None

    async def close(self) -> None:
        await self.timeseries.close()
        await self.chat_store.close()
        if self.knowledge is not None:
            self.knowledge.close()


def build_resources(s: Settings, *, timeseries: TimeSeriesStore | None = None,
                    chat_store: ChatStore | None = None, llm=None,
                    retriever: Retriever | None = None, catalog: DeviceCatalog | None = None
                    ) -> Resources:
    catalog = catalog or DeviceCatalog.load(s.devices_file)
    if timeseries is None:
        timeseries = IoTDB(s) if s.timeseries_backend == "iotdb" else MemoryTimeSeries()
    if chat_store is None:
        if s.opengauss_dsn is not None:
            chat_store = PgChatStore(s.opengauss_dsn.get_secret_value())
        else:
            logger.warning("OPENGAUSS_DSN not set: conversations are kept in memory only")
            chat_store = MemoryChatStore()
    knowledge = None
    if retriever is None and s.milvus_uri:
        embeddings = GLMEmbeddings.from_settings(s)
        if embeddings is None:
            logger.warning("MILVUS_URI set but EMBEDDING_PROVIDER=none: knowledge search off")
        else:
            knowledge = KnowledgeStore(s.milvus_uri, s.kb_name, s.milvus_token.get_secret_value()
                                       if s.milvus_token else None)
            retriever = Retriever(knowledge, embeddings, top_k=s.kb_top_k,
                                  min_score=s.kb_min_score, pinned_version=s.kb_version,
                                  classifications=tuple(s.kb_classifications))
    devices = DeviceService(catalog, timeseries, s)
    llm = llm if llm is not None else build_llm(s)
    graph = build_graph(llm, ALL_TOOLS if retriever else DATA_TOOLS,
                        max_tool_rounds=s.agent_max_tool_rounds) if llm is not None else None
    chat = ChatService(chat_store, graph, devices, retriever, model=model_label(s),
                       history_messages=s.agent_history_messages,
                       max_input_chars=s.agent_max_input_chars,
                       max_tool_rounds=s.agent_max_tool_rounds)
    return Resources(s, timeseries, devices, chat_store, chat, retriever, knowledge)


def get_resources(request: Request) -> Resources:
    return request.app.state.resources


def get_current_user(x_user_id: str | None = Header(default=None)) -> str:
    if x_user_id is None or not x_user_id.strip():
        return "anonymous"
    if not _USER_ID.fullmatch(x_user_id.strip()):
        raise HTTPException(status_code=400, detail="invalid X-User-Id")
    return x_user_id.strip()


def check_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Optional shared key (API_KEY). Disabled when API_KEY is empty."""
    expected = request.app.state.resources.settings.api_key
    if expected is None or not expected.get_secret_value():
        return
    if not secrets.compare_digest((x_api_key or "").encode(), expected.get_secret_value().encode()):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
