"""Storage of analysis batches, records and events.

`PgAnalysisStore`: openGauss tables analysis_batch / analysis_record / analysis_event
(migration 002). `MemoryAnalysisStore`: same interface for tests and database-free demos.
A record and its events are written in one transaction, so a reader never sees half a
record; records of a running batch are hidden from the "current result" queries.
"""

import copy
import json
from datetime import date
from typing import Any, Protocol

from app.analysis.models import (
    SEVERITY,
    Batch,
    BatchStatus,
    Event,
    Explanation,
    Record,
)
from app.db.pool import PgPool

DONE = (BatchStatus.SUCCEEDED, BatchStatus.PARTIAL)


class AnalysisStore(Protocol):
    async def ping(self) -> None: ...
    async def close(self) -> None: ...
    async def batches_for(self, day: date, version: str) -> list[Batch]: ...
    async def create_batch(self, batch: Batch) -> None: ...
    async def update_batch(self, batch_id: str, **fields: Any) -> None: ...
    async def save_record(self, record: Record) -> None: ...
    async def delete_records(self, batch_id: str) -> None: ...
    async def set_explanation(self, record_id: str, explanation: Explanation) -> None: ...
    async def get_batch(self, batch_id: str, snapshot: bool = False) -> Batch | None: ...
    async def list_batches(self, version: str | None, date_from: date | None,
                           date_to: date | None, status: str | None, limit: int,
                           offset: int) -> tuple[list[Batch], int]: ...
    async def current_batch(self, version: str, day: date | None = None) -> Batch | None: ...
    async def running_batches(self) -> list[Batch]: ...
    async def list_records(self, batch_id: str, filters: dict, limit: int, offset: int
                           ) -> tuple[list[Record], int]: ...
    async def get_record(self, record_id: str) -> Record | None: ...


def _mark_current(batches: list[Batch], all_batches: list[Batch]) -> list[Batch]:
    best: dict[tuple, int] = {}
    for b in all_batches:
        if b.status in DONE:
            key = (b.analysis_date, b.version)
            best[key] = max(best.get(key, 0), b.revision)
    for b in batches:
        b.is_current = b.status in DONE and best.get((b.analysis_date, b.version)) == b.revision
    return batches


def _match(r: Record, f: dict) -> bool:
    return all(getattr(r, k) == v for k, v in f.items() if v is not None)


# ---- memory ---------------------------------------------------------------------------------

class MemoryAnalysisStore:
    def __init__(self) -> None:
        self.batches: dict[str, Batch] = {}
        self.records: dict[str, Record] = {}

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def batches_for(self, day: date, version: str) -> list[Batch]:
        found = [copy.deepcopy(b) for b in self.batches.values()
                 if b.analysis_date == day and b.version == version]
        return _mark_current(sorted(found, key=lambda b: b.revision), list(self.batches.values()))

    async def create_batch(self, batch: Batch) -> None:
        if any(b.analysis_date == batch.analysis_date and b.version == batch.version and
               b.revision == batch.revision for b in self.batches.values()):
            raise ValueError("duplicate batch revision")
        self.batches[batch.batch_id] = batch.model_copy(deep=True)

    async def update_batch(self, batch_id: str, **fields: Any) -> None:
        b = self.batches[batch_id]
        self.batches[batch_id] = b.model_copy(update=fields)

    async def save_record(self, record: Record) -> None:
        self.records[record.record_id] = record.model_copy(deep=True)

    async def delete_records(self, batch_id: str) -> None:
        for rid in [r.record_id for r in self.records.values() if r.batch_id == batch_id]:
            del self.records[rid]

    async def set_explanation(self, record_id: str, explanation: Explanation) -> None:
        self.records[record_id].explanation = explanation.model_copy(deep=True)

    async def get_batch(self, batch_id: str, snapshot: bool = False) -> Batch | None:
        b = self.batches.get(batch_id)
        if b is None:
            return None
        out = b.model_copy(deep=True, update={} if snapshot else {"config_snapshot": None})
        return _mark_current([out], list(self.batches.values()))[0]

    async def list_batches(self, version, date_from, date_to, status, limit, offset):
        items = [b.model_copy(deep=True, update={"config_snapshot": None})
                 for b in self.batches.values()
                 if (version is None or b.version == version)
                 and (date_from is None or b.analysis_date >= date_from)
                 and (date_to is None or b.analysis_date <= date_to)
                 and (status is None or b.status == status)]
        items.sort(key=lambda b: (b.analysis_date, b.revision, b.created_at), reverse=True)
        return _mark_current(items[offset:offset + limit], list(self.batches.values())), \
            len(items)

    async def current_batch(self, version: str, day: date | None = None) -> Batch | None:
        done = [b for b in self.batches.values() if b.version == version and b.status in DONE
                and (day is None or b.analysis_date == day)]
        if not done:
            return None
        best = max(done, key=lambda b: (b.analysis_date, b.revision))
        return await self.get_batch(best.batch_id)

    async def running_batches(self) -> list[Batch]:
        return [b.model_copy(deep=True, update={"config_snapshot": None})
                for b in self.batches.values() if b.status == BatchStatus.RUNNING]

    async def list_records(self, batch_id, filters, limit, offset):
        items = [r for r in self.records.values() if r.batch_id == batch_id and _match(r, filters)]
        items.sort(key=lambda r: (-SEVERITY[r.level], r.station_id, r.device_id))
        return [r.model_copy(deep=True, update={"events": None})
                for r in items[offset:offset + limit]], len(items)

    async def get_record(self, record_id: str) -> Record | None:
        r = self.records.get(record_id)
        return r.model_copy(deep=True) if r else None


