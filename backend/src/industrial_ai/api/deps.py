"""Dependency container: wires settings to concrete implementations and services.

Every external dependency (IoTDB, openGauss, Milvus, embedding model, LLM, identity provider)
is chosen here from configuration; services only see interfaces. Tests inject fakes via the
keyword arguments of build_container().
"""

import logging
from dataclasses import dataclass

from fastapi import Request

from industrial_ai.agent.runner import AgentRunner
from industrial_ai.agent.tools import (
    AgentTool,
    DeviceReadingsTool,
    EquipmentHealthTool,
    KnowledgeSearchTool,
)
from industrial_ai.analytics.rules import RuleConfig
from industrial_ai.collector.pipeline import IngestionPipeline
from industrial_ai.core.config import Settings
from industrial_ai.core.identity import IdentityProvider, build_identity_provider
from industrial_ai.integrations.embedding_client import EmbeddingClient, EmbeddingConfig
from industrial_ai.integrations.llm_client import LLMClient, LLMConfig
from industrial_ai.knowledge.milvus_store import MilvusKnowledgeStore
from industrial_ai.knowledge.retriever import KnowledgeRetriever
from industrial_ai.schemas.analysis import ModelInfo
from industrial_ai.services.assistant_service import AssistantService
from industrial_ai.services.equipment_service import EquipmentService
from industrial_ai.services.maintenance_explainer import MaintenanceExplainer
from industrial_ai.services.maintenance_service import MaintenanceService
from industrial_ai.storage.base import AnalysisStore, EquipmentCatalog, TimeSeriesStore
from industrial_ai.storage.catalog import StaticEquipmentCatalog
from industrial_ai.storage.conversations import ConversationStore, InMemoryConversationStore
from industrial_ai.storage.memory import InMemoryAnalysisStore, InMemoryTimeSeriesStore
from industrial_ai.workers.analysis_worker import AnalysisRunner

logger = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    catalog: EquipmentCatalog
    timeseries: TimeSeriesStore
    analysis_store: AnalysisStore
    conversations: ConversationStore
    identity: IdentityProvider
    runner: AnalysisRunner
    equipment: EquipmentService
    maintenance: MaintenanceService
    assistant: AssistantService
    llm: LLMClient | None = None
    embedder: EmbeddingClient | None = None
    knowledge_store: MilvusKnowledgeStore | None = None
    retriever: KnowledgeRetriever | None = None
    collector: IngestionPipeline | None = None  # only when embedded in the API process

    async def aclose(self) -> None:
        await self.runner.shutdown()
        await self.timeseries.close()
        await self.conversations.close()
        if self.llm is not None:
            await self.llm.aclose()
        if self.embedder is not None:
            await self.embedder.aclose()
        if self.knowledge_store is not None:
            self.knowledge_store.close()


def build_timeseries(settings: Settings) -> TimeSeriesStore:
    if settings.timeseries_backend == "iotdb":
        from industrial_ai.storage.iotdb_client import IoTDBTimeSeriesStore

        return IoTDBTimeSeriesStore.from_settings(settings)
    return InMemoryTimeSeriesStore()


def build_catalog(settings: Settings) -> EquipmentCatalog:
    if settings.equipment_catalog_path is None:
        logger.warning("EQUIPMENT_CATALOG_PATH not set; equipment catalog is empty")
        return StaticEquipmentCatalog([])
    return StaticEquipmentCatalog.from_file(settings.equipment_catalog_path)


def build_conversations(settings: Settings) -> ConversationStore:
    if settings.session_store_backend == "opengauss":
        from industrial_ai.storage.opengauss import OpenGaussConversationStore

        assert settings.opengauss_dsn is not None
        return OpenGaussConversationStore(settings.opengauss_dsn.get_secret_value(),
                                          min_size=settings.opengauss_pool_min,
                                          max_size=settings.opengauss_pool_max)
    logger.warning("SESSION_STORE_BACKEND=memory: conversations are lost on restart")
    return InMemoryConversationStore()


