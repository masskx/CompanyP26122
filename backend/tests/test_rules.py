"""Detection rules (pure functions): interval state machine, status runs, missing data,
stuck detector, battery rules."""

from datetime import UTC, datetime, timedelta

from app.analysis import rules
from app.analysis.rules import Sample

T0 = datetime(2026, 11, 8, tzinfo=UTC)
GAP = timedelta(seconds=90)  # 3 x 30 s
L1, L2 = 0.25, 0.5


def series(values, step=30, start=T0):
    return [Sample(start + timedelta(seconds=i * step), v) for i, v in enumerate(values)
            if v is not None]


def spans(values, **kw):
    return rules.threshold_spans(series(values), [L1, L2], max_gap=GAP, window_start=T0, **kw)


def shape(found):
    return [(s.level, int((s.start - T0).total_seconds() // 30),
             int((s.end - T0).total_seconds() // 30)) for s in found]


N, A, B = 0.1, 0.3, 0.6  # normal, above L1, above L2


def test_three_consecutive_samples_make_an_interval():
    assert spans([N, A, A, N, N, N]) == []
    assert shape(spans([N, A, A, A, N, N, N])) == [(1, 1, 3)]


def test_one_continuous_alarm_is_one_event_and_a_new_alarm_after_recovery_is_another():
    # a single normal sample inside the alarm does not split it (recovery needs 3)
    assert shape(spans([A, A, A, N, A, A, N, N, N])) == [(1, 0, 5)]
    # 02:00-03:00 and 08:00-09:00 style: two separate intervals
    assert shape(spans([A, A, A, N, N, N, N, A, A, A, N, N, N])) == [(1, 0, 2), (1, 7, 9)]


def test_escalation_opens_a_new_interval_deescalation_does_not():
    assert shape(spans([A, A, A, B, B, B, A, A, N, N, N])) == [(1, 0, 2), (2, 3, 7)]
    # straight to H2: one interval at level 2
    assert shape(spans([B, B, B, N, N, N])) == [(2, 0, 2)]


def test_gap_ends_an_interval_and_open_flags():
    found = spans([A, A, A, None, None, None, None, A, A, A])
    assert shape(found) == [(1, 0, 2), (1, 7, 9)]
    assert found[0].open_at_start and not found[0].open_at_end
    assert found[1].open_at_end and not found[1].open_at_start


def test_below_direction_for_battery():
    v = series([13.4, 11.7, 11.7, 11.6, 11.3, 11.3, 11.2, 12.0, 13.0, 13.4])
    found = rules.threshold_spans(v, [11.8, 11.4], below=True, max_gap=GAP)
    assert [s.level for s in found] == [1, 2]


def test_status_runs_split_by_value_and_gap():
    status = series([0, 1, 1, 2, 2, 0, 3, 3, None, None, None, None, 3])
    runs = rules.runs(status, {1, 2, 3}, max_gap=GAP)
    assert [(r.level, len(r.samples)) for r in runs] == [(1, 2), (2, 2), (3, 2), (3, 1)]


def test_missing_spans():
    times = [s.t for s in series([1, 1, None, None, None, None, 1, 1])]
    end = T0 + timedelta(seconds=8 * 30)
    found = rules.missing_spans(times, T0, end, max_gap=GAP)
    assert [(int((s.start - T0).total_seconds()), int((s.end - T0).total_seconds()))
            for s in found] == [(30, 180)]
    # a short interruption counts when the source reported it
    offline = [T0 + timedelta(seconds=45)]
    short = rules.missing_spans([T0, T0 + timedelta(seconds=60)], T0, T0 + timedelta(seconds=60),
                                max_gap=GAP, offline=offline)
    assert len(short) == 1 and short[0].detail["offline_reports"] == 1
    whole = rules.missing_spans([], T0, end, max_gap=GAP)
    assert whole[0].detail["whole_window"] and whole[0].open_at_start and whole[0].open_at_end


def test_stuck_detector():
    v = series([0.1, 0.12] + [0.11] * 121 + [0.1])  # 120 x 30 s = 60 min identical
    found = rules.stuck_spans(v, min_duration=timedelta(minutes=60), max_gap=GAP)
    assert len(found) == 1 and len(found[0].samples) == 121
    assert rules.stuck_spans(v, min_duration=timedelta(minutes=61), max_gap=GAP) == []


def test_voltage_drop_only_with_mains_normal_and_not_charging():
    minutes = 120
    v = series([13.5] * 30 + [13.5 - 0.02 * i for i in range(60)] + [12.3] * 30, step=60)
    ac = series([1] * minutes, step=60)
    idle = series([0] * minutes, step=60)
    found = rules.voltage_drop_spans(v, ac, idle, drop_v=0.5, window=timedelta(minutes=60),
                                     max_gap=timedelta(minutes=3))
    assert len(found) == 1 and found[0].start >= T0 + timedelta(minutes=28)
    lost = series([1] * 30 + [0] * 90, step=60)
    assert rules.voltage_drop_spans(v, lost, idle, drop_v=0.5, window=timedelta(minutes=60),
                                    max_gap=timedelta(minutes=3)) == []
    # absorption -> float relaxation right after charging is not a fault
    relax = series([14.4] * 30 + [13.5] * 90, step=60)
    charging = series([1] * 30 + [0] * 90, step=60)
    assert rules.voltage_drop_spans(relax, ac, charging, drop_v=0.5,
                                    window=timedelta(minutes=60),
                                    max_gap=timedelta(minutes=3)) == []


def test_charge_sessions_after_mains_restoration():
    ac = series([1, 0, 0, 1, 1, 1, 1, 1, 1, 1], step=600)
    charging = series([0, 0, 0, 1, 1, 1, 1, 0, 0, 0], step=600)
    [s] = rules.charge_sessions(ac, charging, T0 + timedelta(hours=2), max_gap=GAP)
    assert s.detail == {"started": True, "finished": True} and s.duration_s == 4 * 600
    still = series([0, 0, 0, 1, 1, 1, 1, 1, 1, 1], step=600)
    [open_] = rules.charge_sessions(ac, still, T0 + timedelta(seconds=9 * 600 + 30),
                                    max_gap=GAP)
    assert open_.open_at_end and not open_.detail["finished"]


def test_stats_population_std():
    st = rules.stats([1.0, 3.0])
    assert st == {"count": 2, "min": 1.0, "max": 3.0, "mean": 2.0, "std": 1.0}
    assert rules.stats([])["mean"] is None
