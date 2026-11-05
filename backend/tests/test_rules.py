from datetime import UTC, datetime, timedelta

from industrial_ai.analytics.rules import (
    RuleConfig,
    assess_metric,
    combine_levels,
    coverage_confidence,
    max_consecutive,
)
from industrial_ai.schemas.common import HealthLevel, TimeRange
from industrial_ai.schemas.equipment import MetricDefinition

from .conftest import TEST_THRESHOLDS, make_points

START = datetime(2026, 10, 1, tzinfo=UTC)
RANGE = TimeRange(start=START, end=START + timedelta(hours=1))
CONFIG = RuleConfig(min_completeness=0.8, consecutive_breach_count=3)
METRIC = MetricDefinition(name="m1", thresholds=TEST_THRESHOLDS)


def assess(values, metric=METRIC):
    return assess_metric("E1", metric, make_points(START, values), RANGE, CONFIG)


def test_no_data_is_insufficient():
    result = assess([])
    assert result.level is HealthLevel.INSUFFICIENT_DATA
    assert [g.reason for g in result.gaps] == ["no_data"]


def test_all_null_values_is_insufficient():
    assert assess([None, None]).level is HealthLevel.INSUFFICIENT_DATA


def test_missing_threshold_never_guesses():
    metric = MetricDefinition(name="radiation_dose_rate", thresholds=None)
    result = assess([1e9, 1e9, 1e9], metric)
    assert result.level is HealthLevel.INSUFFICIENT_DATA
    assert [g.reason for g in result.gaps] == ["threshold_not_configured"]
    assert result.evidence  # stats are still reported as facts


def test_within_limits_is_normal():
    assert assess([10, 20, 30]).level is HealthLevel.NORMAL


def test_consecutive_alarm_is_inspect():
    result = assess([10, 81, 82, 83, 10])
    assert result.level is HealthLevel.INSPECT
    assert result.alarm_run == 3


def test_single_alarm_spike_is_watch():
    assert assess([10, 90, 10, 10]).level is HealthLevel.WATCH


def test_consecutive_warn_is_watch():
    assert assess([55, 60, 65]).level is HealthLevel.WATCH


def test_nulls_do_not_break_a_run():
    assert max_consecutive(make_points(START, [90, None, 90, 90]), lambda v: v > 80) == 3


def test_low_completeness_without_breach_is_insufficient():
    metric = MetricDefinition(name="m1", thresholds=TEST_THRESHOLDS, expected_interval_seconds=60)
    result = assess([10, 10], metric)  # 2 of ~60 expected samples
    assert result.level is HealthLevel.INSUFFICIENT_DATA
    assert "low_completeness" in [g.reason for g in result.gaps]


def test_breach_is_reported_even_with_low_completeness():
    metric = MetricDefinition(name="m1", thresholds=TEST_THRESHOLDS, expected_interval_seconds=60)
    assert assess([90, 90, 90], metric).level is HealthLevel.INSPECT


def test_combine_levels_priority():
    L = HealthLevel
    assert combine_levels([]) is L.INSUFFICIENT_DATA
    assert combine_levels([L.NORMAL, L.INSUFFICIENT_DATA]) is L.INSUFFICIENT_DATA
    assert combine_levels([L.NORMAL, L.WATCH, L.INSUFFICIENT_DATA]) is L.WATCH
    assert combine_levels([L.WATCH, L.INSPECT]) is L.INSPECT
    assert combine_levels([L.NORMAL, L.NORMAL]) is L.NORMAL


def test_coverage_confidence():
    ok, missing = assess([10, 10]), assess([])
    assert coverage_confidence([ok], HealthLevel.NORMAL) == 1.0
    assert coverage_confidence([ok, missing], HealthLevel.WATCH) == 0.5
    assert coverage_confidence([ok], HealthLevel.INSUFFICIENT_DATA) is None
