"""Synchronous maintenance analysis (POST /internal/v1/maintenance/analyses), the work-order
copy and the narrator, on the simulated scenario (data/stations.json, last 24 h)."""

import asyncio
import json
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage

from app.maintenance.narrator import InvalidNarration, parse
from app.maintenance.schemas import AnalysisResult

pytestmark = pytest.mark.anyio


def request(store, site: str, kind: str = "general", **extra) -> dict:
    end = store.now
    return {"analysis_id": f"t-{site}-{kind}", "site_id": site, "analysis_type": kind,
            "observation_window": {"start": (end - timedelta(hours=24)).isoformat(),
                                   "end": end.isoformat()},
            "mqtt_watermark": str(store.watermark), **extra}


async def analyse(client, store, site, kind="general", **extra):
    r = await client.post("/maintenance/analyses", json=request(store, site, kind, **extra))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
async def internal(make_client):
    client, _ = await make_client(None)
    client.base_url = "http://t/internal/v1"
    return client


@pytest.mark.parametrize("site, kind, level, rules", [
    ("AS01", "detector", "watch", ["DETECTOR-CONSECUTIVE-WARNING"] * 2),
    ("AS03", "detector", "inspect", ["DETECTOR-CONSECUTIVE-ALARM",
                                     "DETECTOR-CONSECUTIVE-WARNING"]),
    ("AS06", "general", "inspect", ["BACKUP-RUNTIME-DROP", "BACKUP-ON-BATTERY",
                                    "BACKUP-VOLTAGE-LOW"]),
    ("AS06", "detector", "watch", ["DETECTOR-RAIN-CORRELATION"]),
    ("AS12", "detector", "watch", ["DETECTOR-RAIN-CORRELATION"]),
    ("AS08", "detector", "inspect", ["DETECTOR-DEVICE-FAULT"]),
    ("AS04", "detector", "watch", ["DETECTOR-STUCK-READING"]),
    ("BS11", "backup_power", "watch", ["BACKUP-SLOW-RECHARGE"]),
    ("AS05", "backup_power", "watch", ["BACKUP-VOLTAGE-LOW", "BACKUP-VOLTAGE-DROP-ON-AC"]),
    ("BS13", "detector", "insufficient_data", []),   # no data at all
    ("AS13", "detector", "insufficient_data", []),   # thresholds not configured
    ("BS02", "detector", "normal", []),              # offline 3 h: data limitation only
    ("AS02", "general", "normal", []),
])
async def test_scenario(internal, store, site, kind, level, rules):
    res = await analyse(internal, store, site, kind)
    AnalysisResult.model_validate(res)  # contract invariants (actions, limitations, ...)
    assert res["health_level"] == level
    assert [f["rule_id"] for f in res["findings"]] == rules
    assert res["status"] == "completed" and res["explanation_source"] == "template"
    if level in ("watch", "inspect"):
        assert 1 <= len(res["actions"]) <= 3 and res["actions"][0]["priority"] == "primary"
        assert all(a["requires_human_confirmation"] for a in res["actions"])
    else:
        assert res["actions"] == []
    if level == "insufficient_data":
        assert len(res["limitations"]) == 1 and res["findings"] == []
    assert res["versions"]["rule_set"] == "maintenance-rules-2026.11.1"
    assert res["versions"]["action_catalog"] == "maintenance-actions-2026.11.1"
    assert res["data_snapshot"]["mqtt_watermark"] == str(store.watermark)


async def test_finding_fields(internal, store):
    res = await analyse(internal, store, "AS06", "backup_power")
    f = res["findings"][0]
    assert f == {**f, "rule_id": "BACKUP-RUNTIME-DROP", "code": "backup.runtime_drop",
                 "severity": "inspect", "metric": "runtime_hours", "observed": 18.0,
                 "baseline": 24.0, "change_percent": -25.0}
    assert "/" in f["window"] and f["source_refs"][0].startswith("ts:AS06:AS06-BAT-01:")
    assert [a["action_code"] for a in res["actions"]][0] == "backup.check_charging_path"
    snap = res["data_snapshot"]
    assert snap["sample_count"] == 1440 and snap["completeness"] == 1.0
    assert snap["missing_rate"] == 0.0 and snap["data_as_of"]


async def test_limitations(internal, store):
    res = await analyse(internal, store, "BS02", "detector")
    assert res["limitations"][0]["code"] == "detector.offline"
    assert "离线" in res["limitations"][0]["text"]
    res = await analyse(internal, store, "AS11", "detector")
    assert res["limitations"][0]["code"] == "detector.calibration_expired"


