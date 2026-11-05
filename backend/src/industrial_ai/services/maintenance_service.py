"""Maintenance analysis: create, run in background, persist events and results."""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from industrial_ai.analytics.rules import (
    MetricAssessment,
    combine_levels,
    coverage_confidence,
    recommendations_for,
)
from industrial_ai.core.errors import AppError
from industrial_ai.core.security import CallerContext
from industrial_ai.schemas.analysis import (
    AnalysisAccepted,
    AnalysisCreate,
    AnalysisResult,
    AnalysisStatus,
    AnalysisType,
    DataGap,
    ErrorInfo,
)
from industrial_ai.schemas.common import HealthLevel
from industrial_ai.schemas.events import StreamEvent
from industrial_ai.services.equipment_service import EquipmentService
from industrial_ai.storage.base import (
    AnalysisRecord,
    AnalysisStore,
    EquipmentCatalog,
    StorageUnavailableError,
)
from industrial_ai.workers.analysis_worker import AnalysisRunner

logger = logging.getLogger(__name__)


class MaintenanceService:
    def __init__(
        self,
        catalog: EquipmentCatalog,
        equipment: EquipmentService,
        store: AnalysisStore,
        runner: AnalysisRunner,
        *,
        max_range_days: int,
        max_equipment: int,
        timeout_seconds: float,
    ) -> None:
        self._catalog = catalog
        self._equipment = equipment
        self._store = store
        self._runner = runner
        self._max_range = timedelta(days=max_range_days)
        self._max_equipment = max_equipment
        self._timeout = timeout_seconds

    # ---- create -------------------------------------------------------------------------

    async def create(
        self, request: AnalysisCreate, caller: CallerContext, idempotency_key: str | None
    ) -> AnalysisAccepted:
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        await self._validate(request)

        now = datetime.now(UTC)
        result = AnalysisResult(
            analysis_id=f"ma_{uuid.uuid4().hex}",
            status=AnalysisStatus.QUEUED,
            analysis_type=request.analysis_type,
            site_id=request.site_id,
            equipment_ids=request.equipment_ids,
            time_range=request.time_range,
            created_at=now,
        )
        record, created = await self._store.create(AnalysisRecord(
            result=result, request_fingerprint=fingerprint,
            idempotency_key=idempotency_key, created_by=caller.user_id,
        ))
        if not created:
            if record.request_fingerprint != fingerprint:
                raise AppError("idempotency_conflict",
                               "Idempotency-Key 已用于不同的请求内容", status_code=409)
            return AnalysisAccepted(analysis_id=record.result.analysis_id,
                                    status=record.result.status)

        logger.info("analysis created", extra={"fields": {
            "analysis_id": result.analysis_id, "analysis_type": request.analysis_type,
            "user_id": caller.user_id, "equipment_count": len(request.equipment_ids)}})
        await self._store.append_event(result.analysis_id, "stage", _stage("queued", "已排队", 0.0))
        self._runner.submit(result.analysis_id, self.run(result.analysis_id))
        return AnalysisAccepted(analysis_id=result.analysis_id, status=result.status)

    async def _validate(self, request: AnalysisCreate) -> None:
        if request.time_range.end - request.time_range.start > self._max_range:
            raise AppError("time_range_too_large",
                           f"分析时间范围不能超过 {self._max_range.days} 天", status_code=422)
        if len(set(request.equipment_ids)) != len(request.equipment_ids):
            raise AppError("duplicate_equipment", "equipment_ids 存在重复项", status_code=422)
        if len(request.equipment_ids) > self._max_equipment:
            raise AppError("too_many_equipment",
                           f"单次分析设备数不能超过 {self._max_equipment}", status_code=422)

        missing, mismatched = [], []
        for equipment_id in request.equipment_ids:
            item = await self._catalog.get(request.site_id, equipment_id)
            if item is None:
                missing.append(equipment_id)
            elif (request.analysis_type is not AnalysisType.GENERAL
                  and item.category != request.analysis_type.value):
                mismatched.append(equipment_id)
        if missing:
            raise AppError("equipment_not_found", "设备不存在", status_code=404,
                           details={"equipment_ids": missing})
        if mismatched:
            raise AppError("equipment_type_mismatch", "设备类别与分析类型不匹配",
                           status_code=422, details={"equipment_ids": mismatched})

    # ---- read ---------------------------------------------------------------------------

    async def get(self, analysis_id: str) -> AnalysisResult:
        record = await self._store.get(analysis_id)
        if record is None:
            raise AppError("analysis_not_found", "分析任务不存在", status_code=404)
        return record.result

    async def stream_events(
        self, analysis_id: str, after_seq: int, heartbeat_seconds: float
    ) -> AsyncIterator[StreamEvent | None]:
        """Yield stored events after `after_seq` until `done`; yield None as a heartbeat."""
        last = after_seq
        while True:
            events = await self._store.wait_for_events(analysis_id, last, heartbeat_seconds)
            if not events:
                record = await self._store.get(analysis_id)
                if record is None or record.result.status.is_terminal:
                    return  # resumed after `done`: nothing more will arrive
                yield None
                continue
            for item in events:
                yield item
                last = item.seq
                if item.event == "done":
                    return

    # ---- run ----------------------------------------------------------------------------

    async def run(self, analysis_id: str) -> None:
        record = await self._store.get(analysis_id)
        if record is None:
            return
        result = record.result.model_copy(update={"status": AnalysisStatus.RUNNING})
        await self._store.save_result(result)
        try:
            async with asyncio.timeout(self._timeout):
                final = await self._execute(result)
        except StorageUnavailableError:
            final = _failed(result, "timeseries_unavailable", "时序数据暂不可用", retryable=True)
        except TimeoutError:
            final = _failed(result, "analysis_timeout", "分析超时", retryable=True)
        except asyncio.CancelledError:
            await self._finish(_failed(result, "service_shutdown", "服务停止，分析中断",
                                       retryable=True))
            raise
        except Exception:
            logger.exception("analysis failed", extra={"fields": {"analysis_id": analysis_id}})
            final = _failed(result, "internal_error", "分析失败")
        await self._finish(final)

    async def _execute(self, result: AnalysisResult) -> AnalysisResult:
        aid = result.analysis_id
        await self._store.append_event(
            aid, "stage", _stage("loading_data", "正在获取设备数据", 0.1)
        )

        assessments: list[MetricAssessment] = []
        gaps: list[DataGap] = []
        for equipment_id in result.equipment_ids:
            equipment = await self._catalog.get(result.site_id, equipment_id)
            if equipment is None or not equipment.metrics:
                gaps.append(DataGap(equipment_id=equipment_id, reason="no_metrics",
                                    detail="设备不存在或未配置监测指标"))
                continue
            assessments.extend(await self._equipment.assess(equipment, result.time_range))

        await self._store.append_event(
            aid, "stage", _stage("evaluating_rules", "正在执行规则判断", 0.6)
        )
        levels = [a.level for a in assessments]
        if gaps:
            levels.append(HealthLevel.INSUFFICIENT_DATA)
        overall = combine_levels(levels)
        gaps = gaps + [g for a in assessments for g in a.gaps]

        await self._store.append_event(
            aid, "stage", _stage("composing_result", "正在整理分析结果", 0.9)
        )
        return result.model_copy(update={
            "status": AnalysisStatus.PARTIAL if gaps else AnalysisStatus.COMPLETED,
            "health_level": overall,
            "risk_summary": _risk_summary(assessments, overall),
            "evidence": [e for a in assessments for e in a.evidence],
            "recommendations": recommendations_for(assessments),
            "data_gaps": gaps,
            "confidence": coverage_confidence(assessments, overall),
            "generated_at": datetime.now(UTC),
        })

    async def _finish(self, final: AnalysisResult) -> None:
        await self._store.save_result(final)
        if final.error is not None:
            await self._store.append_event(final.analysis_id, "error", final.error.model_dump())
        await self._store.append_event(final.analysis_id, "result", final.model_dump(mode="json"))
        await self._store.append_event(final.analysis_id, "done", {"status": final.status})


