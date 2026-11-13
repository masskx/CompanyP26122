"""Deterministic detection rules (pure functions, no I/O). Every abnormal interval found
here becomes one analysis event; nothing is decided by the language model.

Threshold intervals (dose rate above L1 / L2, battery voltage below warning / alarm):
  * a level starts when N consecutive valid samples are beyond its threshold
    (N = ANALYSIS_CONSECUTIVE_SAMPLES, default 3); the interval starts at the first of them
  * one continuous alarm is one interval; dropping back to a lower (still alarming) level
    stays in the same interval
  * escalation (N consecutive samples beyond the higher threshold) closes the interval and
    opens a new one at the higher level
  * recovery needs N consecutive samples back inside the limits; the interval ends at the
    last abnormal sample. A later alarm opens a new interval
  * a gap longer than ANALYSIS_GAP_INTERVALS sampling intervals ends the interval (we cannot
    claim continuity across missing data; the gap is reported separately)
Status runs: consecutive samples with the same reported status (H1 / H2 / fault).
Missing data: no usable sample for longer than the gap limit, offline / communication
interruption reported by the source, or nothing at all in the window.
Stuck detector: identical consecutive values for at least ANALYSIS_STUCK_MINUTES
(standard deviation zero).
"""

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class Sample:
    t: datetime
    v: float


@dataclass
class Span:
    start: datetime
    end: datetime
    level: int = 0                 # threshold index (1 = first threshold) or status code
    samples: list[Sample] = field(default_factory=list)
    open_at_start: bool = False    # already abnormal when the window began
    open_at_end: bool = False      # still abnormal when the window ended
    detail: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()


def stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None, "std": None}
    return {"count": len(values), "min": min(values), "max": max(values),
            "mean": statistics.fmean(values),
            "std": statistics.pstdev(values) if len(values) > 1 else 0.0}