# ---- openGauss -------------------------------------------------------------------------------

_BATCH_COLS = ("batch_id, analysis_date, version, revision, status, trigger_type, attempts, "
               "time_zone, window_start, window_end, counts, config_gaps, errors, created_at, "
               "started_at, finished_at")
_RECORD_COLS = ("record_id, batch_id, analysis_date, station_id, station_name, equipment_type, "
                "device_id, device_name, level_code, title, sources, event_count, data_status, "
                "day_stats, explanation, created_at")
_EVENT_COLS = ("event_id, record_id, code, kind, title, level_code, source_type, category, "
               "start_time, end_time, duration_s, open_at_start, open_at_end, sample_count, "
               "expected_samples, completeness, unit, min_value, max_value, mean_value, "
               "std_value, corroborated_by_source, rule_info, comparisons, context, "
               "value_series, values_truncated")
_BATCH_FIELDS = {"status": "status", "attempts": "attempts", "started_at": "started_at",
                 "finished_at": "finished_at", "counts": "counts", "config_gaps": "config_gaps",
                 "errors": "errors", "trigger": "trigger_type"}
_JSON_FIELDS = {"counts", "config_gaps", "errors"}


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _batch(row: dict, snapshot: bool = False) -> Batch:
    return Batch(batch_id=row["batch_id"], analysis_date=row["analysis_date"],
                 version=row["version"], revision=row["revision"], status=row["status"],
                 trigger=row["trigger_type"], attempts=row["attempts"],
                 timezone=row["time_zone"], window_start=row["window_start"],
                 window_end=row["window_end"], counts=json.loads(row["counts"] or "{}"),
                 config_gaps=json.loads(row["config_gaps"] or "[]"),
                 errors=json.loads(row["errors"] or "[]"), created_at=row["created_at"],
                 started_at=row["started_at"], finished_at=row["finished_at"],
                 config_snapshot=json.loads(row["config_snapshot"] or "null")
                 if snapshot else None,
                 is_current=bool(row.get("is_current")))


def _record(row: dict) -> Record:
    expl = json.loads(row["explanation"]) if row["explanation"] else None
    return Record(record_id=row["record_id"], batch_id=row["batch_id"],
                  analysis_date=row["analysis_date"], station_id=row["station_id"],
                  station_name=row["station_name"], equipment_type=row["equipment_type"],
                  device_id=row["device_id"], device_name=row["device_name"],
                  level=row["level_code"], title=row["title"],
                  sources=json.loads(row["sources"]), event_count=row["event_count"],
                  data_status=row["data_status"], day_stats=json.loads(row["day_stats"] or "{}"),
                  explanation=expl, created_at=row["created_at"])


