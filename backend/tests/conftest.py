from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from industrial_ai.api.deps import Container, build_container
from industrial_ai.api.main import create_app
from industrial_ai.core.config import Settings
from industrial_ai.schemas.common import TelemetryPoint
from industrial_ai.schemas.equipment import Equipment, MetricDefinition, MetricThresholds
from industrial_ai.storage.catalog import StaticEquipmentCatalog
from industrial_ai.storage.memory import InMemoryTimeSeriesStore

TOKEN = "test-service-token"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-User-Id": "u-1", "X-User-Roles": "operator"}

# Synthetic thresholds for tests only; they are not plant values.
TEST_THRESHOLDS = MetricThresholds(warn_high=50.0, alarm_high=80.0)


def make_points(start: datetime, values: list[float | None], step_s: int = 60
                ) -> list[TelemetryPoint]:
    return [TelemetryPoint(timestamp=start + timedelta(seconds=i * step_s), value=v)
            for i, v in enumerate(values)]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, compute_service_token=SecretStr(TOKEN),
                    sse_heartbeat_seconds=0.05, consecutive_breach_count=3)


@pytest.fixture
def catalog() -> StaticEquipmentCatalog:
    return StaticEquipmentCatalog([
        Equipment(equipment_id="ROOM-OK", site_id="S1", name="room ok", category="isolation_room",
                  metrics=[MetricDefinition(name="m1", thresholds=TEST_THRESHOLDS)]),
        Equipment(equipment_id="ROOM-HOT", site_id="S1", name="room hot",
                  category="isolation_room",
                  metrics=[MetricDefinition(name="m1", thresholds=TEST_THRESHOLDS)]),
        Equipment(equipment_id="ROOM-NOLIMIT", site_id="S1", name="room no limit",
                  category="isolation_room",
                  metrics=[MetricDefinition(name="radiation_dose_rate", thresholds=None)]),
        Equipment(equipment_id="BKP-1", site_id="S1", name="backup", category="backup_power",
                  metrics=[MetricDefinition(name="m1", thresholds=TEST_THRESHOLDS)]),
    ])


@pytest.fixture
def timeseries() -> InMemoryTimeSeriesStore:
    store = InMemoryTimeSeriesStore()
    start = datetime.now(UTC) - timedelta(hours=1)
    store.insert("S1", "ROOM-OK", "m1", make_points(start, [10, 12, 11, 13]))
    store.insert("S1", "ROOM-HOT", "m1", make_points(start, [10, 85, 90, 95, 20]))
    store.insert("S1", "ROOM-NOLIMIT", "radiation_dose_rate", make_points(start, [1.0, 1.1]))
    # BKP-1 has no data at all.
    return store


@pytest.fixture
def container(settings, catalog, timeseries) -> Container:
    return build_container(settings, catalog=catalog, timeseries=timeseries)


@pytest.fixture
async def client(container) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container=container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await container.aclose()
