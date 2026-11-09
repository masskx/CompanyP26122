"""Station configuration model, store and the station REST endpoints."""

import pytest
from pydantic import ValidationError

from app.stations.model import Station
from app.stations.store import MemoryStationStore

pytestmark = pytest.mark.anyio
BASE = {"station_id": "S1", "name": "s", "sampling_interval_s": 60}


@pytest.mark.parametrize("change", [
    {"sampling_interval_s": 45},                               # 30 s / 60 s / 5 min only
    {"dose_l1": 0.5, "dose_l2": 0.25},                         # L1 must be below L2
    {"battery_warn_v": 11.0, "battery_alarm_v": 11.5},         # alarm below warning
    {"devices": [{"device_id": f"D{i}", "name": "d"} for i in range(21)]},  # max 20
    {"devices": [{"device_id": "D1", "name": "a"}, {"device_id": "D1", "name": "b"}]},
    {"devices": [{"device_id": "_station", "name": "x"}]},     # reserved node
    {"calibration_factor": 0},
])
def test_station_validation(change):
    with pytest.raises(ValidationError):
        Station.model_validate({**BASE, **change})


def test_station_flags_and_series():
    s = Station.model_validate({**BASE, "dose_l1": 0.25, "devices": [
        {"device_id": "D1", "name": "a"}, {"device_id": "D2", "name": "b", "enabled": False}]})
    assert not s.dose_configured and not s.battery_configured  # L2 missing
    assert [d.device_id for d in s.active_devices()] == ["D1"]
    assert ("S1", "D2", "dose_rate") in s.series() and ("S1", "_station", "door") in s.series()


async def test_store_versions(stations):
    store = MemoryStationStore(stations)
    assert (await store.versions())["ST01"] == 1
    changed = stations[0].model_copy(update={"dose_l1": 0.3})
    assert await store.upsert([changed], overwrite=False) == {"ST01": "kept"}
    assert await store.upsert([changed]) == {"ST01": "updated"}
    assert await store.upsert([changed]) == {"ST01": "unchanged"}
    assert (await store.versions())["ST01"] == 2 and (await store.get("ST01")).dose_l1 == 0.3


async def test_station_endpoints(make_client):
    client, _ = await make_client(None)
    stations = (await client.get("/stations")).json()
    assert [s["station_id"] for s in stations] == ["ST01", "ST02", "ST03"]
    assert "simulation" not in stations[0] and "simulation" not in stations[0]["devices"][0]
    assert [s["dose_configured"] for s in stations] == [True, True, False]
    st01 = (await client.get("/stations/ST01")).json()
    latest = st01["latest"]
    assert latest["devices"][0]["dose_rate"]["unit"] == "µGy/h"
    assert latest["devices"][0]["status"]["value"] in ("normal", "h1", "h2", "fault", "offline")
    assert latest["station"]["ac_power"]["value"] in ("on", "off")
    assert latest["station"]["battery_voltage"]["unit"] == "V"
    t = (await client.get("/stations/ST01/telemetry",
                          params={"device_id": "D01", "interval": 1800})).json()
    assert len(t["points"]) == 48 and t["unit"] == "µGy/h" and t["calibration_factor"] == 1.0
    v = (await client.get("/stations/ST01/telemetry",
                          params={"metric": "battery_voltage", "interval": 3600})).json()
    assert len(v["points"]) == 24
    assert (await client.get("/stations/NOPE")).status_code == 404
    assert (await client.get("/stations/ST01/telemetry",
                             params={"device_id": "D99"})).status_code == 404
    assert (await client.get("/stations/ST01/telemetry",
                             params={"metric": "battery_voltage",
                                     "device_id": "D01"})).status_code == 422
    assert (await client.get("/stations/ST01/telemetry",
                             params={"start": "2026-01-01T00:00:00"})).status_code == 422


async def test_health_and_api_key(make_client, settings):
    client, _ = await make_client(None)
    h = (await client.get("/health")).json()
    assert h["status"] == "ok" and h["checks"]["llm"]["status"] == "disabled"
    assert h["checks"]["stations"]["count"] == 3 and h["checks"]["analysis"]["status"] == "ok"
    from pydantic import SecretStr

    client.resources.settings.api_key = SecretStr("k")
    assert (await client.get("/health")).status_code == 401
    assert (await client.get("/health", headers={"X-API-Key": "k"})).status_code == 200
