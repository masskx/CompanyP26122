import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from industrial_ai.api.deps import build_container
from industrial_ai.api.main import create_app

from .conftest import AUTH

pytestmark = pytest.mark.anyio


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = [ln for ln in block.splitlines() if not ln.startswith(":")]
        if not lines:
            continue
        fields = dict(ln.split(": ", 1) for ln in lines)
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def analysis_body(equipment_ids, analysis_type="isolation_room"):
    end = datetime.now(UTC)
    return {
        "site_id": "S1",
        "equipment_ids": equipment_ids,
        "analysis_type": analysis_type,
        "time_range": {"start": (end - timedelta(hours=2)).isoformat(), "end": end.isoformat()},
    }


async def run_analysis(client, body, **headers):
    resp = await client.post("/internal/v1/maintenance/analyses", json=body,
                             headers={**AUTH, **headers})
    assert resp.status_code == 202, resp.text
    analysis_id = resp.json()["analysis_id"]
    stream = await client.get(f"/internal/v1/maintenance/analyses/{analysis_id}/events",
                              headers=AUTH)
    return analysis_id, parse_sse(stream.text)


# ---- auth / plumbing ---------------------------------------------------------------------

async def test_live_is_open_and_echoes_request_id(client):
    resp = await client.get("/health/live", headers={"X-Request-Id": "abc"})
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == "abc"


async def test_ready_reports_components(client):
    body = (await client.get("/health/ready")).json()
    assert body["status"] == "ready"
    assert body["components"]["timeseries"]["backend"] == "memory"
    assert body["components"]["knowledge_base"]["status"] == "not_configured"
    assert body["components"]["session_store"]["status"] == "ok"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"},
                                     {"Authorization": "Basic x"}])
