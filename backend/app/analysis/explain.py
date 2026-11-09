"""Explanation text for abnormal records.

The language model never sees raw curves and never decides a level. It receives the
evidence summary built by the rules (event codes E1.., times, statistics, thresholds,
comparisons, context) and writes: summary, details, evidence, recommendations,
uncertainties - all referencing event codes.

Validation (any failure = invalid output):
  * valid JSON matching the schema; every evidence item / recommendation cites known
    event codes and every event is cited at least once in the evidence
  * every number, time and date in the text must appear in the evidence (rounding of
    a supported value is accepted when the number keeps >= 2 significant digits)
Invalid output -> one retry with the error -> rule-based template (method
"template_fallback", the error is kept). Missing-data records always use the template.
Confidence is computed here from configuration, completeness, baseline availability and
event sources - not by the model.
"""

import json
import logging
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError

from app.analysis.models import (
    Comparison,
    Event,
    EvidenceItem,
    Explanation,
    Level,
    Recommendation,
    Record,
    Source,
)

logger = logging.getLogger(__name__)

SYSTEM = """你是核电站固定监测站的设备维护分析助手。你会收到规则引擎已经判定的异常记录证据摘要（JSON）。

要求：
1. 只能依据证据摘要撰写说明。不得改变等级，不得新增判定，不得推测证据中没有的原因、数值、时间或日期。
2. 引用数值、时间时必须与证据中的数值一致（可以保留更少位小数）；需要百分比时使用证据中的 *_pct 字段。
3. 每条 evidence 和 recommendation 都要在 event_ids 中写明对应的事件编号（如 "E1"），每个事件至少在 evidence 中出现一次。
4. 同比/环比为 unavailable 或上下文不可用时，在 uncertainties 中说明，不得把缺失当作 0。
5. 建议仅供授权人员人工核查，不写操作或控制指令。
6. 使用简洁、专业的中文。只输出一个 JSON 对象，不要输出其他文字。

JSON 格式：
{"summary": "一句话结论（不超过 120 字）",
 "details": "分析说明（不超过 600 字）",
 "evidence": [{"event_ids": ["E1"], "observation": "该事件的客观观察"}],
 "recommendations": [{"priority": "high|medium|low", "action": "建议动作", "rationale": "依据", "event_ids": ["E1"]}],
 "uncertainties": ["不确定性或数据缺口"]}"""

_SOURCE_LABELS = {Source.SOURCE_REPORTED: "数据源上报", Source.RULE: "规则计算",
                  Source.DATA_QUALITY: "数据质量检测"}
_METRIC_LABELS = {"dose": "γ 剂量率", "device": "设备状态", "data_quality": "数据完整性",
                  "battery": "蓄电池电压"}
_COMPARISON_LABELS = {"previous_day": "前一日同时段", "previous_7_days": "前 7 日同时段",
                      "previous_month_same_day": "上月同日同时段",
                      "previous_year_same_day": "去年同日同时段"}
_ADVICE = {
    "dose_h1": ("medium", "核查该时段降雨、周边作业等环境因素，并复核探测器读数",
                "剂量率连续超过一级阈值"),
    "dose_h2": ("high", "尽快安排人员现场核查探测器及周边辐射环境",
                "剂量率连续超过二级阈值"),
    "reported_h1": ("medium", "核对设备报警记录与读数，确认报警原因", "设备上报一级报警"),
    "reported_h2": ("high", "尽快核对设备报警记录并现场核查", "设备上报二级报警"),
    "device_fault": ("high", "查看设备自检与故障代码，必要时安排检修", "设备上报故障状态"),
    "offline": ("medium", "检查设备供电与通信链路", "设备上报离线，期间无可用数据"),
    "comm_interruption": ("medium", "检查通信链路与通信模块", "通信中断，期间无可用数据"),
    "sampling_gap": ("low", "检查采集链路，确认缺失数据能否补传", "相邻样本间隔超过允许值"),
    "no_data": ("high", "确认设备是否运行、通信是否中断、配置是否正确", "分析窗口内没有可用数据"),
    "stuck_detector": ("medium", "检查探测器是否卡滞或采集程序是否重复上报旧值",
                       "读数长时间完全不变"),
    "low_completeness": ("low", "检查采集与传输稳定性", "全天数据完整率低于要求"),
    "battery_low_warn": ("medium", "检查蓄电池容量和市电供电情况", "电压低于预警值"),
    "battery_low_alarm": ("high", "尽快检查蓄电池与供电，评估是否需要更换", "电压低于报警值"),
    "battery_drop_on_ac": ("high", "检查充电器 / 整流模块与蓄电池连接", "市电正常时电压异常下降"),
    "slow_charging": ("medium", "评估蓄电池老化程度，安排容量测试", "市电恢复后充电时间明显变长"),
}
MISSING_KINDS = {"no_data", "offline", "comm_interruption", "sampling_gap", "low_completeness"}