def _event(row: dict) -> Event:
    return Event(event_id=row["event_id"], record_id=row["record_id"], code=row["code"],
                 kind=row["kind"], title=row["title"], level=row["level_code"],
                 source=row["source_type"], category=row["category"], start=row["start_time"],
                 end=row["end_time"], duration_s=row["duration_s"],
                 open_at_start=row["open_at_start"], open_at_end=row["open_at_end"],
                 sample_count=row["sample_count"], expected_samples=row["expected_samples"],
                 completeness=row["completeness"], unit=row["unit"], min=row["min_value"],
                 max=row["max_value"], mean=row["mean_value"], std=row["std_value"],
                 corroborated_by_source=row["corroborated_by_source"],
                 rule=json.loads(row["rule_info"] or "{}"),
                 comparisons=json.loads(row["comparisons"] or "{}"),
                 context=json.loads(row["context"] or "{}"),
                 values=json.loads(row["value_series"] or "[]"),
                 values_truncated=row["values_truncated"])


_CURRENT = ("(b.status IN ('succeeded', 'partial') AND b.revision = (SELECT MAX(x.revision) "
            "FROM analysis_batch x WHERE x.analysis_date = b.analysis_date AND x.version = "
            "b.version AND x.status IN ('succeeded', 'partial'))) AS is_current")


