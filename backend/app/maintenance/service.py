"""Synchronous maintenance analysis called by the main server
(POST /internal/v1/maintenance/analyses, docs/项目规划/设计文档.md §3.1 / §4.2).

1. idempotency: the same Idempotency-Key (or analysis_id) returns the same result; concurrent
   duplicates share one execution; the same key with a different request is a conflict
2. wait (bounded) until the local MQTT consumption watermark reaches mqtt_watermark
3. rules per equipment (app/maintenance/detect.py) + history (work-order copy or MES API)
4. aggregate: quality gate -> insufficient_data, else inspect > watch > normal; top 3
   findings; one merged limitation; up to 3 preset actions from the catalogue
5. explanation by the model (primary action chosen among the candidates), else template
Nothing is stored: the main server owns results and work orders.
"""

import asyncio
import hashlib
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.db.work_orders import WorkOrder, WorkOrderStore
from app.maintenance.actions import ActionCatalog
from app.maintenance.detect import Detection, Detector, Gap, Hit, window_text
from app.maintenance.narrator import InvalidNarration, Narrator
from app.maintenance.schemas import (
    AnalysisError,
    AnalysisRequest,
    AnalysisResult,
    DataSnapshot,
    Finding,
    Limitation,
    ModelVersion,
    Versions,
    Window,
)
from app.stations.model import Station
from app.stations.service import StationService
from app.stations.store import StationStore
from app.timeseries.iotdb import TimeSeriesError

logger = logging.getLogger(__name__)
LEVEL_TEXT = {"inspect": "建议检修", "watch": "需关注"}


class HistorySource:
    """De-identified history of earlier events: the main server's history API when
    MES_API_BASE_URL is set, otherwise the work-order copy pushed to this service."""

    def __init__(self, store: WorkOrderStore, s: Settings) -> None:
        self.store, self.s = store, s

    async def recent(self, site: str, equipment: list[str], start: datetime, end: datetime
                     ) -> list[WorkOrder]:
        if self.s.mes_api_base_url:
            return await self._remote(site, equipment, start, end)
        out: list[WorkOrder] = []
        for eq in equipment:
            out += await self.store.list(site, eq, start, end, 100)
        return out

    async def _remote(self, site: str, equipment: list[str], start: datetime, end: datetime
                      ) -> list[WorkOrder]:
        headers = {}
        if self.s.mes_service_token:
            headers["Authorization"] = f"Bearer {self.s.mes_service_token.get_secret_value()}"
        out: list[WorkOrder] = []
        async with httpx.AsyncClient(base_url=self.s.mes_api_base_url, timeout=10,
                                     headers=headers) as client:
            for eq in equipment:
                r = await client.get("/internal/v1/maintenance/history", params={
                    "site_id": site, "equipment_id": eq, "from": start.isoformat(),
                    "to": end.isoformat(), "limit": 50})
                r.raise_for_status()
                out += [WorkOrder.model_validate(i) for i in r.json().get("items", [])]
        return out