async def test_business_endpoints_require_token(client, headers):
    resp = await client.get("/internal/v1/equipment/health?site_id=S1", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_fails_closed_when_token_not_configured(settings, catalog, timeseries):
    import httpx

    settings = settings.model_copy(update={"compute_service_token": SecretStr("")})
    app = create_app(container=build_container(settings, catalog=catalog, timeseries=timeseries))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        resp = await c.get("/internal/v1/equipment/health?site_id=S1", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "auth_not_configured"


async def test_validation_error_shape(client):
    resp = await client.get("/internal/v1/equipment/health?site_id=bad%20id", headers=AUTH)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


# ---- equipment health --------------------------------------------------------------------

async def test_health_overview(client):
    resp = await client.get("/internal/v1/equipment/health",
                            params={"site_id": "S1", "category": "isolation_room"}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    levels = {i["equipment_id"]: i["health_level"] for i in body["items"]}
    assert levels == {"ROOM-OK": "normal", "ROOM-HOT": "inspect",
                      "ROOM-NOLIMIT": "insufficient_data"}
    assert body["counts"] == {"normal": 1, "watch": 0, "inspect": 1, "insufficient_data": 1}


# ---- maintenance analyses ----------------------------------------------------------------

async def test_analysis_lifecycle(client):
    analysis_id, events = await run_analysis(client, analysis_body(["ROOM-HOT", "ROOM-OK"]))
    kinds = [k for k, _ in events]
    assert kinds[0] == "stage" and kinds[-2:] == ["result", "done"]
    seqs = [d["seq"] for _, d in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    result = (await client.get(f"/internal/v1/maintenance/analyses/{analysis_id}",
                               headers=AUTH)).json()
    assert result["status"] == "completed"
    assert result["health_level"] == "inspect"
    assert result["model"] is None
    assert all(r["requires_human_confirmation"] for r in result["recommendations"])
    assert {r["equipment_id"] for r in result["recommendations"]} == {"ROOM-HOT"}
    streamed = {k: v for k, v in events[-2][1].items() if k != "seq"}
    assert streamed == result  # the persisted result matches what was streamed


async def test_missing_threshold_gives_insufficient_data(client):
    analysis_id, _ = await run_analysis(client, analysis_body(["ROOM-NOLIMIT"]))
    result = (await client.get(f"/internal/v1/maintenance/analyses/{analysis_id}",
                               headers=AUTH)).json()
    assert result["status"] == "partial"
    assert result["health_level"] == "insufficient_data"
    assert result["confidence"] is None
    assert result["data_gaps"][0]["reason"] == "threshold_not_configured"


async def test_no_data_backup_power(client):
    analysis_id, _ = await run_analysis(client, analysis_body(["BKP-1"], "backup_power"))
    result = (await client.get(f"/internal/v1/maintenance/analyses/{analysis_id}",
                               headers=AUTH)).json()
    assert result["health_level"] == "insufficient_data"
    assert result["data_gaps"][0]["reason"] == "no_data"


async def test_resume_with_last_event_id(client):
    analysis_id, events = await run_analysis(client, analysis_body(["ROOM-OK"]))
    cut = events[1][1]["seq"]
    resp = await client.get(f"/internal/v1/maintenance/analyses/{analysis_id}/events",
                            headers={**AUTH, "Last-Event-ID": str(cut)})
    resumed = parse_sse(resp.text)
    assert [d["seq"] for _, d in resumed] == [d["seq"] for _, d in events if d["seq"] > cut]

    # Resuming after `done` ends immediately instead of hanging.
    last = events[-1][1]["seq"]
    resp = await client.get(f"/internal/v1/maintenance/analyses/{analysis_id}/events",
                            params={"after_seq": last}, headers=AUTH)
    assert parse_sse(resp.text) == []


async def test_idempotency(client):
    body = analysis_body(["ROOM-OK"])
    headers = {**AUTH, "Idempotency-Key": "k-1"}
    first = await client.post("/internal/v1/maintenance/analyses", json=body, headers=headers)
    again = await client.post("/internal/v1/maintenance/analyses", json=body, headers=headers)
    assert first.json()["analysis_id"] == again.json()["analysis_id"]

    other = analysis_body(["ROOM-HOT"])
    conflict = await client.post("/internal/v1/maintenance/analyses", json=other, headers=headers)
    assert conflict.status_code == 409


@pytest.mark.parametrize("body, status, code", [
    (analysis_body(["NOPE"]), 404, "equipment_not_found"),
    (analysis_body(["BKP-1"], "isolation_room"), 422, "equipment_type_mismatch"),
    (analysis_body(["ROOM-OK", "ROOM-OK"]), 422, "duplicate_equipment"),
])
async def test_create_rejections(client, body, status, code):
    resp = await client.post("/internal/v1/maintenance/analyses", json=body, headers=AUTH)
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code


async def test_time_range_validation(client):
    body = analysis_body(["ROOM-OK"])
    body["time_range"]["start"], body["time_range"]["end"] = (body["time_range"]["end"],
                                                              body["time_range"]["start"])
    resp = await client.post("/internal/v1/maintenance/analyses", json=body, headers=AUTH)
    assert resp.status_code == 422

    body = analysis_body(["ROOM-OK"])
    body["time_range"]["start"] = "2020-01-01T00:00:00+08:00"
    resp = await client.post("/internal/v1/maintenance/analyses", json=body, headers=AUTH)
    assert resp.json()["error"]["code"] == "time_range_too_large"

    body = analysis_body(["ROOM-OK"])
    body["time_range"]["start"] = "2026-10-01T00:00:00"  # naive datetime
    resp = await client.post("/internal/v1/maintenance/analyses", json=body, headers=AUTH)
    assert resp.status_code == 422


async def test_unknown_analysis_is_404(client):
    resp = await client.get(f"/internal/v1/maintenance/analyses/ma_{'0' * 32}", headers=AUTH)
    assert resp.status_code == 404


async def test_storage_failure_marks_analysis_failed(client, timeseries, monkeypatch):
    from industrial_ai.storage.base import StorageUnavailableError

    async def boom(*_args, **_kwargs):
        raise StorageUnavailableError("down")

    monkeypatch.setattr(timeseries, "query", boom)
    analysis_id, events = await run_analysis(client, analysis_body(["ROOM-OK"]))
    assert events[-3][0] == "error"
    assert events[-3][1]["code"] == "timeseries_unavailable"
    result = (await client.get(f"/internal/v1/maintenance/analyses/{analysis_id}",
                               headers=AUTH)).json()
    assert result["status"] == "failed"