class InvalidOutput(ValueError):
    pass


class _Evidence(BaseModel):
    event_ids: list[str] = Field(min_length=1)
    observation: str = Field(min_length=1, max_length=400)


class _Recommendation(BaseModel):
    priority: str = Field(pattern=r"^(high|medium|low)$")
    action: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=400)
    event_ids: list[str] = Field(min_length=1)


class _Output(BaseModel):
    summary: str = Field(min_length=1, max_length=300)
    details: str = Field(min_length=1, max_length=1500)
    evidence: list[_Evidence] = Field(min_length=1)
    recommendations: list[_Recommendation] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


# ---- evidence summary --------------------------------------------------------------------

def _fmt(t: datetime, tz: ZoneInfo) -> str:
    return t.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def _cmp(c: Comparison) -> dict:
    if c.status != "available":
        return {"status": c.status, "reason": c.reason}
    out = {"status": "available", "mean": c.mean, "max": c.max, "samples": c.samples,
           "delta_mean": c.delta_mean, "delta_pct": c.delta_pct}
    if c.days_available is not None:
        out["days_available"] = c.days_available
    return {k: v for k, v in out.items() if v is not None}


def evidence_summary(record: Record, tz: ZoneInfo) -> dict:
    ds = record.day_stats
    day = {k: ds.get(k) for k in ("unit", "sampling_interval_s", "calibration_factor",
                                  "expected_samples", "valid_samples", "min", "max", "mean",
                                  "std", "status_counts", "battery_level_min", "ac_outages")
           if ds.get(k) not in (None, [], {})}
    if ds.get("completeness") is not None:
        day["completeness_pct"] = round(ds["completeness"] * 100, 1)
    events = []
    for e in record.events or []:
        item = {"event_id": e.code, "kind": e.kind, "title": e.title, "level": e.level.value,
                "source": e.source.value, "start": _fmt(e.start, tz), "end": _fmt(e.end, tz),
                "duration_minutes": round(e.duration_s / 60, 1),
                "duration_hours": round(e.duration_s / 3600, 2),
                "open_at_start": e.open_at_start, "open_at_end": e.open_at_end,
                "sample_count": e.sample_count, "unit": e.unit, "min": e.min, "max": e.max,
                "mean": e.mean, "std": e.std, "rule": e.rule,
                "comparisons": {k: _cmp(c) for k, c in e.comparisons.items()},
                "context": e.context}
        if e.completeness is not None:
            item["completeness_pct"] = round(e.completeness * 100, 1)
        if e.corroborated_by_source is not None:
            item["corroborated_by_source"] = e.corroborated_by_source
        events.append({k: v for k, v in item.items() if v is not None})
    return {"station": f"{record.station_name}（{record.station_id}）",
            "device": f"{record.device_name}（{record.device_id}）",
            "equipment_type": record.equipment_type.value,
            "analysis_date": record.analysis_date.isoformat(), "window_hours": 24,
            "level": record.level.value, "data_status": record.data_status,
            "day_stats": day, "events": events}


