"""Daily batch: analyse the previous calendar day, store records, explain them.

Idempotency key: (analysis_date, ANALYSIS_VERSION).
  * a running / finished batch for the key is returned instead of starting another one
  * a failed batch is retried in place (attempts + 1, same configuration snapshot)
  * force=True (manual re-run) creates a new revision with a fresh configuration snapshot;
    the current result of a day is its highest successful revision
Status: succeeded | partial (some stations / devices failed, the rest is stored) | failed.

`Scheduler` (inside the API process, one instance only): every day at ANALYSIS_RUN_AT it
analyses yesterday; on start it marks batches interrupted by a restart as failed and runs
missed days (ANALYSIS_CATCHUP_DAYS); failed batches are retried every
ANALYSIS_RETRY_MINUTES up to ANALYSIS_MAX_ATTEMPTS.
"""

import asyncio
import contextlib
import logging
from collections import Counter
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.analysis.analyzer import Params, StationAnalyzer
from app.analysis.explain import Explainer
from app.analysis.models import Batch, BatchStatus
from app.analysis.store import DONE, AnalysisStore
from app.config import Settings
from app.stations.model import Station
from app.stations.store import StationStore
from app.timeseries.iotdb import TimeSeriesStore

logger = logging.getLogger(__name__)


class AnalysisService:
    def __init__(self, store: AnalysisStore, stations: StationStore, ts: TimeSeriesStore,
                 explainer: Explainer, settings: Settings) -> None:
        self.store, self.stations, self.explainer = store, stations, explainer
        self.s = settings
        self.tz = ZoneInfo(settings.analysis_timezone)
        self.version = settings.analysis_version
        self.params = Params.from_settings(settings)
        self.analyzer = StationAnalyzer(ts, self.params, self.tz)
        self._create_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()   # one batch at a time
        self._tasks: dict[str, asyncio.Task] = {}

    # ---- time -----------------------------------------------------------------------------

    def window(self, day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, time(0), self.tz)
        end = datetime.combine(day + timedelta(days=1), time(0), self.tz)
        return start.astimezone(UTC), end.astimezone(UTC)

    def today(self, now: datetime | None = None) -> date:
        return (now or datetime.now(UTC)).astimezone(self.tz).date()

    def run_time(self, day: date) -> datetime:
        hh, mm = (int(x) for x in self.s.analysis_run_at.split(":"))
        return datetime.combine(day, time(hh, mm), self.tz).astimezone(UTC)

    # ---- requests -------------------------------------------------------------------------

    async def request(self, day: date, *, trigger: str = "manual", force: bool = False
                      ) -> tuple[Batch, bool]:
        """Start (or return) the batch of `day`. Returns (batch, started_now)."""
        if day >= self.today():
            raise ValueError("only finished calendar days can be analysed")
        async with self._create_lock:
            existing = await self.store.batches_for(day, self.version)
            running = [b for b in existing if b.status == BatchStatus.RUNNING]
            if running:
                return running[-1], False
            done = [b for b in existing if b.status in DONE]
            latest = existing[-1] if existing else None
            if done and not force:
                return max(done, key=lambda b: b.revision), False
            now = datetime.now(UTC)
            if latest is not None and latest.status == BatchStatus.FAILED and not force:
                attempts = latest.attempts + 1
                await self.store.update_batch(latest.batch_id, status=BatchStatus.RUNNING,
                                              attempts=attempts, trigger="retry"
                                              if trigger == "schedule" else trigger,
                                              started_at=None, finished_at=None, errors=[])
                batch = latest.model_copy(update={"status": BatchStatus.RUNNING,
                                                  "attempts": attempts})
            else:
                revision = (latest.revision + 1) if latest else 1
                start, end = self.window(day)
                batch = Batch(batch_id=f"B{day:%Y%m%d}-{self.version}-r{revision}",
                              analysis_date=day, version=self.version, revision=revision,
                              status=BatchStatus.RUNNING, trigger=trigger, attempts=1,
                              timezone=self.s.analysis_timezone, window_start=start,
                              window_end=end, created_at=now,
                              config_snapshot=await self._snapshot(start, end))
                await self.store.create_batch(batch)
        self._tasks[batch.batch_id] = asyncio.create_task(self._run(batch.batch_id),
                                                          name=f"analysis:{batch.batch_id}")
        return (await self.store.get_batch(batch.batch_id)) or batch, True

    async def wait(self, batch_id: str) -> Batch | None:
        task = self._tasks.get(batch_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        return await self.store.get_batch(batch_id)

    async def run_now(self, day: date, *, force: bool = False, trigger: str = "manual"
                      ) -> Batch | None:
        batch, _ = await self.request(day, trigger=trigger, force=force)
        return await self.wait(batch.batch_id)

    async def _snapshot(self, start: datetime, end: datetime) -> dict:
        stations = await self.stations.all()
        versions = await self.stations.versions()
        return {"captured_at": datetime.now(UTC).isoformat(), "version": self.version,
                "timezone": self.s.analysis_timezone,
                "window": {"start": start.isoformat(), "end": end.isoformat()},
                "params": {**asdict(self.params), "fault_level": self.params.fault_level.value},
                "stations": [s.model_dump(mode="json") for s in stations],
                "config_versions": {s.station_id: versions.get(s.station_id) for s in stations}}

    # ---- execution --------------------------------------------------------------------------

    async def _run(self, batch_id: str) -> None:
        async with self._run_lock:
            try:
                await self._execute(batch_id)
            except asyncio.CancelledError:
                await self._fail(batch_id, "interrupted (service stopped)")
                raise
            except Exception as exc:
                logger.exception("analysis batch %s failed", batch_id)
                await self._fail(batch_id, f"{type(exc).__name__}: {exc}"[:500])
            finally:
                self._tasks.pop(batch_id, None)

    async def _fail(self, batch_id: str, error: str) -> None:
        with contextlib.suppress(Exception):
            await self.store.update_batch(batch_id, status=BatchStatus.FAILED,
                                          finished_at=datetime.now(UTC),
                                          errors=[{"station_id": None, "device_id": None,
                                                   "error": error}])

    async def _execute(self, batch_id: str) -> None:
        batch = await self.store.get_batch(batch_id, snapshot=True)
        if batch is None or batch.config_snapshot is None:
            raise RuntimeError(f"batch {batch_id} not found")
        logger.info("analysis batch %s (%s) started", batch_id, batch.analysis_date)
        await self.store.update_batch(batch_id, started_at=datetime.now(UTC))
        await self.store.delete_records(batch_id)  # leftovers of a failed attempt
        stations = [Station.model_validate(s) for s in batch.config_snapshot["stations"]]
        gaps: list[dict] = []
        errors: list[dict] = []
        levels: Counter[str] = Counter()
        equipment: Counter[str] = Counter()
        methods: Counter[str] = Counter()
        analysed_devices = analysable = stations_ok = 0
        for station in stations:
            analysable += (len(station.active_devices()) if station.dose_configured else 0) + \
                (1 if station.battery_configured else 0)
            result = await self.analyzer.analyze(station, batch_id, batch.analysis_date,
                                                 batch.window_start, batch.window_end)
            gaps += result.gaps
            errors += result.errors
            analysed_devices += result.devices_analysed
            stations_ok += not any(e.get("device_id") is None for e in result.errors)
            for record in result.records:
                try:
                    record.explanation = await self.explainer.explain(record)
                    await self.store.save_record(record)
                except Exception as exc:
                    errors.append({"station_id": record.station_id,
                                   "device_id": record.device_id,
                                   "error": f"store: {type(exc).__name__}: {exc}"[:500]})
                    continue
                levels[record.level.value] += 1
                equipment[record.equipment_type.value] += 1
                methods[record.explanation.method] += 1
        if not errors:
            status = BatchStatus.SUCCEEDED
        elif analysed_devices == 0 and analysable > 0:
            status = BatchStatus.FAILED
        else:
            status = BatchStatus.PARTIAL
        counts = {"stations": len(stations), "stations_analysed": stations_ok,
                  "devices_analysable": analysable, "devices_analysed": analysed_devices,
                  "records": sum(levels.values()), "by_level": dict(levels),
                  "by_equipment": dict(equipment), "explanations": dict(methods)}
        await self.store.update_batch(batch_id, status=status, finished_at=datetime.now(UTC),
                                      counts=counts, config_gaps=gaps, errors=errors)
        logger.info("analysis batch %s finished: %s %s", batch_id, status.value, counts)

    # ---- status ---------------------------------------------------------------------------

    async def status(self, scheduler: "Scheduler | None" = None) -> dict:
        yesterday = self.today() - timedelta(days=1)
        return {"version": self.version, "timezone": self.s.analysis_timezone,
                "yesterday": yesterday.isoformat(),
                "schedule": {"enabled": scheduler is not None, "run_at": self.s.analysis_run_at,
                             "next_run_at": scheduler.next_run().isoformat()
                             if scheduler else None},
                "yesterday_batch": await self.store.current_batch(self.version, yesterday),
                "latest_batch": await self.store.current_batch(self.version),
                "running": await self.store.running_batches(),
                "explanations": {"llm": self.explainer.llm is not None,
                                 "model": self.explainer.model_name}}

    async def close(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(BaseException):
                await task


class Scheduler:
    def __init__(self, service: AnalysisService, tick_seconds: float = 60) -> None:
        self.svc, self.tick = service, tick_seconds

    def next_run(self, now: datetime | None = None) -> datetime:
        now = now or datetime.now(UTC)
        today = self.svc.today(now)
        run = self.svc.run_time(today)
        return run if run > now else self.svc.run_time(today + timedelta(days=1))

    async def recover(self) -> None:
        """Batches still 'running' were interrupted by a restart (single instance)."""
        for b in await self.svc.store.running_batches():
            if b.batch_id not in self.svc._tasks:
                await self.svc._fail(b.batch_id, "interrupted (service restart)")
                logger.warning("analysis batch %s was interrupted; will be retried", b.batch_id)

    def due_days(self, now: datetime | None = None) -> list[date]:
        """Yesterday (once today's run time has passed) and the missed days before it,
        oldest first."""
        now = now or datetime.now(UTC)
        today = self.svc.today(now)
        days = [today - timedelta(days=k)
                for k in range(max(1, self.svc.s.analysis_catchup_days), 0, -1)]
        if now < self.svc.run_time(today):
            days.remove(today - timedelta(days=1))
        return days

    async def check(self, now: datetime | None = None) -> list[str]:
        """Start whatever is due: missing days, retries of failed batches."""
        now = now or datetime.now(UTC)
        started = []
        retry_after = timedelta(minutes=self.svc.s.analysis_retry_minutes)
        for day in self.due_days(now):
            batches = await self.svc.store.batches_for(day, self.svc.version)
            latest = batches[-1] if batches else None
            if latest is None:
                trigger = "schedule" if day == self.svc.today(now) - timedelta(days=1) \
                    else "catchup"
            elif latest.status == BatchStatus.FAILED and \
                    latest.attempts < self.svc.s.analysis_max_attempts and \
                    (latest.finished_at is None or now - latest.finished_at >= retry_after):
                trigger = "retry"
            else:
                continue
            batch, created = await self.svc.request(day, trigger=trigger)
            if created:
                started.append(batch.batch_id)
        return started

    async def run(self) -> None:
        await self.recover()
        while True:
            try:
                await self.check()
            except Exception:
                logger.exception("analysis scheduler check failed")
            wait = min(self.tick, max(1.0, (self.next_run() - datetime.now(UTC))
                                      .total_seconds() + 1))
            await asyncio.sleep(wait)
