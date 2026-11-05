"""Storage interfaces.

The time-series database, the relational database for analysis results and assistant
sessions, and the vector database are not finalised. Services depend only on these
protocols so a backend can be swapped without touching business logic.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from industrial_ai.schemas.analysis import AnalysisResult
from industrial_ai.schemas.assistant import ChatMessage, ChatSession
from industrial_ai.schemas.common import TelemetryPoint, TimeRange
from industrial_ai.schemas.equipment import Equipment
from industrial_ai.schemas.events import EventType, StreamEvent


class StorageUnavailableError(Exception):
    """A backing store could not be reached or returned an error."""


class TimeSeriesStore(Protocol):
    """Read-only access to equipment telemetry."""

    async def query(
        self, site_id: str, equipment_id: str, metric: str, time_range: TimeRange
    ) -> list[TelemetryPoint]:
        """Points in [start, end], ascending by timestamp."""
        ...

    async def ping(self) -> None:
        """Raise StorageUnavailableError if the store is not usable."""
        ...

    async def close(self) -> None: ...


class EquipmentCatalog(Protocol):
    """Equipment master data (owned by MES; this is a read-only view)."""

    async def list_equipment(self, site_id: str, category: str | None = None) -> list[Equipment]:
        ...

    async def get(self, site_id: str, equipment_id: str) -> Equipment | None: ...


@dataclass
class AnalysisRecord:
    result: AnalysisResult
    request_fingerprint: str
    idempotency_key: str | None = None
    created_by: str | None = None


class AnalysisStore(Protocol):
    async def create(self, record: AnalysisRecord) -> tuple[AnalysisRecord, bool]:
        """Insert the record, or return the existing one with the same idempotency key.

        Returns (record, created).
        """
        ...

    async def get(self, analysis_id: str) -> AnalysisRecord | None: ...

    async def save_result(self, result: AnalysisResult) -> None: ...

    async def append_event(
        self, analysis_id: str, event: EventType, data: dict[str, Any]
    ) -> StreamEvent:
        """Persist an event with the next monotonically increasing seq."""
        ...

    async def wait_for_events(
        self, analysis_id: str, after_seq: int, wait_seconds: float
    ) -> list[StreamEvent]:
        """Events with seq > after_seq; waits up to `wait_seconds` if there are none."""
        ...


class SessionStore(Protocol):
    async def create_session(self, user_id: str | None) -> ChatSession: ...

    async def get_session(self, session_id: str) -> ChatSession | None: ...

    async def append_message(self, message: ChatMessage) -> None: ...

    async def list_messages(self, session_id: str) -> list[ChatMessage]: ...