def build_llm(settings: Settings) -> LLMClient | None:
    config = LLMConfig.from_settings(settings)
    if config is None:
        return None
    if config.provider == "deepseek":
        logger.warning(
            "LLM_PROVIDER=deepseek: prompts are sent to a public internet API. "
            "Use synthetic or approved desensitised test data only."
        )
    return LLMClient(config)


def build_embedder(settings: Settings) -> EmbeddingClient | None:
    config = EmbeddingConfig.from_settings(settings)
    if config is None:
        return None
    if settings.embedding_provider == "zhipu":
        logger.warning("EMBEDDING_PROVIDER=zhipu: queries are sent to a public internet API.")
    return EmbeddingClient(config)


def build_knowledge_store(settings: Settings) -> MilvusKnowledgeStore | None:
    if not settings.milvus_uri:
        return None
    return MilvusKnowledgeStore(
        settings.milvus_uri, alias=settings.kb_alias,
        token=settings.milvus_token.get_secret_value() if settings.milvus_token else None,
        db_name=settings.milvus_db_name, index_type=settings.kb_index_type)


def model_info(llm: LLMClient) -> ModelInfo:
    c = llm.config
    return ModelInfo(provider=c.provider, name=c.model, precision=c.precision or "unknown",
                     revision=c.revision or "unknown")


def build_container(
    settings: Settings,
    *,
    catalog: EquipmentCatalog | None = None,
    timeseries: TimeSeriesStore | None = None,
    analysis_store: AnalysisStore | None = None,
    conversations: ConversationStore | None = None,
    identity: IdentityProvider | None = None,
    llm: LLMClient | None = None,
    retriever: KnowledgeRetriever | None = None,
) -> Container:
    catalog = catalog or build_catalog(settings)
    timeseries = timeseries or build_timeseries(settings)
    analysis_store = analysis_store or InMemoryAnalysisStore()
    conversations = conversations or build_conversations(settings)
    identity = identity or build_identity_provider(settings)
    runner = AnalysisRunner()
    llm = llm or build_llm(settings)
    rules = RuleConfig(min_completeness=settings.min_data_completeness,
                       consecutive_breach_count=settings.consecutive_breach_count)
    equipment = EquipmentService(catalog, timeseries, rules, settings.health_window_hours)
    maintenance = MaintenanceService(
        catalog, equipment, analysis_store, runner,
        max_range_days=settings.analysis_max_range_days,
        max_equipment=settings.max_equipment_per_analysis,
        timeout_seconds=settings.analysis_timeout_seconds,
        explainer=MaintenanceExplainer(llm) if llm is not None else None,
    )

    embedder = knowledge_store = None
    if retriever is None:
        knowledge_store = build_knowledge_store(settings)
        embedder = build_embedder(settings) if knowledge_store is not None else None
        if knowledge_store is not None and embedder is not None:
            retriever = KnowledgeRetriever(
                knowledge_store, embedder, pinned_version=settings.knowledge_index_version,
                top_k=settings.kb_top_k, min_score=settings.kb_min_score,
                allowed_classifications=settings.kb_allowed_classifications)
        elif knowledge_store is not None:
            logger.warning("MILVUS_URI set but EMBEDDING_PROVIDER=none; knowledge search off")

    agent = None
    if llm is not None:
        tools: list[AgentTool] = [EquipmentHealthTool(equipment),
                                  DeviceReadingsTool(catalog, timeseries, rules)]
        if retriever is not None:
            tools.insert(0, KnowledgeSearchTool(retriever))
        agent = AgentRunner(llm, tools, model_info=model_info(llm),
                            max_steps=settings.assistant_max_tool_steps,
                            tool_result_chars=settings.assistant_tool_result_chars,
                            max_answer_tokens=settings.assistant_max_answer_tokens)
    assistant = AssistantService(conversations, agent,
                                 max_message_chars=settings.assistant_max_message_chars,
                                 history_messages=settings.assistant_history_messages)
    return Container(settings, catalog, timeseries, analysis_store, conversations, identity,
                     runner, equipment, maintenance, assistant, llm=llm, embedder=embedder,
                     knowledge_store=knowledge_store, retriever=retriever)


def get_container(request: Request) -> Container:
    return request.app.state.container