def expected_samples(start: datetime, end: datetime, interval_s: float) -> int:
    return max(1, int((end - start).total_seconds() // interval_s) + 1)


# ---- threshold intervals ----------------------------------------------------------------

def threshold_spans(samples: list[Sample], thresholds: list[float], *, below: bool = False,
                    consecutive: int = 3, max_gap: timedelta, window_start: datetime | None = None
                    ) -> list[Span]:
    """Intervals beyond ascending-severity thresholds (dose: L1 < L2 with `>`;
    battery: warn > alarm with `<`, pass below=True). Span.level = threshold index (1-based)."""
    n_levels = len(thresholds)

    def level_of(v: float) -> int:
        lv = 0
        for i, th in enumerate(thresholds, 1):
            if (v < th) if below else (v > th):
                lv = i
        return lv

    levels = [level_of(s.v) for s in samples]
    spans: list[Span] = []
    streak = [0] * (n_levels + 1)
    streak_start = [0] * (n_levels + 1)
    cur: dict | None = None  # {"level", "start", "last"} (sample indexes)
    recovery = 0

    def close(start: int, end: int, level: int, open_end: bool = False) -> None:
        part = samples[start:end + 1]
        open_start = start == 0 and window_start is not None and \
            samples[0].t - window_start <= max_gap
        spans.append(Span(part[0].t, part[-1].t, level, part, open_start, open_end))

    for i, s in enumerate(samples):
        if i and s.t - samples[i - 1].t > max_gap:
            if cur:
                close(cur["start"], cur["last"], cur["level"])
            cur, recovery = None, 0
            streak = [0] * (n_levels + 1)
        lv = levels[i]
        for lvl in range(1, n_levels + 1):
            if lv >= lvl:
                if streak[lvl] == 0:
                    streak_start[lvl] = i
                streak[lvl] += 1
            else:
                streak[lvl] = 0
        if cur is None:
            top = max((lvl for lvl in range(1, n_levels + 1) if streak[lvl] >= consecutive),
                      default=0)
            if top:
                cur, recovery = {"level": top, "start": streak_start[top], "last": i}, 0
            continue
        higher = max((lvl for lvl in range(cur["level"] + 1, n_levels + 1)
                      if streak[lvl] >= consecutive), default=0)
        if higher:
            new_start = streak_start[higher]
            before = [j for j in range(cur["start"], new_start) if levels[j] >= 1]
            if before:  # escalation: the lower-level interval ends, a new one begins
                close(cur["start"], before[-1], cur["level"])
                cur = {"level": higher, "start": new_start, "last": i}
            else:       # escalated immediately: same interval, higher level
                cur.update(level=higher, last=i)
            recovery = 0
        elif lv >= 1:
            cur["last"], recovery = i, 0
        else:
            recovery += 1
            if recovery >= consecutive:
                close(cur["start"], cur["last"], cur["level"])
                cur = None
    if cur:
        close(cur["start"], cur["last"], cur["level"], open_end=recovery == 0)
    return spans


# ---- status runs ------------------------------------------------------------------------

def runs(samples: list[Sample], codes: set[int], *, max_gap: timedelta,
         window_start: datetime | None = None, window_end: datetime | None = None
         ) -> list[Span]:
    """Runs of consecutive samples whose (integer) value is in `codes`; one span per run
    of the same value. A gap longer than max_gap ends a run."""
    out: list[Span] = []
    cur: list[Sample] = []

    def flush() -> None:
        if not cur:
            return
        open_start = window_start is not None and cur[0].t == samples[0].t and \
            cur[0].t - window_start <= max_gap
        open_end = window_end is not None and cur[-1].t == samples[-1].t and \
            window_end - cur[-1].t <= max_gap
        out.append(Span(cur[0].t, cur[-1].t, int(cur[0].v), list(cur), open_start, open_end))

    for i, s in enumerate(samples):
        code = int(round(s.v))
        gap = i > 0 and s.t - samples[i - 1].t > max_gap
        if cur and (gap or code != int(cur[0].v)):
            flush()
            cur = []
        if code in codes:
            cur.append(Sample(s.t, float(code)))
    flush()
    return out


# ---- missing data -----------------------------------------------------------------------

def missing_spans(present: list[datetime], start: datetime, end: datetime, *,
                  max_gap: timedelta, offline: list[datetime] | None = None) -> list[Span]:
    """Periods without a usable sample. `present` = times of usable samples, `offline` =
    times the source reported offline / communication interrupted. A period counts if it
    is longer than max_gap or contains an offline report. detail["offline_reports"] holds
    how many offline reports fall inside. A window without any usable sample returns one
    span covering the whole window with detail["whole_window"] = True."""
    offline = sorted(offline or [])
    present = sorted(present)
    if not present:
        return [Span(start, end, 0, [], True, True,
                     {"whole_window": True, "offline_reports": len(offline)})]
    out = []
    bounds = [(start, present[0], True, False)] + \
        [(a, b, False, False) for a, b in zip(present, present[1:], strict=False)] + \
        [(present[-1], end, False, True)]
    for a, b, at_start, at_end in bounds:
        inside = [t for t in offline if a < t < b or (at_start and t == a) or
                  (at_end and t == b)]
        if b - a > max_gap or inside:
            out.append(Span(a, b, 0, [], at_start, at_end,
                            {"whole_window": False, "offline_reports": len(inside)}))
    return out


def stuck_spans(samples: list[Sample], *, min_duration: timedelta, max_gap: timedelta
                ) -> list[Span]:
    """Identical consecutive values lasting at least min_duration (std = 0)."""
    out: list[Span] = []
    run: list[Sample] = []

    def flush() -> None:
        if len(run) > 1 and run[-1].t - run[0].t >= min_duration:
            out.append(Span(run[0].t, run[-1].t, 0, list(run)))

    for s in samples:
        if run and (not math.isclose(s.v, run[-1].v, rel_tol=0, abs_tol=1e-12) or
                    s.t - run[-1].t > max_gap):
            flush()
            run = []
        run.append(s)
    flush()
    return out


# ---- battery ------------------------------------------------------------------------------

def state_at(series: list[Sample], t: datetime, max_age: timedelta) -> float | None:
    """Last value at or before t, if not older than max_age (series sorted by time)."""
    lo, hi = 0, len(series)
    while lo < hi:
        mid = (lo + hi) // 2
        if series[mid].t <= t:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    s = series[lo - 1]
    return s.v if t - s.t <= max_age else None


def voltage_drop_spans(voltage: list[Sample], ac: list[Sample], charging: list[Sample], *,
                       drop_v: float, window: timedelta, max_gap: timedelta,
                       settle: timedelta = timedelta(minutes=10)) -> list[Span]:
    """Voltage falling by >= drop_v within `window` while mains (AC) is normal and the
    battery is not charging (and not within `settle` after a charge ended, when the voltage
    naturally relaxes from absorption to float). Overlapping detections are merged."""
    hits: list[tuple[datetime, datetime]] = []
    seg: deque[Sample] = deque()   # samples of the current AC-normal, not-charging segment
    maxq: deque[Sample] = deque()  # monotonic queue for the window maximum
    last_charge: datetime | None = None
    prev: Sample | None = None
    for s in voltage:
        ac_on = state_at(ac, s.t, max_gap)
        chg = state_at(charging, s.t, max_gap)
        if chg == 1:
            last_charge = s.t
        usable = ac_on == 1 and chg != 1 and \
            (last_charge is None or s.t - last_charge > settle)
        if not usable or (prev is not None and s.t - prev.t > max_gap):
            seg.clear()
            maxq.clear()
        prev = s
        if not usable:
            continue
        seg.append(s)
        while maxq and maxq[-1].v <= s.v:
            maxq.pop()
        maxq.append(s)
        while seg and s.t - seg[0].t > window:
            old = seg.popleft()
            if maxq and maxq[0] is old:
                maxq.popleft()
        if maxq and maxq[0].v - s.v >= drop_v:
            # start at the last sample still near the maximum (ignores an earlier noise peak)
            top = maxq[0].v - drop_v * 0.1
            begin = next(x.t for x in reversed(seg) if x.v >= top)
            hits.append((begin, s.t))
    merged: list[list[datetime]] = []
    for a, b in hits:
        if merged and a <= merged[-1][1] + max_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [Span(a, b, 0, [v for v in voltage if a <= v.t <= b]) for a, b in merged]


def charge_sessions(ac: list[Sample], charging: list[Sample], end: datetime, *,
                    max_gap: timedelta, start_within: timedelta = timedelta(minutes=30)
                    ) -> list[Span]:
    """After every mains restoration (AC 0 -> 1): the charging session that follows.
    Span = [restoration, charging finished]; open_at_end when still charging at window end;
    detail["started"] False when charging never started within `start_within`."""
    out = []
    for prev, cur in zip(ac, ac[1:], strict=False):
        if not (prev.v == 0 and cur.v == 1):
            continue
        restored = cur.t
        on = [c for c in charging if restored <= c.t <= restored + start_within and c.v == 1]
        if not on:
            out.append(Span(restored, restored, 0, [], detail={"started": False}))
            continue
        finished = next((c.t for c in charging if c.t > on[0].t and c.v == 0), None)
        if finished is None:
            last = charging[-1].t if charging else restored
            out.append(Span(restored, last, 0, [], open_at_end=end - last <= max_gap,
                            detail={"started": True, "finished": False}))
        else:
            out.append(Span(restored, finished, 0, [], detail={"started": True,
                                                               "finished": True}))
    return out
