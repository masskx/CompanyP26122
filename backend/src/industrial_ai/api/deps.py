"""Dependency container: wires settings to concrete store implementations and services."""

import logging
from dataclasses import dataclass

from fastapi import Request

from industrial_ai.analytics.rules import RuleConfig
from industrial_ai.core.config import Settings
from industrial_ai.services.assistant_service import AssistantService
from industrial_ai.services.equipment_service import EquipmentService
from industrial_ai.services.maintenance_service import MaintenanceService
from industrial_ai.storage.base import (
    AnalysisStore,
    EquipmentCatalog,
    SessionStore,
    TimeSeriesStore,
)
from industrial_ai.storage.catalog import StaticEquipmentCatalog
from industrial_ai.storage.memory import (
    InMemoryAnalysisStore,
    InMemorySessionStore,
    InMemoryTimeSeriesStore,
)
from industrial_ai.workers.analysis_worker import AnalysisRunner

logger = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    catalog: EquipmentCatalog
    timeseries: TimeSeriesStore
    analysis_store: AnalysisStore
    session_store: SessionStore
    runner: AnalysisRunner
    equipment: EquipmentService
    maintenance: MaintenanceService
    assistant: AssistantService

    async def aclose(self) -> None:
        await self.runner.shutdown()
        await self.timeseries.close()


def build_timeseries(settings: Settings) -> TimeSeriesStore:
    if settings.timeseries_backend == "iotdb":
        from industrial_ai.storage.iotdb_client import IoTDBTimeSeriesStore

        password = settings.iotdb_password.get_secret_value() if settings.iotdb_password else None
        return IoTDBTimeSeriesStore.from_settings(
            host=settings.iotdb_host, port=settings.iotdb_port, user=settings.iotdb_user,
            password=password, root_path=settings.iotdb_root_path,
            pool_size=settings.iotdb_pool_size, wait_timeout_ms=settings.iotdb_wait_timeout_ms,
        )
    return InMemoryTimeSeriesStore()


def build_catalog(settings: Settings) -> EquipmentCatalog:
    if settings.equipment_catalog_path is None:
        logger.warning("EQUIPMENT_CATALOG_PATH not set; equipment catalog is empty")
        return StaticEquipmentCatalog([])
    return StaticEquipmentCatalog.from_file(settings.equipment_catalog_path)


def build_container(
    settings: Settings,
    *,
    catalog: EquipmentCatalog | None = None,
    timeseries: TimeSeriesStore | None = None,
    analysis_store: AnalysisStore | None = None,
    session_store: SessionStore | None = None,
) -> Container:
    catalog = catalog or build_catalog(settings)
    timeseries = timeseries or build_timeseries(settings)
    analysis_store = analysis_store or InMemoryAnalysisStore()
    session_store = session_store or InMemorySessionStore()
    runner = AnalysisRunner()
    rules = RuleConfig(min_completeness=settings.min_data_completeness,
                       consecutive_breach_count=settings.consecutive_breach_count)
    equipment = EquipmentService(catalog, timeseries, rules, settings.health_window_hours)
    maintenance = MaintenanceService(
        catalog, equipment, analysis_store, runner,
        max_range_days=settings.analysis_max_range_days,
        max_equipment=settings.max_equipment_per_analysis,
        timeout_seconds=settings.analysis_timeout_seconds,
    )
    assistant = AssistantService(
        session_store, max_message_chars=settings.assistant_max_message_chars,
        knowledge_index_version=settings.knowledge_index_version,
    )
    return Container(settings, catalog, timeseries, analysis_store, session_store, runner,
                     equipment, maintenance, assistant)


def get_container(request: Request) -> Container:
    return request.app.state.container
