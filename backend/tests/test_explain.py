"""LLM explanations: evidence-only input, strict validation, one retry, template fallback,
code-computed confidence."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage

from app.analysis.analyzer import Params, StationAnalyzer
from app.analysis.explain import Explainer, InvalidOutput, confidence, evidence_summary, validate

from .conftest import FakeChatModel

pytestmark = pytest.mark.anyio
TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
async def records(store, stations):
    day = datetime.now(UTC).astimezone(TZ).date() - timedelta(days=1)
    start = datetime.combine(day, datetime.min.time(), TZ).astimezone(UTC)
    st01 = next(s for s in stations if s.station_id == "ST01")
    res = await StationAnalyzer(store, Params(), TZ).analyze(st01, "b", day, start,
                                                             start + timedelta(days=1))
    return {r.device_id: r for r in res.records}


def good(record) -> dict:
    e1, e2 = record.events
    return {"summary": f"两次剂量率连续超一级阈值，最大 {e1.max} µGy/h。",
            "details": f"E1 自 {e1.start.astimezone(TZ):%H:%M} 起持续 "
                       f"{round(e1.duration_s / 60, 1)} 分钟，均值 {round(e1.mean, 3)} µGy/h；"
                       f"E2 最大 {e2.max} µGy/h。",
            "evidence": [{"event_ids": ["E1"], "observation": "同期有降雨。"},
                         {"event_ids": ["E2"], "observation": "设备未上报报警。"}],
            "recommendations": [{"priority": "medium", "action": "核查降雨影响",
                                 "rationale": "E1 与降雨重合", "event_ids": ["E1"]}],
            "uncertainties": ["同比/环比不可用"]}


def reply(obj) -> AIMessage:
    return AIMessage(content=obj if isinstance(obj, str) else json.dumps(obj,
                                                                        ensure_ascii=False))


async def test_evidence_summary_has_no_raw_curve(records):
    ev = evidence_summary(records["D02"], TZ)
    text = json.dumps(ev, ensure_ascii=False)
    assert [e["event_id"] for e in ev["events"]] == ["E1", "E2"]
    assert "values" not in text and len(text) < 8000
    e1 = ev["events"][0]
    assert {"start", "end", "duration_minutes", "mean", "std", "rule", "comparisons",
            "context", "completeness_pct"} <= set(e1)
    assert e1["comparisons"]["previous_year_same_day"]["status"] == "unavailable"


async def test_validation(records):
    rec = records["D02"]
    ev = evidence_summary(rec, TZ)
    out = good(rec)
    assert validate(json.dumps(out, ensure_ascii=False), rec, ev).summary
    assert validate("```json\n" + json.dumps(out) + "\n```", rec, ev)  # fenced is fine

    def bad(**change) -> str:
        return json.dumps({**out, **change}, ensure_ascii=False)

    cases = {
        "not json": "这不是 JSON",
        "unknown event": bad(evidence=[{"event_ids": ["E1", "E9"], "observation": "x"}]),
        "uncovered": bad(evidence=[{"event_ids": ["E1"], "observation": "x"}]),
        "number": bad(summary="最大 0.97 µGy/h"),
        "one-digit rounding": bad(summary=f"最大约 {round(rec.events[0].max, 1)} µGy/h"),
        "time": bad(details="E1 自 05:17 起"),
        "date": bad(details="2020-01-01 出现异常"),
        "code in text": bad(details="E7 异常"),
    }
    for name, raw in cases.items():
        with pytest.raises(InvalidOutput):
            validate(raw, rec, ev)
            pytest.fail(name)
    # rounding that keeps two significant digits is accepted
    assert validate(bad(summary=f"最大约 {round(rec.events[0].max, 2)} µGy/h"), rec, ev)
    # a negative value in the evidence supports its magnitude ("下降 0.62")
    rec.events[0].comparisons["previous_day"] = rec.events[0].comparisons["previous_day"] \
        .model_copy(update={"status": "available", "mean": 0.1, "delta_mean": -0.6224})
    ev = evidence_summary(rec, TZ)
    assert validate(bad(summary="较前一日下降 0.6224 µGy/h"), rec, ev)
    with pytest.raises(InvalidOutput):  # digits inside identifiers are not evidence
        validate(bad(summary="数值 108"), rec, {**ev, "id": "B20261108-v1"})


async def test_llm_explanation_with_retry(records):
    rec = records["D02"]
    llm = FakeChatModel(responses=[reply({**good(rec), "summary": "最大 9.99 µGy/h"}),
                                   reply(good(rec))], calls=[])
    ex = await Explainer(llm, "vllm:qwen3", TZ).explain(rec)
    assert ex.method == "llm" and ex.model == "vllm:qwen3" and ex.error is None
    assert len(llm.calls) == 2
    retry_prompt = llm.calls[1]["messages"][-1].content
    assert "数值 9.99 无证据支持" in retry_prompt
    first_prompt = llm.calls[0]["messages"][1].content
    assert "E1" in first_prompt and '"values"' not in first_prompt
    item = ex.evidence[0]
    assert item.metric == "γ 剂量率" and item.source == "规则计算" and "–" in item.time_range
    assert ex.recommendations[0].requires_human_confirmation


async def test_fallback_after_two_invalid_outputs(records):
    rec = records["D02"]
    llm = FakeChatModel(responses=[reply("胡言乱语"), reply({**good(rec), "summary": "8.88"})],
                        calls=[])
    ex = await Explainer(llm, "m", TZ).explain(rec)
    assert ex.method == "template_fallback" and "8.88" in ex.error
    assert ex.summary.startswith("1号固定监测站") and len(ex.evidence) == 2


async def test_model_error_falls_back(records):
    class Broken(FakeChatModel):
        def _generate(self, *a, **k):
            raise TimeoutError("timed out")

    ex = await Explainer(Broken(responses=[], calls=[]), "m", TZ).explain(records["D03"])
    assert ex.method == "template_fallback" and "模型调用失败" in ex.error


async def test_missing_data_records_use_the_template_without_the_model(records):
    llm = FakeChatModel(responses=[], calls=[])
    ex = await Explainer(llm, "m", TZ).explain(records["D05"])
    assert ex.method == "template" and llm.calls == []
    assert "期间无可用样本" in ex.details
    assert {r.priority for r in ex.recommendations} <= {"medium", "low"}


async def test_confidence_is_computed_by_code(records):
    c2, f2 = confidence(records["D02"])   # one event not reported by the device
    c3, f3 = confidence(records["D03"])   # both events corroborated by the device
    assert set(f2) == {"thresholds_configured", "completeness", "baseline_available",
                       "event_sources", "weights"}
    assert f2["baseline_available"] == 0.0  # no history in the 48 h simulation
    assert f3["event_sources"] == 1.0 and f2["event_sources"] == 0.85
    assert 0 < c2 < c3 <= 1
    battery = records["BATTERY"]
    assert confidence(battery)[1]["event_sources"] == 0.7
