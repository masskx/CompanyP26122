"""Station directory: a versioned snapshot file (STATIONS_FILE, default data/stations.json).

The main server owns station configuration. openGauss on the compute server stores only
conversations and the work-order copy, so the directory is a file shipped with the
deployment (later it can be refreshed from the main server). The file is re-read when it
changes; `version` identifies the snapshot used by an analysis.
"""

import hashlib
import json
from pathlib import Path

from app.stations.model import AuxiliarySource, Station


def load_directory(path: Path) -> tuple[list[Station], list[AuxiliarySource]]:
    """Stations (including simulation profiles) and auxiliary sources from the snapshot file."""
    if not path.is_file():
        raise FileNotFoundError(
            f"station file not found: {path} (STATIONS_FILE). It ships with the repository as "
            "backend/data/stations.json; run commands from the backend/ directory")
    raw = json.loads(path.read_text(encoding="utf-8"))
    stations = [Station.model_validate(s) for s in raw.get("stations", [])]
    auxiliary = [AuxiliarySource.model_validate(a) for a in raw.get("auxiliary_sources", [])]
    ids = [s.station_id for s in stations] + [a.source_id for a in auxiliary]
    if len(set(ids)) != len(ids):
        raise ValueError(f"{path}: duplicate station_id / source_id")
    return stations, auxiliary


def load_file(path: Path) -> list[Station]:
    return load_directory(path)[0]


def known_equipment(path: Path) -> set[tuple[str, str]]:
    """(site, equipment) pairs the ingester accepts: station units and auxiliary sources."""
    stations, auxiliary = load_directory(path)
    return {(st.station_id, eq) for st in stations for eq in st.equipment_ids()} | \
        {(a.source_id, a.equipment_id) for a in auxiliary}  # type: ignore[misc]


class StationStore:
    """Read-only station directory. Built from a file (re-read on change) or a list."""

    def __init__(self, stations: list[Station] | None = None, path: Path | None = None,
                 auxiliary: list[AuxiliarySource] | None = None) -> None:
        self._path = path
        self._mtime: float | None = None
        self._stations: dict[str, Station] = {}
        self._auxiliary: dict[str, AuxiliarySource] = {}
        self.version = "inline"
        if stations is not None:
            self._set(stations, auxiliary or [], "inline")
        elif path is not None:
            self._reload()

    @classmethod
    def from_file(cls, path: Path) -> "StationStore":
        return cls(path=path)

    def _set(self, stations: list[Station], auxiliary: list[AuxiliarySource],
             version: str) -> None:
        self._stations = {s.station_id: s for s in stations}
        self._auxiliary = {a.source_id: a for a in auxiliary}
        self.version = version

    def _reload(self) -> None:
        assert self._path is not None
        mtime = self._path.stat().st_mtime if self._path.is_file() else None
        if mtime is not None and mtime == self._mtime:
            return
        data = self._path.read_bytes() if self._path.is_file() else b""
        self._set(*load_directory(self._path), hashlib.sha256(data).hexdigest()[:12])
        self._mtime = mtime

    async def all(self, include_disabled: bool = False) -> list[Station]:
        if self._path is not None:
            self._reload()
        return [s for s in self._stations.values() if include_disabled or s.enabled]

    async def get(self, station_id: str) -> Station | None:
        if self._path is not None:
            self._reload()
        return self._stations.get(station_id)

    async def auxiliary(self) -> list[AuxiliarySource]:
        """Monitoring vehicle, weather tower, ...: stored as reported, no maintenance rules."""
        if self._path is not None:
            self._reload()
        return list(self._auxiliary.values())

    async def auxiliary_source(self, source_id: str) -> AuxiliarySource | None:
        if self._path is not None:
            self._reload()
        return self._auxiliary.get(source_id)

    async def close(self) -> None:
        return None
