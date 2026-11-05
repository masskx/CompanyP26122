"""LLM client / explainer tests against a mocked OpenAI-compatible endpoint (no network)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from industrial_ai.api.deps import build_container
from industrial_ai.api.main import create_app
from industrial_ai.core.config import Settings
from industrial_ai.integrations.llm_client import LLMClient, LLMConfig, LLMError, clean_content
from industrial_ai.schemas.analysis import (
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    Evidence,
    Recommendation,
)
from industrial_ai.schemas.common import HealthLevel, TimeRange
from industrial_ai.services.maintenance_explainer import MaintenanceExplainer

from .conftest import AUTH

pytestmark = pytest.mark.anyio

DEEPSEEK = LLMConfig(provider="deepseek", base_url="https://api.deepseek.com",
                     model="deepseek-chat", api_key="sk-test", retry_backoff_seconds=0)
VLLM = LLMConfig(provider="vllm", base_url="http://127.0.0.1:8000/v1", model="qwen3-30b-a3b",
                 precision="W8A8", retry_backoff_seconds=0)


def completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "model": "served-model",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })


class Recorder:
    """MockTransport handler that replays queued responses and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def client_for(config: LLMConfig, recorder: Recorder) -> LLMClient:
    return LLMClient(config, transport=httpx.MockTransport(recorder))


# ---- settings ----------------------------------------------------------------------------

def test_deepseek_forbidden_in_production():
    with pytest.raises(ValidationError, match="not allowed"):
        Settings(_env_file=None, app_env="production", llm_provider="deepseek",
                 llm_api_key=SecretStr("sk"))


def test_deepseek_requires_key_and_has_defaults():
    with pytest.raises(ValidationError, match="LLM_API_KEY"):
        Settings(_env_file=None, llm_provider="deepseek")
    s = Settings(_env_file=None, llm_provider="deepseek", llm_api_key=SecretStr("sk"))
    assert s.resolved_llm_base_url == "https://api.deepseek.com"
    assert s.resolved_llm_model == "deepseek-chat"


def test_vllm_requires_url_and_model():
    with pytest.raises(ValidationError, match="LLM_BASE_URL"):
        Settings(_env_file=None, llm_provider="vllm")
    s = Settings(_env_file=None, app_env="production", llm_provider="vllm",
                 llm_base_url="http://127.0.0.1:8000/v1", llm_model="qwen3")
    assert LLMConfig.from_settings(s).provider == "vllm"


# ---- client ------------------------------------------------------------------------------

async def test_deepseek_request_shape():
    rec = Recorder(completion('{"a": 1}'))
    llm = client_for(DEEPSEEK, rec)
    result = await llm.chat([{"role": "user", "content": "hi"}], json_mode=True)
    req = rec.requests[0]
    assert str(req.url) == "https://api.deepseek.com/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    body = rec.body()
    assert body["model"] == "deepseek-chat"
    assert body["response_format"] == {"type": "json_object"}
    assert "chat_template_kwargs" not in body
    assert result.content == '{"a": 1}' and result.prompt_tokens == 10
    await llm.aclose()


async def test_vllm_request_shape():
    rec = Recorder(completion("ok"))
    llm = client_for(VLLM, rec)
    await llm.chat([{"role": "user", "content": "hi"}])
    assert str(rec.requests[0].url) == "http://127.0.0.1:8000/v1/chat/completions"
    assert "authorization" not in rec.requests[0].headers
    assert rec.body()["chat_template_kwargs"] == {"enable_thinking": False}
    await llm.aclose()


async def test_retries_transient_errors_only():
    rec = Recorder(httpx.Response(503), completion("ok"))
    assert (await client_for(DEEPSEEK, rec).chat([])).content == "ok"
    assert len(rec.requests) == 2

    rec = Recorder(httpx.Response(401))
    with pytest.raises(LLMError) as exc:
        await client_for(DEEPSEEK, rec).chat([])
    assert not exc.value.retryable and len(rec.requests) == 1

    rec = Recorder(httpx.ConnectTimeout("t"), httpx.ConnectTimeout("t"))
    with pytest.raises(LLMError, match="timed out"):
        await client_for(DEEPSEEK, rec).chat([])
    assert len(rec.requests) == 2  # max_retries=1


