"""Equipment health assessment shared by the overview and maintenance analyses."""

import asyncio
from datetime import UTC, datetime, timedelta

from industrial_ai.analytics.rules import (
    MetricAssessment,
    RuleConfig,
    assess_metric,
    combine_levels,
)
from industrial_ai.core.errors import AppError
from industrial_ai.schemas.common import HealthLevel, TimeRange
from industrial_ai.schemas.equipment import Equipment, EquipmentHealth, HealthOverview
from industrial_ai.storage.base import EquipmentCatalog, StorageUnavailableError, TimeSeriesStore

_MAX_CONCURRENT_QUERIES = 8


def timeseries_unavailable() -> AppError:
    return AppError("timeseries_unavailable", "时序数据暂不可用", status_code=503, retryable=True)


class EquipmentService:
    def __init__(
        self,
        catalog: EquipmentCatalog,
        timeseries: TimeSeriesStore,
        rules: RuleConfig,
        health_window_hours: int,
    ) -> None:
        self._catalog = catalog
        self._timeseries = timeseries
        self._rules = rules
        self._window = timedelta(hours=health_window_hours)
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_QUERIES)

    async def assess(self, equipment: Equipment, time_range: TimeRange) -> list[MetricAssessment]:
        """Evaluate every metric of one equipment. Raises StorageUnavailableError."""

        async def one(metric_name: str) -> list:
            async with self._semaphore:
                return await self._timeseries.query(
                    equipment.site_id, equipment.equipment_id, metric_name, time_range
                )

        series = await asyncio.gather(*(one(m.name) for m in equipment.metrics))
        return [
            assess_metric(equipment.equipment_id, metric, points, time_range, self._rules)
            for metric, points in zip(equipment.metrics, series, strict=True)
        ]

    async def health_overview(self, site_id: str, category: str | None) -> HealthOverview:
        now = datetime.now(UTC)
        window = TimeRange(start=now - self._window, end=now)
        equipment = await self._catalog.list_equipment(site_id, category)
        try:
            assessed = await asyncio.gather(*(self.assess(e, window) for e in equipment))
        except StorageUnavailableError as exc:
            raise timeseries_unavailable() from exc

        items = [_to_health(e, a) for e, a in zip(equipment, assessed, strict=True)]
        counts = {level: 0 for level in HealthLevel}
        for item in items:
            counts[item.health_level] += 1
        return HealthOverview(
            site_id=site_id, category=category, window_start=window.start, window_end=window.end,
            generated_at=now, counts=counts, items=items,
        )


def _to_health(equipment: Equipment, assessments: list[MetricAssessment]) -> EquipmentHealth:
    reasons: list[str] = []
    if not assessments:
        reasons.append("未配置监测指标")
    for a in assessments:
        reasons.extend(f"{a.metric.name}: {gap.detail}" for gap in a.gaps)
        if a.level in (HealthLevel.INSPECT, HealthLevel.WATCH):
            reasons.append(f"{a.metric.name}: {a.evidence[-1].observation}")

    ratios = [a.completeness for a in assessments if a.completeness is not None]
    seen = [a.stats.last_timestamp for a in assessments if a.stats.last_timestamp is not None]
    return EquipmentHealth(
        equipment_id=equipment.equipment_id,
        name=equipment.name,
        category=equipment.category,
        health_level=combine_levels(a.level for a in assessments),
        data_completeness=round(sum(ratios) / len(ratios), 4) if ratios else None,
        last_seen=max(seen) if seen else None,
        reasons=reasons,
    )
