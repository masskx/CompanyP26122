"""LLM explanation of rule-based maintenance results.

The rules own the verdict. The model may only rewrite the risk summary and the wording of
recommendations, based on the evidence it is given. Its output is schema-validated and
must not drop or downgrade any rule-derived recommendation; if it fails validation (after
retries) the rule-based text is kept and the result carries no model info.
"""

import json
import logging

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from industrial_ai.integrations.llm_client import LLMClient, LLMError
from industrial_ai.schemas.analysis import AnalysisResult, ModelInfo, Recommendation

logger = logging.getLogger(__name__)

_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2}

SYSTEM_PROMPT = """你是核电站设备预测性维护分析助手，\
负责把确定性规则的判定结果整理成便于人工核查的说明。
必须遵守：
1. 只能使用输入中的证据、数据缺口和规则建议，不得引入输入以外的数值、事实或标准。
2. 不得改变健康等级，不得删除或降低规则建议的优先级。
3. 不得给出设备操作、控制、报警确认或工单流转指令；所有建议仅供授权人员人工核查。
4. 数据不足时如实说明缺口，不得推测缺失数据。
只输出一个 JSON 对象，格式为：
{"risk_summary": "不超过300字的风险说明",
 "recommendations": [{"equipment_id": "...", "priority": "low|medium|high",
                      "action": "核查项", "rationale": "依据（引用证据）"}]}"""


class _ExplainedRecommendation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    equipment_id: str
    priority: str = Field(pattern=r"^(low|medium|high)$")
    action: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)


class _Explanation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    risk_summary: str = Field(min_length=1, max_length=1000)
    recommendations: list[_ExplainedRecommendation] = Field(default_factory=list, max_length=20)


class ExplanationRejected(Exception):
    pass


def build_messages(result: AnalysisResult) -> list[dict[str, str]]:
    context = {
        "analysis_type": result.analysis_type,
        "equipment_ids": result.equipment_ids,
        "time_range": result.time_range.label(),
        "health_level": result.health_level,
        "evidence": [e.model_dump() for e in result.evidence],
        "data_gaps": [g.model_dump() for g in result.data_gaps],
        "rule_recommendations": [r.model_dump(exclude={"requires_human_confirmation"})
                                 for r in result.recommendations],
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)},
    ]


def validate_explanation(raw: str, result: AnalysisResult) -> _Explanation:
    try:
        parsed = _Explanation.model_validate_json(raw)
    except ValidationError as exc:
        raise ExplanationRejected("output does not match schema") from exc

    allowed = set(result.equipment_ids)
    if any(r.equipment_id not in allowed for r in parsed.recommendations):
        raise ExplanationRejected("recommendation references unknown equipment")

    for rule in result.recommendations:
        best = max((_PRIORITY_RANK[r.priority] for r in parsed.recommendations
                    if r.equipment_id == rule.equipment_id), default=-1)
        if best < _PRIORITY_RANK[rule.priority]:
            raise ExplanationRejected("rule recommendation dropped or downgraded")
    return parsed


class MaintenanceExplainer:
    def __init__(self, client: LLMClient, *, attempts: int = 2) -> None:
        self._client = client
        self._attempts = attempts

    @property
    def model_info(self) -> ModelInfo:
        c = self._client.config
        return ModelInfo(provider=c.provider, name=c.model, precision=c.precision or "unknown",
                         revision=c.revision or "unknown")

    async def explain(self, result: AnalysisResult) -> AnalysisResult:
        """Return the result with model-written text, or unchanged if the model fails."""
        if not result.evidence:
            return result  # nothing factual to explain
        messages = build_messages(result)
        for attempt in range(1, self._attempts + 1):
            try:
                reply = await self._client.chat(messages, json_mode=True)
                parsed = validate_explanation(reply.content, result)
            except ExplanationRejected as exc:
                logger.warning("model explanation rejected", extra={"fields": {
                    "analysis_id": result.analysis_id, "attempt": attempt, "reason": str(exc)}})
                continue
            except LLMError as exc:
                logger.warning("model call failed", extra={"fields": {
                    "analysis_id": result.analysis_id, "error": str(exc)}})
                return result
            return result.model_copy(update={
                "risk_summary": parsed.risk_summary,
                "recommendations": [
                    Recommendation(priority=r.priority, equipment_id=r.equipment_id,  # type: ignore[arg-type]
                                   action=r.action, rationale=r.rationale)
                    for r in parsed.recommendations
                ],
                "model": self.model_info,
            })
        return result
