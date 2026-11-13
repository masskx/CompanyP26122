"""Station directory model, file store and the station REST endpoints."""

import json

import pytest
from pydantic import ValidationError

from app.stations.model import EquipmentType, Metric, Station
from app.stations.store import StationStore

pytestmark = pytest.mark.anyio
BASE = {"station_id": "S1", "name": "s", "sampling_interval_s": 60}


@pytest.mark.parametrize("change", [
    {"sampling_interval_s": 45},                               # 30 s / 60 s / 5 min only
    {"dose_l1": 0.5, "dose_l2": 0.25},                         # L1 must be below L2
    {"battery_warn_v": 11.0, "battery_alarm_v": 11.5},         # alarm below warning
    {"devices": [{"device_id": f"D{i}", "name": "d"} for i in range(21)]},  # max 20
    {"devices": [{"device_id": "D1", "name": "a"}, {"device_id": "D1", "name": "b"}]},
    {"devices": [{"device_id": "S1-BAT-01", "name": "x"}]},    # clashes with the battery id
    {"calibration_factor": 0},
    {"comm_type": "satellite"},
])
def test_station_validation(change):
    with pytest.raises(ValidationError):
        Station.model_validate({**BASE, **change})


def test_equipment_and_series():
    s = Station.model_validate({**BASE, "dose_l1": 0.25, "devices": [
        {"device_id": "S1-DET-01", "name": "a", "calibration_valid_until": "2027-01-31"},
        {"device_id": "S1-DET-02", "name": "b", "enabled": False}]})
    assert not s.dose_configured and not s.battery_configured  # L2 missing
    assert s.equipment_ids() == ["S1-DET-01", "S1-DET-02", "S1-BAT-01", "S1-ENV-01"]
    assert [d.device_id for d in s.active_devices()] == ["S1-DET-01"]
    assert s.equipment_type("S1-BAT-01") is EquipmentType.BACKUP_POWER
    assert s.equipment_type("nope") is None
    assert s.node_for(Metric.BATTERY_VOLTAGE) == "S1-BAT-01"
    assert s.node_for(Metric.RAINFALL) == "S1-ENV-01"
    assert s.node_for(Metric.DOSE_RATE, "S1-DET-02") == "S1-DET-02"
    with pytest.raises(ValueError):
        s.node_for(Metric.DOSE_RATE)
    series = s.series()
    assert ("S1", "S1-DET-02", "dose_rate_std") in series and ("S1", "S1-ENV-01", "door") \
        in series and len(series) == 2 * 5 + 5 + 4


async def test_file_store_reloads(tmp_path, stations):
    path = tmp_path / "stations.json"
    path.write_text(json.dumps({"stations": [BASE]}), encoding="utf-8")
    store = StationStore.from_file(path)
    first = store.version
    assert [s.station_id for s in await store.all()] == ["S1"]
    import os
    path.write_text(json.dumps({"stations": [BASE, {**BASE, "station_id": "S2"}]}),
                    encoding="utf-8")
    os.utime(path, (1, 1))  # a different mtime
    assert [s.station_id for s in await store.all()] == ["S1", "S2"]
    assert store.version != first and await store.get("S2") is not None
    with pytest.raises(FileNotFoundError, match="STATIONS_FILE"):
        StationStore.from_file(tmp_path / "missing.json")


def test_prototype_directory(stations):
    ids = [s.station_id for s in stations]
    assert len(ids) == 18 and "M01" not in ids  # the monitoring vehicle is not a station
    as01 = next(s for s in stations if s.station_id == "AS01")
    assert as01.name == "三废区" and as01.comm_type == "fiber" and as01.has_rain_gauge
    assert next(s for s in stations if s.station_id == "BS11").comm_type == "4g"
    assert not next(s for s in stations if s.station_id == "AS13").dose_configured


async def test_station_endpoints(make_client):
    client, _ = await make_client(None)
    stations = (await client.get("/stations")).json()
    assert len(stations) == 18 and "simulation" not in stations[0]
    assert "simulation" not in stations[0]["devices"][0]
    st = (await client.get("/stations/AS06")).json()
    latest = st["latest"]
    det = latest["devices"][0]
    assert det["dose_rate"]["unit"] == "µGy/h" and det["dose_rate_avg"]["value"] > 0
    assert det["status"]["value"] in ("normal", "h1", "h2", "fault", "offline")
    assert latest["backup_power"]["ac_power"]["value"] == "off"  # AS06 runs on battery
    assert latest["backup_power"]["runtime_hours"]["value"] == 18.0
    assert latest["environment"]["door"]["value"] in ("open", "closed")
    t = (await client.get("/stations/AS06/telemetry",
                          params={"device_id": "AS06-DET-01", "interval": 1800})).json()
    assert len(t["points"]) == 48 and t["unit"] == "µGy/h" and t["calibration_factor"] == 1.0
    v = (await client.get("/stations/AS06/telemetry",
                          params={"metric": "battery_voltage", "interval": 3600})).json()
    assert len(v["points"]) == 24 and v["equipment_id"] == "AS06-BAT-01"
    assert (await client.get("/stations/NOPE")).status_code == 404
    assert (await client.get("/stations/AS06/telemetry",
                             params={"device_id": "AS06-DET-99"})).status_code == 404
    assert (await client.get("/stations/AS06/telemetry",
                             params={"metric": "battery_voltage",
                                     "device_id": "AS06-DET-01"})).status_code == 422
    assert (await client.get("/stations/AS06/telemetry",
                             params={"start": "2026-01-01T00:00:00"})).status_code == 422


async def test_health_and_service_auth(make_client, settings):
    client, _ = await make_client(None)
    h = (await client.get("/health")).json()
    assert h["status"] == "ok" and h["checks"]["llm"]["status"] == "disabled"
    assert h["checks"]["stations"]["count"] == 18 and h["checks"]["work_orders"]["status"] == "ok"
    assert h["checks"]["mqtt_watermark"]["value"] > 0
    from pydantic import SecretStr

    client.resources.settings.compute_service_token = SecretStr("tok")
    client.resources.settings.api_key = SecretStr("k")
    assert (await client.get("/health")).status_code == 401
    assert (await client.get("/health", headers={"Authorization": "Bearer tok"})).status_code \
        == 200
    assert (await client.get("/health", headers={"X-API-Key": "k"})).status_code == 200
    assert (await client.get("/health", headers={"Authorization": "Bearer k"})).status_code \
        == 401