class PgAnalysisStore:
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def _all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        async with await self._pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchall()

    async def ping(self) -> None:
        await self._all("SELECT 1 FROM analysis_batch WHERE 1 = 0")

    async def close(self) -> None:
        return None

    async def batches_for(self, day: date, version: str) -> list[Batch]:
        rows = await self._all(f"SELECT {_BATCH_COLS}, {_CURRENT} FROM analysis_batch b "
                               "WHERE analysis_date = %s AND version = %s ORDER BY revision",
                               (day, version))
        return [_batch(r) for r in rows]

    async def create_batch(self, b: Batch) -> None:
        async with await self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO analysis_batch (batch_id, analysis_date, version, revision, status, "
                "trigger_type, attempts, time_zone, window_start, window_end, config_snapshot, "
                "counts, config_gaps, errors, created_at, started_at, finished_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (b.batch_id, b.analysis_date, b.version, b.revision, b.status.value, b.trigger,
                 b.attempts, b.timezone, b.window_start, b.window_end,
                 _dumps(b.config_snapshot), _dumps(b.counts), _dumps(b.config_gaps),
                 _dumps(b.errors), b.created_at, b.started_at, b.finished_at))

    async def update_batch(self, batch_id: str, **fields: Any) -> None:
        sets, params = [], []
        for k, v in fields.items():
            sets.append(f"{_BATCH_FIELDS[k]} = %s")
            params.append(_dumps(v) if k in _JSON_FIELDS else
                          v.value if isinstance(v, BatchStatus) else v)
        async with await self._pool.connection() as conn:
            await conn.execute(f"UPDATE analysis_batch SET {', '.join(sets)} WHERE batch_id = %s",
                               [*params, batch_id])

    async def save_record(self, r: Record) -> None:
        async with await self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                f"INSERT INTO analysis_record ({_RECORD_COLS}, explanation_method, confidence) "
                f"VALUES ({', '.join(['%s'] * 18)})",
                (r.record_id, r.batch_id, r.analysis_date, r.station_id, r.station_name,
                 r.equipment_type.value, r.device_id, r.device_name, r.level.value, r.title,
                 _dumps([s.value for s in r.sources]), r.event_count, r.data_status,
                 _dumps(r.day_stats),
                 r.explanation.model_dump_json() if r.explanation else None, r.created_at,
                 r.explanation.method if r.explanation else None,
                 r.explanation.confidence if r.explanation else None))
            for e in r.events or []:
                d = e.model_dump(mode="json")
                await conn.execute(
                    f"INSERT INTO analysis_event ({_EVENT_COLS}) VALUES "
                    f"({', '.join(['%s'] * 27)})",
                    (e.event_id, r.record_id, e.code, e.kind, e.title, e.level.value,
                     e.source.value, e.category.value, e.start, e.end, e.duration_s,
                     e.open_at_start, e.open_at_end, e.sample_count, e.expected_samples,
                     e.completeness, e.unit, e.min, e.max, e.mean, e.std,
                     e.corroborated_by_source, _dumps(d["rule"]), _dumps(d["comparisons"]),
                     _dumps(d["context"]), _dumps(d["values"] or []), e.values_truncated))

    async def delete_records(self, batch_id: str) -> None:
        async with await self._pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM analysis_event WHERE record_id IN (SELECT record_id "
                               "FROM analysis_record WHERE batch_id = %s)", (batch_id,))
            await conn.execute("DELETE FROM analysis_record WHERE batch_id = %s", (batch_id,))

    async def set_explanation(self, record_id: str, explanation: Explanation) -> None:
        async with await self._pool.connection() as conn:
            await conn.execute(
                "UPDATE analysis_record SET explanation = %s, explanation_method = %s, "
                "confidence = %s WHERE record_id = %s",
                (explanation.model_dump_json(), explanation.method, explanation.confidence,
                 record_id))

    async def get_batch(self, batch_id: str, snapshot: bool = False) -> Batch | None:
        rows = await self._all(f"SELECT {_BATCH_COLS}, config_snapshot, {_CURRENT} "
                               "FROM analysis_batch b WHERE batch_id = %s", (batch_id,))
        return _batch(rows[0], snapshot) if rows else None

    async def list_batches(self, version, date_from, date_to, status, limit, offset):
        where, params = ["1 = 1"], []
        for cond, value in (("version = %s", version), ("analysis_date >= %s", date_from),
                            ("analysis_date <= %s", date_to), ("status = %s", status)):
            if value is not None:
                where.append(cond)
                params.append(value)
        clause = " AND ".join(where)
        total = (await self._all(f"SELECT COUNT(*) AS n FROM analysis_batch WHERE {clause}",
                                 params))[0]["n"]
        rows = await self._all(f"SELECT {_BATCH_COLS}, {_CURRENT} FROM analysis_batch b "
                               f"WHERE {clause} ORDER BY analysis_date DESC, revision DESC, "
                               "created_at DESC LIMIT %s OFFSET %s", [*params, limit, offset])
        return [_batch(r) for r in rows], total

    async def current_batch(self, version: str, day: date | None = None) -> Batch | None:
        sql = (f"SELECT {_BATCH_COLS}, {_CURRENT} FROM analysis_batch b WHERE version = %s "
               "AND status IN ('succeeded', 'partial')")
        params: list = [version]
        if day is not None:
            sql += " AND analysis_date = %s"
            params.append(day)
        rows = await self._all(sql + " ORDER BY analysis_date DESC, revision DESC LIMIT 1",
                               params)
        return _batch(rows[0]) if rows else None

    async def running_batches(self) -> list[Batch]:
        rows = await self._all(f"SELECT {_BATCH_COLS}, FALSE AS is_current FROM analysis_batch b "
                               "WHERE status = 'running'")
        return [_batch(r) for r in rows]

    async def list_records(self, batch_id, filters, limit, offset):
        cols = {"station_id": "station_id", "device_id": "device_id",
                "equipment_type": "equipment_type", "level": "level_code"}
        where, params = ["batch_id = %s"], [batch_id]
        for k, v in filters.items():
            if v is not None:
                where.append(f"{cols[k]} = %s")
                params.append(v.value if hasattr(v, "value") else v)
        clause = " AND ".join(where)
        total = (await self._all(f"SELECT COUNT(*) AS n FROM analysis_record WHERE {clause}",
                                 params))[0]["n"]
        rows = await self._all(
            f"SELECT {_RECORD_COLS} FROM analysis_record WHERE {clause} ORDER BY "
            "CASE level_code WHEN 'inspect' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END, "
            "station_id, device_id LIMIT %s OFFSET %s", [*params, limit, offset])
        return [_record(r) for r in rows], total

    async def get_record(self, record_id: str) -> Record | None:
        rows = await self._all(f"SELECT {_RECORD_COLS} FROM analysis_record WHERE record_id = %s",
                               (record_id,))
        if not rows:
            return None
        record = _record(rows[0])
        events = await self._all(f"SELECT {_EVENT_COLS} FROM analysis_event WHERE record_id = %s "
                                 "ORDER BY start_time, code", (record_id,))
        record.events = [_event(e) for e in events]
        return record