# ---- validation -----------------------------------------------------------------------------

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
_CODE = re.compile(r"\bE\d+\b")
# a number not glued to an identifier ("ST01", "RM-02", "H1"); a minus sign is allowed and
# ignored, so "-0.62" in the evidence supports "下降 0.62"
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(?<![A-Za-z0-9_]-)\d+(?:\.\d+)?(?![A-Za-z0-9_])")


def _hhmm(t: str) -> str:
    h, m = t.split(":")[:2]
    return f"{int(h):02d}:{m}"


def _numbers(text: str) -> list[str]:
    text = _CODE.sub(" ", _TIME.sub(" ", _DATE.sub(" ", text)))
    return _NUMBER.findall(text)


def _sig_digits(token: str) -> int:
    return len(token.replace(".", "").lstrip("0"))


def _supported(token: str, allowed: list[float]) -> bool:
    value = float(token)
    decimals = len(token.split(".")[1]) if "." in token else 0
    for v in allowed:
        if abs(v - value) <= 1e-9:
            return True
        if _sig_digits(token) >= 2 and round(v, decimals) == value and \
                (decimals > 0 or abs(v) >= 10):
            return True
    return False


def validate(raw: str, record: Record, evidence: dict) -> _Output:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        out = _Output.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise InvalidOutput(f"JSON 无法解析或不符合格式：{str(exc)[:300]}") from exc
    codes = {e.code for e in record.events or []}
    cited = set()
    for item in [*out.evidence, *out.recommendations]:
        unknown = set(item.event_ids) - codes
        if unknown:
            raise InvalidOutput(f"引用了不存在的事件编号：{sorted(unknown)}")
        if isinstance(item, _Evidence):
            cited.update(item.event_ids)
    if codes - cited:
        raise InvalidOutput(f"evidence 未覆盖事件：{sorted(codes - cited)}")
    texts = [out.summary, out.details, *out.uncertainties,
             *(e.observation for e in out.evidence),
             *(t for r in out.recommendations for t in (r.action, r.rationale))]
    joined = "\n".join(texts)
    if unknown := set(_CODE.findall(joined)) - codes:
        raise InvalidOutput(f"文本引用了不存在的事件编号：{sorted(unknown)}")
    source = json.dumps(evidence, ensure_ascii=False)
    allowed_dates = set(_DATE.findall(source))
    allowed_times = {_hhmm(t) for t in _TIME.findall(source)} | {"00:00", "24:00"}
    allowed = [float(n) for n in _numbers(source)]
    for d in _DATE.findall(joined):
        if d not in allowed_dates:
            raise InvalidOutput(f"日期 {d} 不在证据中")
    for t in _TIME.findall(joined):
        if _hhmm(t) not in allowed_times:
            raise InvalidOutput(f"时间 {t} 不在证据中")
    for n in _numbers(joined):
        if not _supported(n, allowed):
            raise InvalidOutput(f"数值 {n} 无证据支持")
    return out


# ---- confidence --------------------------------------------------------------------------

_SOURCE_SCORE = {Source.SOURCE_REPORTED: 0.8, Source.RULE: 0.7, Source.DATA_QUALITY: 0.6}


def confidence(record: Record) -> tuple[float, dict]:
    events = record.events or []
    unevaluated = record.day_stats.get("unevaluated_rules") or []
    factors: dict[str, float | None] = {
        # records only exist when the thresholds are configured
        "thresholds_configured": 1.0 if not unevaluated else 0.8,
        "completeness": record.day_stats.get("completeness"),
    }
    applicable = [c for e in events for c in e.comparisons.values()
                  if c.status != "not_applicable"]
    factors["baseline_available"] = (sum(c.status == "available" for c in applicable)
                                     / len(applicable)) if applicable else None
    scores = [1.0 if e.corroborated_by_source else _SOURCE_SCORE[e.source] for e in events]
    factors["event_sources"] = sum(scores) / len(scores) if scores else None
    weights = {"thresholds_configured": 0.2, "completeness": 0.3, "baseline_available": 0.2,
               "event_sources": 0.3}
    used = {k: v for k, v in factors.items() if v is not None}
    total = sum(weights[k] for k in used)
    value = sum(weights[k] * v for k, v in used.items()) / total if total else 0.0
    return round(value, 2), {k: (round(v, 3) if v is not None else None)
                             for k, v in factors.items()} | {"weights": weights}