def test_clean_content():
    assert clean_content('<think>secret</think>\n{"x": 1}') == '{"x": 1}'
    assert clean_content('```json\n{"x": 1}\n```') == '{"x": 1}'


# ---- explainer ---------------------------------------------------------------------------

def rule_result() -> AnalysisResult:
    start = datetime(2026, 10, 1, tzinfo=UTC)
    return AnalysisResult(
        analysis_id="ma_" + "0" * 32, status=AnalysisStatus.COMPLETED,
        analysis_type=AnalysisType.ISOLATION_ROOM, site_id="S1", equipment_ids=["E1", "E2"],
        time_range=TimeRange(start=start, end=start + timedelta(hours=1)),
        health_level=HealthLevel.INSPECT, risk_summary="rule summary",
        evidence=[Evidence(source="MES", equipment_id="E1", metric="m1", time_range="x",
                           observation="连续 3 个样本超出报警限值")],
        recommendations=[Recommendation(priority="high", equipment_id="E1",
                                        action="rule action", rationale="rule rationale")],
        created_at=start,
    )


def explanation(priority="high", equipment_id="E1") -> str:
    return json.dumps({"risk_summary": "模型说明", "recommendations": [
        {"equipment_id": equipment_id, "priority": priority,
         "action": "核查 m1", "rationale": "连续超限"}]}, ensure_ascii=False)


async def test_explainer_applies_valid_output():
    rec = Recorder(completion(explanation()))
    out = await MaintenanceExplainer(client_for(VLLM, rec)).explain(rule_result())
    assert out.risk_summary == "模型说明"
    assert out.health_level is HealthLevel.INSPECT  # verdict untouched
    assert out.recommendations[0].requires_human_confirmation is True
    assert out.model.provider == "vllm" and out.model.precision == "W8A8"
    sent = json.loads(rec.body()["messages"][1]["content"])
    assert sent["health_level"] == "inspect" and sent["rule_recommendations"]


@pytest.mark.parametrize("bad", [
    explanation(priority="low"),            # downgrades a rule recommendation
    explanation(equipment_id="OTHER"),      # equipment outside the analysis
    "not json",
    '{"recommendations": []}',              # missing risk_summary
])
async def test_explainer_rejects_and_keeps_rule_text(bad):
    rec = Recorder(completion(bad), completion(bad))
    original = rule_result()
    out = await MaintenanceExplainer(client_for(DEEPSEEK, rec)).explain(original)
    assert out == original and out.model is None
    assert len(rec.requests) == 2  # one retry on invalid output


async def test_explainer_keeps_rule_text_when_model_down():
    rec = Recorder(httpx.Response(500), httpx.Response(500))
    original = rule_result()
    assert await MaintenanceExplainer(client_for(DEEPSEEK, rec)).explain(original) == original


async def test_explainer_skips_when_no_evidence():
    rec = Recorder()
    original = rule_result().model_copy(update={"evidence": []})
    assert await MaintenanceExplainer(client_for(DEEPSEEK, rec)).explain(original) == original
    assert rec.requests == []


# ---- end to end --------------------------------------------------------------------------

async def test_analysis_uses_model_explanation(settings, catalog, timeseries):
    from .test_api import analysis_body, parse_sse

    rec = Recorder(completion(json.dumps({"risk_summary": "模型说明", "recommendations": [
        {"equipment_id": "ROOM-HOT", "priority": "high", "action": "核查", "rationale": "超限"}]},
        ensure_ascii=False)))
    container = build_container(settings, catalog=catalog, timeseries=timeseries,
                                llm=client_for(DEEPSEEK, rec))
    app = create_app(container=container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        created = await c.post("/internal/v1/maintenance/analyses",
                               json=analysis_body(["ROOM-HOT"]), headers=AUTH)
        aid = created.json()["analysis_id"]
        events = parse_sse((await c.get(f"/internal/v1/maintenance/analyses/{aid}/events",
                                        headers=AUTH)).text)
        result = (await c.get(f"/internal/v1/maintenance/analyses/{aid}", headers=AUTH)).json()
    await container.aclose()

    assert "explaining" in [d.get("stage") for k, d in events if k == "stage"]
    assert result["health_level"] == "inspect"
    assert result["risk_summary"] == "模型说明"
    assert result["model"]["provider"] == "deepseek"
