"""Builds every service from settings once at startup; routes get them via `get_resources`.

openGauss (OPENGAUSS_DSN) stores only conversations (sessions, messages, report records) and
the copy of the main server's work orders. Without it both stay in memory (demos, tests).
The station directory is a file (STATIONS_FILE).

Who the end user is (`get_current_user`), until the client application has login:
  X-User-Id      set by the main server for an identified user          -> "<id>"
  X-Client-Id    random id the browser keeps in localStorage           -> "client:<id>"
  neither                                                               -> "anonymous"
This identifies, it does not authenticate (the service token / API key authenticates the
caller). When login exists, POST /api/v1/sessions/claim moves a browser's conversations to
the user. Roles (X-User-Roles) are not used yet.
"""

import logging
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from fastapi import Header, HTTPException, Request

from app.agent.chat import ChatService
from app.agent.graph import build_graph
from app.agent.llm import build_llm, model_label
from app.agent.reports import MemoryReportStore, PgReportStore, ReportStore
from app.agent.tools import ALL_TOOLS, DATA_TOOLS
from app.config import Settings
from app.db.chat_store import ChatStore, MemoryChatStore, PgChatStore
from app.db.pool import PgPool
from app.db.work_orders import MemoryWorkOrderStore, PgWorkOrderStore, WorkOrderStore
from app.maintenance.actions import ActionCatalog
from app.maintenance.narrator import Narrator
from app.maintenance.service import HistorySource, MaintenanceService
from app.rag.embeddings import GLMEmbeddings
from app.rag.rerank import Reranker
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeStore
from app.stations.service import StationService
from app.stations.store import StationStore
from app.timeseries.iotdb import IoTDB, MemoryTimeSeries, TimeSeriesStore

logger = logging.getLogger(__name__)
_USER_ID = re.compile(r"^[\w.@:\-]{1,128}$")


@dataclass
class Resources:
    settings: Settings
    timeseries: TimeSeriesStore
    stations: StationStore
    station_data: StationService
    maintenance: MaintenanceService
    work_orders: WorkOrderStore
    reports: ReportStore
    chat_store: ChatStore
    chat: ChatService
    retriever: Retriever | None = None
    knowledge: KnowledgeStore | None = None
    pool: PgPool | None = None

    async def close(self) -> None:
        await self.timeseries.close()
        await self.chat_store.close()
        await self.work_orders.close()
        if self.pool is not None:
            await self.pool.close()
        if self.knowledge is not None:
            self.knowledge.close()


def build_resources(s: Settings, *, timeseries: TimeSeriesStore | None = None,
                    chat_store: ChatStore | None = None, llm=None,
                    retriever: Retriever | None = None, stations: StationStore | None = None,
                    work_orders: WorkOrderStore | None = None,
                    reports: ReportStore | None = None) -> Resources:
    if timeseries is None:
        timeseries = IoTDB(s) if s.timeseries_backend == "iotdb" else MemoryTimeSeries()
    dsn = s.opengauss_dsn.get_secret_value() if s.opengauss_dsn is not None else None
    pool = PgPool(dsn) if dsn and (work_orders is None or reports is None) else None
    if dsn is None:
        logger.warning("OPENGAUSS_DSN not set: conversations and work orders kept in memory")
    stations = stations or StationStore.from_file(Path(s.stations_file))
    work_orders = work_orders or (PgWorkOrderStore(pool) if pool else MemoryWorkOrderStore())
    reports = reports or (PgReportStore(pool) if pool else MemoryReportStore())
    if chat_store is None:
        chat_store = PgChatStore(dsn) if dsn else MemoryChatStore()
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
                                  classifications=tuple(s.kb_classifications),
                                  reranker=Reranker.from_settings(s),
                                  candidates=s.kb_rerank_candidates)
    station_data = StationService(stations, timeseries)
    llm = llm if llm is not None else build_llm(s)
    narrator_llm = build_llm(s, model=s.maintenance_narrator_model_name, streaming=False) \
        if s.maintenance_narrator_model_name and llm is not None else llm
    maintenance = MaintenanceService(
        stations, station_data, HistorySource(work_orders, s),
        Narrator(narrator_llm, s.maintenance_narrator_max_chars,
                 s.maintenance_narrator_timeout_seconds),
        ActionCatalog.load(Path(s.maintenance_action_catalog_path)), s,
        (s.maintenance_narrator_model_name or s.llm_model_name or "fake")
        if llm is not None else None)
    graph = build_graph(llm, ALL_TOOLS if retriever else DATA_TOOLS,
                        max_tool_rounds=s.agent_max_tool_rounds) if llm is not None else None
    chat = ChatService(chat_store, graph, station_data, maintenance, work_orders, reports, s,
                       retriever, model=model_label(s),
                       history_messages=s.agent_history_messages,
                       max_input_chars=s.agent_max_input_chars,
                       max_tool_rounds=s.agent_max_tool_rounds)
    return Resources(s, timeseries, stations, station_data, maintenance, work_orders, reports,
                     chat_store, chat, retriever, knowledge, pool)


def get_resources(request: Request) -> Resources:
    return request.app.state.resources


def _clean(value: str | None, header: str) -> str | None:
    if value is None or not value.strip():
        return None
    if not _USER_ID.fullmatch(value.strip()):
        raise HTTPException(status_code=400, detail=f"invalid {header}")
    return value.strip()


def get_current_user(x_user_id: str | None = Header(default=None),
                     x_client_id: str | None = Header(default=None)) -> str:
    user = _clean(x_user_id, "X-User-Id")
    if user:
        return user
    client = _clean(x_client_id, "X-Client-Id")
    return f"client:{client}" if client else "anonymous"


def check_api_key(request: Request, x_api_key: str | None = Header(default=None),
                  authorization: str | None = Header(default=None)) -> None:
    """Service authentication: `Authorization: Bearer <COMPUTE_SERVICE_TOKEN>` (main server,
    design doc) or `X-API-Key: <API_KEY>`. Open when neither is configured."""
    s = request.app.state.resources.settings
    expected = [(k.get_secret_value(), v) for k, v in
                ((s.compute_service_token, "bearer"), (s.api_key, "key"))
                if k is not None and k.get_secret_value()]
    if not expected:
        return
    bearer = authorization[7:].strip() if authorization and \
        authorization[:7].lower() == "bearer " else None
    for secret, kind in expected:
        given = bearer if kind == "bearer" else x_api_key
        if given and secrets.compare_digest(given.encode(), secret.encode()):
            return
    raise HTTPException(status_code=401, detail="invalid or missing service credentials")