# ---- template -------------------------------------------------------------------------------

def _num(v: float | None, unit: str | None) -> str:
    if v is None:
        return "-"
    return f"{v:g}{' ' + unit if unit else ''}"


def _time_range(e: Event, tz: ZoneInfo) -> str:
    a, b = e.start.astimezone(tz), e.end.astimezone(tz)
    end = b.strftime("%H:%M:%S") if b.date() == a.date() else "24:00:00"
    return f"{a.strftime('%H:%M:%S')}–{end}"


def _observation(e: Event, tz: ZoneInfo) -> str:
    parts = [f"{e.code} {_time_range(e, tz)} {e.title}，持续 {round(e.duration_s / 60, 1)} 分钟"]
    if e.kind in MISSING_KINDS and e.kind != "low_completeness":
        parts.append("期间无可用样本")
    elif e.kind == "low_completeness":
        parts.append(f"有效样本 {e.sample_count}/{e.expected_samples}")
    elif e.kind == "slow_charging":
        parts.append(f"充电持续 {e.context.get('charge_minutes')} 分钟，"
                     f"预期 {e.rule.get('expected_minutes')} 分钟")
    else:
        parts.append(f"样本 {e.sample_count} 个，最大 {_num(e.max, e.unit)}，"
                     f"最小 {_num(e.min, e.unit)}，均值 {_num(e.mean, e.unit)}")
        if e.rule.get("threshold") is not None:
            parts.append(f"阈值 {_num(e.rule['threshold'], e.unit)}")
    ac = e.context.get("ac_power", {})
    if ac.get("status") == "normal":
        parts.append("同期市电正常")
    elif ac.get("status") in ("lost", "interrupted"):
        parts.append("同期市电中断")
    rain = e.context.get("rainfall", {})
    if rain.get("status") == "available" and rain.get("raining"):
        parts.append(f"同期降雨 {rain.get('total_mm')} mm")
    return "，".join(parts) + "。"


def _uncertainties(record: Record) -> list[str]:
    out: list[str] = []
    for e in record.events or []:
        reasons = sorted({f"{_COMPARISON_LABELS.get(k, k)}：{c.reason}"
                          for k, c in e.comparisons.items() if c.status == "unavailable"})
        if reasons:
            out.append(f"{e.code} 同比/环比不可用（" + "；".join(reasons) + "）")
        if e.open_at_start:
            out.append(f"{e.code} 在分析窗口开始时已在进行，实际开始时间更早")
        if e.open_at_end:
            out.append(f"{e.code} 在分析窗口结束时仍未结束")
        if e.corroborated_by_source is False:
            out.append(f"{e.code} 设备未上报对应的报警状态，需核实设备报警设置")
        rain = e.context.get("rainfall", {})
        if e.category.value == "dose" and rain.get("status") == "unavailable":
            out.append(f"{e.code} 降雨数据不可用（{rain.get('reason')}），无法排除降雨影响")
    for rule in record.day_stats.get("unevaluated_rules") or []:
        out.append(f"未能评估的规则：{rule}")
    return list(dict.fromkeys(out))


