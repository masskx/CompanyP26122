"""Rules, device service and device REST endpoints on simulated data."""

from datetime import UTC, datetime, timedelta

import pytest

from app.timeseries.devices import Level, Metric, assess_metric
from app.timeseries.iotdb import Point

pytestmark = pytest.mark.anyio
START = datetime(2026, 11, 5, tzinfo=UTC)
END = START + timedelta(hours=1)
M = Metric(warn_high=50, alarm_high=80)


def pts(values, step=60):
    return [Point(START + timedelta(seconds=i * step), v) for i, v in enumerate(values)]


@pytest.mark.parametrize("metric, values, level", [
    (M, [], Level.INSUFFICIENT_DATA),
    (Metric(), [1, 2, 3], Level.INSUFFICIENT_DATA),             # no thresholds: never guess
    (M, [10, 20, 30], Level.NORMAL),
    (M, [10, 81, 82, 83, 10], Level.INSPECT),
    (M, [10, 90, 10], Level.WATCH),
    (M, [55, 60, 65], Level.WATCH),
    (M, [90, None, 90, 90], Level.INSPECT),                    # gaps do not break a run
    (Metric(warn_high=50, alarm_high=80, interval_seconds=60), [10, 10], Level.INSUFFICIENT_DATA),
])
def test_rules(metric, values, level):
    assert assess_metric(metric, pts(values), START, END).level is level


async def test_overview_matches_scenario(make_client):
    client, _ = await make_client(None)
    body = (await client.get("/overview", params={"site": "AS01"})).json()
    levels = {d["device_id"]: d["level"] for d in body["devices"]}
    assert levels == {"AS01-ISO-RM-01": "normal", "AS01-ISO-RM-02": "inspect",
                      "AS01-ISO-RM-03": "insufficient_data", "AS01-ISO-RM-04": "watch",
                      "AS01-ISO-RM-05": "normal", "AS01-ENV-01": "normal",
                      "AS01-ENV-02": "normal"}


async def test_device_endpoints(make_client):
    client, _ = await make_client(None)
    devices = (await client.get("/devices", params={"site": "AS01"})).json()
    assert len(devices) == 7 and "simulation" not in devices[0]
    assert devices[0]["latest"]["dose_rate"]["unit"] == "µSv/h"
    a = (await client.get("/devices/AS01-ISO-RM-02/assessment", params={"hours": 6})).json()
    assert a["level"] == "inspect" and a["metrics"][0]["max"] > 12
    t = (await client.get("/devices/AS01-ISO-RM-02/telemetry",
                          params={"interval": 1800})).json()
    assert len(t["points"]) == 12 and {"avg", "min", "max", "count"} <= set(t["points"][0])
    raw = (await client.get("/devices/AS01-ENV-01/telemetry")).json()
    assert 300 <= len(raw["points"]) <= 361
    assert (await client.get("/devices/NOPE")).status_code == 404
    assert (await client.get("/devices/AS01-ENV-01/telemetry",
                             params={"start": "2026-01-01T00:00:00"})).status_code == 422


async def test_health_and_api_key(make_client, settings):
    client, _ = await make_client(None)
    h = (await client.get("/health")).json()
    assert h["status"] == "ok" and h["checks"]["llm"]["status"] == "disabled"
    from pydantic import SecretStr

    settings.api_key = SecretStr("k")
    assert (await client.get("/health")).status_code == 401
    assert (await client.get("/health", headers={"X-API-Key": "k"})).status_code == 200
