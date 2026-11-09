"""Station configuration storage.

openGauss table `station_config` is the source of truth (one row per station, the device
directory as JSON). It is seeded / updated from STATIONS_FILE (data/stations.json) with
`python scripts/init_db.py` (or `scripts/stations.py import`). Without OPENGAUSS_DSN the
file itself is used (demos and tests).

Every change bumps `config_version`; analysis batches store a full snapshot, so later
threshold changes never reinterpret old records.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.db.pool import PgPool
from app.stations.model import Station


def load_file(path: Path) -> list[Station]:
    """Stations from the seed file, including simulation profiles."""
    if not path.is_file():
        raise FileNotFoundError(
            f"station file not found: {path} (STATIONS_FILE). It ships with the repository as "
            "backend/data/stations.json; run commands from the backend/ directory")
    raw = json.loads(path.read_text(encoding="utf-8"))
    stations = [Station.model_validate(s) for s in raw.get("stations", [])]
    ids = [s.station_id for s in stations]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: duplicate station_id")
    return stations


class StationStore(Protocol):
    async def all(self, include_disabled: bool = False) -> list[Station]: ...
    async def get(self, station_id: str) -> Station | None: ...
    async def versions(self) -> dict[str, int]: ...
    async def upsert(self, stations: list[Station], overwrite: bool = True
                     ) -> dict[str, str]: ...
    async def close(self) -> None: ...


class MemoryStationStore:
    def __init__(self, stations: list[Station]) -> None:
        self._stations = {s.station_id: s for s in stations}
        self._versions = dict.fromkeys(self._stations, 1)

    @classmethod
    def from_file(cls, path: Path) -> "MemoryStationStore":
        return cls(load_file(path))

    async def all(self, include_disabled: bool = False) -> list[Station]:
        return [s for s in self._stations.values() if include_disabled or s.enabled]

    async def get(self, station_id: str) -> Station | None:
        return self._stations.get(station_id)

    async def versions(self) -> dict[str, int]:
        return dict(self._versions)

    async def upsert(self, stations: list[Station], overwrite: bool = True) -> dict[str, str]:
        out = {}
        for s in stations:
            old = self._stations.get(s.station_id)
            if old is not None and not overwrite:
                out[s.station_id] = "kept"
                continue
            if old is None:
                out[s.station_id], self._versions[s.station_id] = "created", 1
            elif old.model_dump() != s.model_dump():
                out[s.station_id] = "updated"
                self._versions[s.station_id] += 1
            else:
                out[s.station_id] = "unchanged"
            self._stations[s.station_id] = s
        return out

    async def close(self) -> None:
        return None


_COLUMNS = ("station_id", "name", "comm_address", "sampling_interval_s", "calibration_factor",
            "dose_l1", "dose_l2", "battery_warn_v", "battery_alarm_v", "battery_drop_v",
            "battery_drop_minutes", "battery_charge_minutes", "has_rain_gauge", "enabled")


def _row(s: Station) -> dict:
    d = s.model_dump()
    row = {c: d[c] for c in _COLUMNS}
    row["devices"] = json.dumps(d["devices"], ensure_ascii=False, sort_keys=True)
    return row


def _station(row: dict) -> Station:
    data = {c: row[c] for c in _COLUMNS}
    return Station.model_validate({**data, "devices": json.loads(row["devices"] or "[]")})


class PgStationStore:
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    async def all(self, include_disabled: bool = False) -> list[Station]:
        sql = f"SELECT {', '.join(_COLUMNS)}, devices FROM station_config"
        if not include_disabled:
            sql += " WHERE enabled"
        async with await self._pool.connection() as conn:
            rows = await (await conn.execute(sql + " ORDER BY station_id")).fetchall()
        return [_station(r) for r in rows]

    async def get(self, station_id: str) -> Station | None:
        async with await self._pool.connection() as conn:
            row = await (await conn.execute(
                f"SELECT {', '.join(_COLUMNS)}, devices FROM station_config "
                "WHERE station_id = %s", (station_id,))).fetchone()
        return _station(row) if row else None

    async def versions(self) -> dict[str, int]:
        async with await self._pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT station_id, config_version FROM station_config")).fetchall()
        return {r["station_id"]: r["config_version"] for r in rows}

    async def upsert(self, stations: list[Station], overwrite: bool = True) -> dict[str, str]:
        """Insert new stations; update changed ones (overwrite=False keeps existing rows)."""
        out = {}
        now = datetime.now(UTC)
        async with await self._pool.connection() as conn, conn.transaction():
            for s in stations:
                new = _row(s)
                old = await (await conn.execute(
                    f"SELECT {', '.join(_COLUMNS)}, devices FROM station_config "
                    "WHERE station_id = %s FOR UPDATE", (s.station_id,))).fetchone()
                if old is None:
                    cols = [*new.keys(), "config_version", "updated_at"]
                    await conn.execute(
                        f"INSERT INTO station_config ({', '.join(cols)}) VALUES "
                        f"({', '.join(['%s'] * len(cols))})", [*new.values(), 1, now])
                    out[s.station_id] = "created"
                elif not overwrite:
                    out[s.station_id] = "kept"
                elif _row(_station(old)) != new:
                    sets = ", ".join(f"{c} = %s" for c in new if c != "station_id")
                    await conn.execute(
                        f"UPDATE station_config SET {sets}, config_version = config_version + 1,"
                        " updated_at = %s WHERE station_id = %s",
                        [*(v for c, v in new.items() if c != "station_id"), now, s.station_id])
                    out[s.station_id] = "updated"
                else:
                    out[s.station_id] = "unchanged"
        return out

    async def close(self) -> None:
        return None  # the pool is owned by Resources
