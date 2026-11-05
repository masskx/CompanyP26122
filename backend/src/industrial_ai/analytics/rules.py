"""Deterministic, testable health rules.

Rules decide the health level; a model (later) may only explain the result. When data or
thresholds are missing the rules report a data gap and never guess a value.

Per metric:
  * no valid points                                   -> insufficient_data (gap: no_data)
  * no thresholds configured                          -> insufficient_data
                                                         (gap: threshold_not_configured)
  * >= N consecutive points beyond an alarm bound     -> inspect
  * any alarm-bound point, or >= N consecutive beyond
    a warn bound                                      -> watch
  * otherwise normal, unless completeness is below the
    minimum                                           -> insufficient_data (gap: low_completeness)

Per equipment / analysis: inspect > watch > insufficient_data > normal. "normal" therefore
requires every metric to be evaluable.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from industrial_ai.analytics.statistics import SeriesStats, completeness, summarize
from industrial_ai.schemas.analysis import DataGap, Evidence, Recommendation
from industrial_ai.schemas.common import HealthLevel, TelemetryPoint, TimeRange
from industrial_ai.schemas.equipment import MetricDefinition, MetricThresholds

DATA_SOURCE = "MES 设备历史数据"


@dataclass(frozen=True)
class RuleConfig:
    min_completeness: float = 0.8
    consecutive_breach_count: int = 3


@dataclass
class MetricAssessment:
    equipment_id: str
    metric: MetricDefinition
    level: HealthLevel
    stats: SeriesStats
    completeness: float | None
    alarm_run: int = 0
    warn_run: int = 0
    alarm_points: int = 0
    evidence: list[Evidence] = field(default_factory=list)
    gaps: list[DataGap] = field(default_factory=list)


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.4g}"


def _outside(value: float, low: float | None, high: float | None) -> bool:
    return (low is not None and value < low) or (high is not None and value > high)


def max_consecutive(points: Iterable[TelemetryPoint], predicate: Callable[[float], bool]) -> int:
    best = run = 0
    for p in points:
        if p.value is not None and predicate(p.value):
            run += 1
            best = max(best, run)
        elif p.value is not None:
            run = 0
    return best


def _is_alarm(t: MetricThresholds) -> Callable[[float], bool]:
    return lambda v: _outside(v, t.alarm_low, t.alarm_high)


def _is_warn(t: MetricThresholds) -> Callable[[float], bool]:
    return lambda v: _outside(v, t.warn_low, t.warn_high) or _outside(v, t.alarm_low, t.alarm_high)


def assess_metric(
    equipment_id: str,
    metric: MetricDefinition,
    points: list[TelemetryPoint],
    time_range: TimeRange,
    config: RuleConfig,
) -> MetricAssessment:
    points = sorted(points, key=lambda p: p.timestamp)
    stats = summarize(points)
    ratio = completeness(stats.valid_points, metric.expected_interval_seconds,
                         time_range.duration_seconds)
    result = MetricAssessment(equipment_id, metric, HealthLevel.INSUFFICIENT_DATA, stats, ratio)
    unit = f" {metric.unit}" if metric.unit else ""

    if stats.valid_points == 0:
        result.gaps.append(DataGap(equipment_id=equipment_id, metric=metric.name, reason="no_data",
                                   detail="时间范围内无有效数据"))
        return result

    observation = (
        f"有效样本 {stats.valid_points}/{stats.total_points}，最小 {_fmt(stats.minimum)}{unit}，"
        f"最大 {_fmt(stats.maximum)}{unit}，均值 {_fmt(stats.mean)}{unit}，"
        f"最新 {_fmt(stats.last_value)}{unit}"
    )
    if ratio is not None:
        observation += f"，数据完整率 {ratio:.0%}"

    low_completeness = ratio is not None and ratio < config.min_completeness
    if low_completeness:
        result.gaps.append(DataGap(
            equipment_id=equipment_id, metric=metric.name, reason="low_completeness",
            detail=f"数据完整率 {ratio:.0%} 低于要求 {config.min_completeness:.0%}",
        ))

    thresholds = metric.thresholds
    if thresholds is None:
        result.gaps.append(DataGap(
            equipment_id=equipment_id, metric=metric.name, reason="threshold_not_configured",
            detail="未配置判定阈值，无法判断该指标是否异常",
        ))
        result.evidence.append(_evidence(equipment_id, metric, time_range, observation))
        return result

    alarm = _is_alarm(thresholds)
    result.alarm_run = max_consecutive(points, alarm)
    result.warn_run = max_consecutive(points, _is_warn(thresholds))
    result.alarm_points = sum(1 for p in points if p.value is not None and alarm(p.value))
    n = config.consecutive_breach_count

    if result.alarm_run >= n:
        result.level = HealthLevel.INSPECT
        observation += f"；连续 {result.alarm_run} 个样本超出报警限值"
    elif result.alarm_points > 0 or result.warn_run >= n:
        result.level = HealthLevel.WATCH
        if result.alarm_points:
            observation += f"；{result.alarm_points} 个样本超出报警限值（未达连续 {n} 个）"
        if result.warn_run >= n:
            observation += f"；连续 {result.warn_run} 个样本超出预警限值"
    elif low_completeness:
        result.level = HealthLevel.INSUFFICIENT_DATA
    else:
        result.level = HealthLevel.NORMAL
        observation += "；未超出配置限值"

    result.evidence.append(_evidence(equipment_id, metric, time_range, observation))
    return result


def _evidence(
    equipment_id: str, metric: MetricDefinition, time_range: TimeRange, observation: str
) -> Evidence:
    return Evidence(source=DATA_SOURCE, equipment_id=equipment_id, metric=metric.name,
                    time_range=time_range.label(), observation=observation)


_SEVERITY = [HealthLevel.INSPECT, HealthLevel.WATCH, HealthLevel.INSUFFICIENT_DATA,
             HealthLevel.NORMAL]


def combine_levels(levels: Iterable[HealthLevel]) -> HealthLevel:
    present = set(levels)
    if not present:
        return HealthLevel.INSUFFICIENT_DATA
    return next(level for level in _SEVERITY if level in present)


def recommendations_for(assessments: Iterable[MetricAssessment]) -> list[Recommendation]:
    recs: list[Recommendation] = []
    for a in assessments:
        label = a.metric.display_name or a.metric.name
        if a.level is HealthLevel.INSPECT:
            recs.append(Recommendation(
                priority="high", equipment_id=a.equipment_id,
                action=f"安排授权人员核查 {a.equipment_id} 的 {label}",
                rationale=f"连续 {a.alarm_run} 个样本超出报警限值",
            ))
        elif a.level is HealthLevel.WATCH:
            recs.append(Recommendation(
                priority="medium", equipment_id=a.equipment_id,
                action=f"加强对 {a.equipment_id} 的 {label} 的监视并复核趋势",
                rationale="存在超出预警或报警限值的样本，但未达到连续判定条件",
            ))
    return recs


def coverage_confidence(assessments: list[MetricAssessment], overall: HealthLevel) -> float | None:
    """Data-coverage score in [0, 1]: share of evaluable metrics x mean completeness."""
    if overall is HealthLevel.INSUFFICIENT_DATA or not assessments:
        return None
    evaluated = [a for a in assessments if a.level is not HealthLevel.INSUFFICIENT_DATA]
    coverage = len(evaluated) / len(assessments)
    ratios = [a.completeness for a in evaluated if a.completeness is not None]
    mean_ratio = sum(ratios) / len(ratios) if ratios else 1.0
    return round(coverage * mean_ratio, 2)