def _stage(stage: str, message: str, progress: float) -> dict:
    return {"stage": stage, "message": message, "progress": progress}


def _failed(result: AnalysisResult, code: str, message: str, *, retryable: bool = False
            ) -> AnalysisResult:
    return result.model_copy(update={
        "status": AnalysisStatus.FAILED,
        "error": ErrorInfo(code=code, message=message, retryable=retryable),
        "generated_at": datetime.now(UTC),
    })


def _risk_summary(assessments: list[MetricAssessment], overall: HealthLevel) -> str:
    inspect = sum(a.level is HealthLevel.INSPECT for a in assessments)
    watch = sum(a.level is HealthLevel.WATCH for a in assessments)
    unknown = sum(a.level is HealthLevel.INSUFFICIENT_DATA for a in assessments)
    if overall is HealthLevel.INSPECT:
        text = f"{inspect} 项指标连续超出报警限值，建议人工核查"
    elif overall is HealthLevel.WATCH:
        text = f"{watch} 项指标出现超限样本，建议加强监视"
    elif overall is HealthLevel.INSUFFICIENT_DATA:
        return "数据或判定阈值不足，无法给出健康结论"
    else:
        return "所有指标均在配置限值内"
    if unknown:
        text += f"；另有 {unknown} 项指标数据或阈值不足"
    return text
