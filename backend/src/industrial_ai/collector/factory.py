"""Builds a pipeline from settings. This is the only place that knows which source,
parser and writer are in use, so switching simulator -> MQTT is configuration only."""

import logging
from datetime import timedelta

from industrial_ai.collector.parser import ProvisionalJsonCodec, build_parser
from industrial_ai.collector.pipeline import IngestionPipeline
from industrial_ai.collector.simulation import load_scenario
from industrial_ai.collector.sources.base import MessageSource
from industrial_ai.collector.sources.mqtt import MqttConfig, MqttSource
from industrial_ai.collector.sources.simulator import SimulatedSource
from industrial_ai.collector.validator import MeasurementValidator
from industrial_ai.collector.writer import InMemoryWriter, IoTDBWriter, MeasurementWriter
from industrial_ai.core.config import Settings
from industrial_ai.storage.catalog import StaticEquipmentCatalog
from industrial_ai.storage.iotdb_client import pool_factory_from_settings
from industrial_ai.storage.memory import InMemoryTimeSeriesStore

logger = logging.getLogger(__name__)


def mqtt_config(settings: Settings) -> MqttConfig:
    return MqttConfig(
        host=settings.mqtt_host, port=settings.mqtt_port, topics=tuple(settings.mqtt_topics),
        qos=settings.mqtt_qos, client_id=settings.mqtt_client_id,
        username=settings.mqtt_username,
        password=settings.mqtt_password.get_secret_value() if settings.mqtt_password else None,
        keepalive=settings.mqtt_keepalive, tls=settings.mqtt_tls,
        ca_file=str(settings.mqtt_ca_file) if settings.mqtt_ca_file else None,
        persistent_session=settings.mqtt_persistent_session,
    )


def build_simulated_source(settings: Settings) -> SimulatedSource:
    if settings.simulator_scenario_path is None:
        raise ValueError("SIMULATOR_SCENARIO_PATH is required for the simulator")
    scenario = load_scenario(settings.simulator_scenario_path)
    return SimulatedSource(
        scenario.simulation.devices, ProvisionalJsonCodec(settings.collector_topic_prefix),
        backfill=timedelta(hours=settings.simulator_backfill_hours),
        realtime=settings.simulator_realtime, seed=settings.simulator_seed,
    )


def build_source(settings: Settings) -> MessageSource:
    if settings.collector_source == "mqtt":
        return MqttSource(mqtt_config(settings))
    return build_simulated_source(settings)


def build_writer(settings: Settings, memory_store: InMemoryTimeSeriesStore | None = None
                 ) -> MeasurementWriter:
    if settings.timeseries_backend == "memory":
        if memory_store is None:
            raise ValueError("the memory backend only works with the collector embedded in the "
                             "API (API_EMBEDDED_COLLECTOR=true)")
        return InMemoryWriter(memory_store)
    return IoTDBWriter(pool_factory_from_settings(settings), root_path=settings.iotdb_root_path)


def build_validator(settings: Settings) -> MeasurementValidator:
    known = None
    if settings.collector_reject_unknown_equipment:
        if settings.equipment_catalog_path is None:
            logger.warning("EQUIPMENT_CATALOG_PATH not set; accepting measurements for any device")
        else:
            known = StaticEquipmentCatalog.from_file(settings.equipment_catalog_path).keys()
    max_age = (timedelta(hours=settings.collector_max_age_hours)
               if settings.collector_max_age_hours else None)
    return MeasurementValidator(
        known_equipment=known,
        max_future_skew=timedelta(seconds=settings.collector_max_future_skew_seconds),
        max_age=max_age,
    )


def build_pipeline(
    settings: Settings,
    *,
    memory_store: InMemoryTimeSeriesStore | None = None,
    source: MessageSource | None = None,
    writer: MeasurementWriter | None = None,
) -> IngestionPipeline:
    return IngestionPipeline(
        source or build_source(settings),
        build_parser(settings),
        build_validator(settings),
        writer or build_writer(settings, memory_store),
        batch_size=settings.collector_batch_size,
        flush_seconds=settings.collector_flush_seconds,
        queue_size=settings.collector_queue_size,
        max_buffer=settings.collector_max_buffer,
    )