def template(record: Record, tz: ZoneInfo) -> dict:
    events = record.events or []
    level_text = {Level.INSPECT: "建议安排检修核查", Level.WATCH: "需关注",
                  Level.INSUFFICIENT_DATA: "数据缺失，无法完成判定"}[record.level]
    summary = f"{record.station_name} {record.device_name}：{record.title}，{level_text}。"
    observations = [_observation(e, tz) for e in events]
    recs: dict[str, Recommendation] = {}
    for e in events:
        priority, action, why = _ADVICE.get(e.kind, ("medium", "人工核查该事件", e.title))
        if action in recs:
            recs[action].event_ids.append(e.code)
        else:
            recs[action] = Recommendation(priority=priority, action=action, rationale=why,
                                          event_ids=[e.code])
    return {"summary": summary, "details": "".join(observations),
            "evidence": [EvidenceItem(event_ids=[e.code], observation=o)
                         for e, o in zip(events, observations, strict=True)],
            "recommendations": sorted(recs.values(), key=lambda r: ("high", "medium",
                                                                    "low").index(r.priority)),
            "uncertainties": _uncertainties(record)}


def _decorate(items: list[EvidenceItem], record: Record, tz: ZoneInfo) -> list[EvidenceItem]:
    """Metric / time range / source of evidence items come from the events, not the model."""
    by_code = {e.code: e for e in record.events or []}
    for item in items:
        events = [by_code[c] for c in item.event_ids if c in by_code]
        if not events:
            continue
        item.metric = "、".join(dict.fromkeys(_METRIC_LABELS[e.category.value] for e in events))
        item.time_range = "；".join(_time_range(e, tz) for e in events)
        item.source = "、".join(dict.fromkeys(_SOURCE_LABELS[e.source] for e in events))
    return items


# ---- explainer ------------------------------------------------------------------------------

class Explainer:
    def __init__(self, llm, model_name: str | None, tz: ZoneInfo, use_llm: bool = True) -> None:
        self.llm = llm.bind(response_format={"type": "json_object"}) \
            if llm is not None and use_llm else None
        self.model_name, self.tz = model_name, tz

    async def explain(self, record: Record) -> Explanation:
        conf, factors = confidence(record)
        now = datetime.now(UTC)
        missing_only = record.level == Level.INSUFFICIENT_DATA and \
            all(e.kind in MISSING_KINDS for e in record.events or [])
        if self.llm is None or missing_only:
            return self._from_template(record, "template", conf, factors, now)
        evidence = evidence_summary(record, self.tz)
        messages = [SystemMessage(SYSTEM), HumanMessage(
            "证据摘要：\n```json\n" + json.dumps(evidence, ensure_ascii=False, indent=1) + "\n```")]
        error = None
        for attempt in range(2):
            try:
                reply = await self.llm.ainvoke(messages)
                raw = reply.content if isinstance(reply.content, str) else str(reply.content)
                out = validate(raw, record, evidence)
            except InvalidOutput as exc:
                error = str(exc)
                logger.info("explanation for %s rejected (attempt %d): %s", record.record_id,
                            attempt + 1, error)
                messages = [*messages, HumanMessage(
                    f"上一次输出无效：{error}。请严格依据证据摘要重新输出 JSON。")]
                continue
            except Exception as exc:  # model unreachable, timeout ...
                error = f"模型调用失败：{type(exc).__name__}: {exc}"[:300]
                logger.warning("explanation for %s: %s", record.record_id, error)
                break
            items = [EvidenceItem(**e.model_dump()) for e in out.evidence]
            return Explanation(
                summary=out.summary, details=out.details,
                evidence=_decorate(items, record, self.tz),
                recommendations=[Recommendation(**r.model_dump()) for r in out.recommendations],
                uncertainties=out.uncertainties, method="llm", model=self.model_name,
                confidence=conf, confidence_factors=factors, generated_at=now)
        return self._from_template(record, "template_fallback", conf, factors, now, error)

    def _from_template(self, record: Record, method: str, conf: float, factors: dict,
                       now: datetime, error: str | None = None) -> Explanation:
        t = template(record, self.tz)
        return Explanation(summary=t["summary"], details=t["details"],
                           evidence=_decorate(t["evidence"], record, self.tz),
                           recommendations=t["recommendations"],
                           uncertainties=t["uncertainties"], method=method,  # type: ignore[arg-type]
                           model=None, error=error, confidence=conf, confidence_factors=factors,
                           generated_at=now)