async def test_idempotency(internal, store):
    body = request(store, "AS03", "detector")
    headers = {"Idempotency-Key": "k-1"}
    a, b = await asyncio.gather(*(internal.post("/maintenance/analyses", json=body,
                                                headers=headers) for _ in range(2)))
    assert a.json()["generated_at"] == b.json()["generated_at"]  # one execution
    other = {**body, "analysis_type": "general"}
    r = await internal.post("/maintenance/analyses", json=other, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "idempotency_conflict"


async def test_watermark_wait(internal, store):
    r = await internal.post("/maintenance/analyses", json=request(
        store, "AS03", mqtt_watermark=str(store.watermark + 1000)))
    body = r.json()
    assert r.status_code == 503 and body["code"] == "watermark_not_reached"
    assert body["retryable"] and body["local_watermark"] == store.watermark
    # without a watermark the analysis runs on what is there (development)
    r = await internal.post("/maintenance/analyses", json={
        **request(store, "AS03"), "mqtt_watermark": None, "analysis_id": "nowm"})
    assert r.status_code == 200


@pytest.mark.parametrize("change, status, code", [
    ({"site_id": "XX99"}, 404, "site_not_found"),
    ({"equipment_ids": ["AS03-DET-99"]}, 404, "equipment_not_found"),
])
async def test_errors(internal, store, change, status, code):
    r = await internal.post("/maintenance/analyses", json={**request(store, "AS03"), **change})
    assert r.status_code == status and r.json()["code"] == code


async def test_request_validation(internal, store):
    bad = request(store, "AS03")
    bad["observation_window"]["start"] = bad["observation_window"]["end"]
    assert (await internal.post("/maintenance/analyses", json=bad)).status_code == 422
    bad = {**request(store, "AS03"), "mqtt_watermark": "abc"}
    assert (await internal.post("/maintenance/analyses", json=bad)).status_code == 422


async def test_work_order_copy_and_history_rule(internal, store):
    end = store.now
    for i, outcome in enumerate(["accurate", "needs_revision"]):
        wo = {"event_id": f"me-{i}", "site_id": "AS03", "equipment_id": "AS03-DET-01",
              "risk_type": "detector", "health_level": "watch", "status": "confirmed",
              "triggered_at": (end - timedelta(days=10 + i)).isoformat(),
              "ai_result": {"explanation": "x"},
              "review": {"outcome": outcome, "comment": "c",
                         "reviewed_at": (end - timedelta(days=9)).isoformat()},
              "updated_at": (end - timedelta(days=9)).isoformat()}
        r = await internal.put(f"/maintenance/work-orders/me-{i}", json=wo)
        assert r.json() == {"event_id": f"me-{i}", "result": "created"}
    stale = {**wo, "status": "pending", "updated_at": (end - timedelta(days=20)).isoformat()}
    assert (await internal.put("/maintenance/work-orders/me-1", json=stale)).json()["result"] \
        == "stale"
    assert (await internal.put("/maintenance/work-orders/other", json=wo)).status_code == 422
    got = (await internal.get("/maintenance/work-orders/me-1")).json()
    assert got["status"] == "confirmed" and got["review"]["outcome"] == "needs_revision"
    listed = (await internal.get("/maintenance/work-orders", params={"site_id": "AS03"})).json()
    assert [w["event_id"] for w in listed["items"]] == ["me-0", "me-1"]
    res = await analyse(internal, store, "AS03", "detector", analysis_id="hist")
    hist = [f for f in res["findings"] if f["rule_id"] == "HISTORY-REPEAT"]
    # ranked after the two threshold findings, so only visible when <= 3 findings exist
    assert len(res["findings"]) == 3 and hist and hist[0]["observed"] == 2.0
    assert set(hist[0]["source_refs"]) == {"wo:me-0", "wo:me-1"}


async def test_narrator_model_chooses_primary(make_client, store):
    facts_ok = {"explanation": "AS06 备电参考续航 18.0 小时，较基线 24.0 小时下降约 25%，"
                               "外部供电已中断约 5.0 小时，应先核查供电恢复情况。",
                "primary_action": "backup.check_mains_supply"}
    client, llm = await make_client([
        AIMessage(content=json.dumps({**facts_ok, "explanation": "续航下降 33%"},
                                     ensure_ascii=False)),       # unsupported number -> retry
        AIMessage(content=json.dumps(facts_ok, ensure_ascii=False))])
    client.base_url = "http://t/internal/v1"
    res = await analyse(client, store, "AS06", "backup_power", analysis_id="narr")
    assert res["explanation_source"] == "model" and res["explanation"] == facts_ok["explanation"]
    assert [a["action_code"] for a in res["actions"]][0] == "backup.check_mains_supply"
    assert [a["priority"] for a in res["actions"]] == ["primary", "secondary", "secondary"]
    assert "数值 33 无事实支持" in llm.calls[1]["messages"][-1].content
    assert '"candidate_actions"' in llm.calls[0]["messages"][1].content


async def test_narrator_fallback_and_insufficient_data(make_client, store):
    client, llm = await make_client([AIMessage(content="不是 JSON"),
                                     AIMessage(content='{"explanation": "x", '
                                                       '"primary_action": "made.up"}')])
    client.base_url = "http://t/internal/v1"
    res = await analyse(client, store, "AS08", "detector", analysis_id="fb")
    assert res["explanation_source"] == "template"
    assert res["actions"][0]["action_code"] == "detector.fault_diagnosis"  # catalogue order
    calls = len(llm.calls)
    res = await analyse(client, store, "BS13", "detector", analysis_id="ins")
    assert res["health_level"] == "insufficient_data" and len(llm.calls) == calls  # no model


def test_narration_validation():
    evidence = {"findings": [{"observed": 0.482, "window": "2026-11-13T03:17:00+08:00/..."}]}
    ok = '{"explanation": "最高 0.48 µGy/h，03:17 起", "primary_action": "a"}'
    assert parse(ok, evidence, ["a"], 100)[1] == "a"
    for raw, why in [('{"explanation": "最高 0.5", "primary_action": "a"}', "数值"),
                     ('{"explanation": "05:00 起", "primary_action": "a"}', "时间"),
                     ('{"explanation": "ok", "primary_action": "b"}', "primary_action"),
                     ('{"explanation": "' + "长" * 101 + '", "primary_action": "a"}', "超过"),
                     ("[]", "explanation")]:
        with pytest.raises(InvalidNarration, match=why):
            parse(raw, evidence, ["a"], 100)