class MaintenanceService:
    def __init__(self, stations: StationStore, data: StationService, history: HistorySource,
                 narrator: Narrator, catalog: ActionCatalog, s: Settings,
                 model_name: str | None) -> None:
        self.stations, self.data, self.history = stations, data, history
        self.narrator, self.catalog, self.s = narrator, catalog, s
        self.tz = ZoneInfo(s.analysis_timezone)
        self.detector = Detector(data.store, s, self.tz)
        self.model_name = model_name or "template-only"
        self._cache: dict[str, tuple[str, float, asyncio.Future]] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    @property
    def versions(self) -> Versions:
        return Versions(rule_set=self.s.maintenance_rule_set_version,
                        action_catalog=self.catalog.version,
                        prompt=self.s.maintenance_narrator_prompt_version,
                        model=ModelVersion(name=self.model_name,
                                           precision=self.s.llm_model_precision,
                                           revision=self.s.llm_model_revision))

    # ---- idempotency ---------------------------------------------------------------------

    async def analyze(self, req: AnalysisRequest, idempotency_key: str | None = None
                      ) -> AnalysisResult:
        key = idempotency_key or req.analysis_id
        fingerprint = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
        async with self._lock:
            now = time.monotonic()
            self._cache = {k: v for k, v in self._cache.items()
                           if now - v[1] < self.s.analysis_idempotency_ttl_seconds}
            cached = self._cache.get(key)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise AnalysisError(409, "idempotency_conflict",
                                        "the same key was used for a different request")
                future = cached[2]
            else:
                future = asyncio.get_running_loop().create_future()
                self._cache[key] = (fingerprint, now, future)
                task = asyncio.create_task(self._fill(key, future, req))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        return await asyncio.shield(future)

    async def _fill(self, key: str, future: asyncio.Future, req: AnalysisRequest) -> None:
        try:
            future.set_result(await self._analyze(req))
        except Exception as exc:
            self._cache.pop(key, None)  # failures are not cached: the caller may retry
            future.set_exception(exc)
            future.exception()  # mark retrieved; awaiting callers still receive it

    # ---- one analysis --------------------------------------------------------------------

    async def _wait_watermark(self, requested: str | None) -> None:
        if requested is None:
            return
        target = int(requested)
        deadline = time.monotonic() + self.s.analysis_watermark_wait_seconds
        while True:
            try:
                local = await self.data.watermark()
            except TimeSeriesError as exc:
                raise AnalysisError(503, "timeseries_unavailable", str(exc)[:200], True) from exc
            if local is not None and local >= target:
                return
            if time.monotonic() >= deadline:
                raise AnalysisError(503, "watermark_not_reached",
                                    "local MQTT data has not caught up with the requested "
                                    "watermark", True, local_watermark=local,
                                    requested_watermark=requested)
            await asyncio.sleep(self.s.analysis_watermark_poll_ms / 1000)

    def _resolve(self, st: Station, req: AnalysisRequest) -> tuple[list, bool]:
        """(detectors to analyse, analyse backup power?)"""
        want = set(req.equipment_ids)
        known = set(st.equipment_ids())
        if want - known:
            raise AnalysisError(404, "equipment_not_found",
                                f"unknown equipment {sorted(want - known)} at {st.station_id}")
        detectors = [d for d in st.active_devices() if not want or d.device_id in want]
        backup = req.analysis_type in ("backup_power", "general") and \
            (not want or st.backup_power_id in want or req.analysis_type == "general")
        if req.analysis_type == "backup_power":
            detectors = []
        if req.analysis_type == "detector":
            backup = False
            if not detectors:
                raise AnalysisError(422, "no_detector", "no enabled detector to analyse")
        return detectors, backup

    async def assess(self, site_id: str, start: datetime, end: datetime,
                     analysis_type: str = "general", explain: bool = False) -> AnalysisResult:
        """Same rules for the assistant (no watermark, no idempotency cache; by default no
        model explanation, so an all-station overview does not cost one model call each)."""
        req = AnalysisRequest(analysis_id=f"assistant-{uuid.uuid4().hex[:12]}",
                              site_id=site_id, analysis_type=analysis_type,  # type: ignore[arg-type]
                              observation_window=Window(start=start, end=end),
                              history_lookback_days=self.s.analysis_history_lookback_days)
        return await self._analyze(req, explain=explain)

    async def _analyze(self, req: AnalysisRequest, explain: bool = True) -> AnalysisResult:
        st = await self.stations.get(req.site_id)
        if st is None:
            raise AnalysisError(404, "site_not_found", f"unknown site {req.site_id}")
        start, end = req.observation_window.start, req.observation_window.end
        if req.data_cutoff is not None:
            end = min(end, req.data_cutoff)
        if end <= start:
            raise AnalysisError(422, "empty_window", "data_cutoff is before the window start")
        if end - start > timedelta(days=self.s.analysis_max_window_days):
            raise AnalysisError(422, "window_too_long",
                                f"window is limited to {self.s.analysis_max_window_days} days")
        detectors, backup = self._resolve(st, req)
        await self._wait_watermark(req.mqtt_watermark)
        try:
            detections: list[Detection] = [await self.detector.detector(st, d, start, end)
                                           for d in detectors]
            if backup:
                detections.append(await self.detector.backup(st, start, end))
        except TimeSeriesError as exc:
            raise AnalysisError(503, "timeseries_unavailable", str(exc)[:200], True) from exc
        hits = [h for d in detections for h in d.hits]
        gaps = [g for d in detections for g in d.gaps]
        if hits:
            await self._history(req, st, [d.equipment_id for d in detections], end, hits, gaps)
        blocking = [g for g in gaps if g.blocking]
        # key metric: insufficient as soon as any analysed unit has no usable data
        if blocking:
            level = "insufficient_data"
        elif any(h.severity == "inspect" for h in hits):
            level = "inspect"
        elif hits:
            level = "watch"
        else:
            level = "normal"
        for h in sorted(hits, key=lambda h: h.rank):
            logger.info("analysis %s hit %s %s %s", req.analysis_id, h.rule_id, h.code,
                        h.message)
        top = sorted(hits, key=lambda h: h.rank)[:3] if level in ("watch", "inspect") else []
        findings = [Finding(rule_id=h.rule_id, code=h.code, severity=h.severity,  # type: ignore[arg-type]
                            metric=h.metric, observed=h.observed, baseline=h.baseline,
                            change_percent=h.change_percent,
                            window=window_text(h.start, h.end, self.tz), message=h.message,
                            source_refs=h.source_refs) for h in top]
        limitations = self._limitation(gaps)
        candidates = self.catalog.candidates(findings) if findings else []
        explanation, source, primary = await self._explain(st, req, level, findings,
                                                           limitations, candidates, detections,
                                                           explain)
        quality = [d.quality for d in detections]
        samples = sum(q.sample_count for q in quality)
        expected = sum(q.expected for q in quality) or 1
        completeness = round(min(1.0, samples / expected), 4)
        snapshot = DataSnapshot(
            mqtt_watermark=req.mqtt_watermark, window=Window(start=start, end=end),
            data_as_of=max((q.data_as_of for q in quality if q.data_as_of), default=None),
            sample_count=samples, completeness=completeness,
            missing_rate=round(1 - completeness, 4))
        return AnalysisResult(
            analysis_id=req.analysis_id, health_level=level,  # type: ignore[arg-type]
            explanation=explanation, explanation_source=source,  # type: ignore[arg-type]
            findings=findings,
            actions=self.catalog.build(candidates, primary) if level in ("watch", "inspect")
            else [],
            limitations=limitations, data_snapshot=snapshot, versions=self.versions,
            generated_at=datetime.now(UTC).astimezone(self.tz))

    async def _history(self, req: AnalysisRequest, st: Station, equipment: list[str],
                       end: datetime, hits: list[Hit], gaps: list[Gap]) -> None:
        if not req.history_lookback_days:
            return
        start = end - timedelta(days=req.history_lookback_days)
        try:
            past = await self.history.recent(st.station_id, equipment, start, end)
        except Exception as exc:
            logger.warning("analysis %s: history unavailable: %s", req.analysis_id, exc)
            gaps.append(Gap("history.unavailable", "历史工单与审核结论暂不可用",
                            "无法参考同设备历史事件"))
            return
        relevant = [w for w in past if w.health_level in ("watch", "inspect")]
        if len(relevant) >= 2:
            confirmed = sum(1 for w in relevant if w.review and w.review.outcome == "accurate")
            latest = max(relevant, key=lambda w: w.triggered_at)
            hits.append(Hit("HISTORY-REPEAT", "history.repeated_event", "watch", "events",
                            min(w.triggered_at for w in relevant), latest.triggered_at,
                            f"近 {req.history_lookback_days} 天同设备已有 {len(relevant)} 次"
                            f"维护事件，其中 {confirmed} 次经人工审核确认准确",
                            float(len(relevant)), None,
                            [f"wo:{w.event_id}" for w in relevant[:5]]))

    @staticmethod
    def _limitation(gaps: list[Gap]) -> list[Limitation]:
        if not gaps:
            return []
        ordered = sorted(gaps, key=lambda g: not g.blocking)
        texts = list(dict.fromkeys(g.text for g in ordered))
        affects = list(dict.fromkeys(g.affects for g in ordered))
        code = ordered[0].code if len(ordered) == 1 else ordered[0].code + "+"
        return [Limitation(code=code, text="；".join(texts), affects="；".join(affects))]

    async def _explain(self, st: Station, req: AnalysisRequest, level: str,
                       findings: list[Finding], limitations: list[Limitation],
                       candidates: list[str], detections: list[Detection],
                       explain: bool = True) -> tuple[str, str, str | None]:
        """(explanation, explanation_source, primary action)"""
        name = f"{st.station_id} {st.name}"
        if level == "insufficient_data":
            text = f"{name}：{limitations[0].text}，{limitations[0].affects}，本次无法给出维护判断。"
            if any(d.reported_alarm for d in detections):
                text += "窗口内设备曾上报报警或故障状态，请保持告警状态并安排现场核查。"
            return text, "template", None
        if level == "normal":
            text = f"{name}：观察窗口内未命中维护规则。"
            if limitations:
                text += f"注意：{limitations[0].text}。"
            return text, "template", None
        evidence = {"site": name, "analysis_type": req.analysis_type, "health_level": level,
                    "health_level_label": LEVEL_TEXT[level],
                    "findings": [f.model_dump(exclude={"source_refs"}) for f in findings],
                    "limitations": [lim.model_dump() for lim in limitations],
                    "candidate_actions": [{"action_code": c, "text": self.catalog.actions[c]}
                                          for c in candidates]}
        if self.s.maintenance_use_llm and explain:
            try:
                text, primary, _ = await self.narrator.narrate(evidence, candidates)
                return text, "model", primary
            except InvalidNarration as exc:
                logger.info("analysis %s: template explanation (%s)", req.analysis_id, exc)
        advice = f"建议优先{self.catalog.actions[candidates[0]]}。"
        for n in (2, 1):  # drop the second finding rather than cutting a sentence
            text = f"{name}：{LEVEL_TEXT[level]}。" + "；".join(f.message for f in findings[:n])
            if len(findings) > n:
                text += f"；另有 {len(findings) - n} 条依据"
            text += f"。{advice}"
            if len(text) <= self.s.maintenance_narrator_max_chars:
                break
        return text[:self.s.maintenance_narrator_max_chars], "template", candidates[0]
