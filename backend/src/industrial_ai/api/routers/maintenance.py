from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Path, Query, Request, status
from fastapi.responses import StreamingResponse

from industrial_ai.api.deps import Container, get_container
from industrial_ai.api.routers.sse import sse_response
from industrial_ai.core.errors import AppError
from industrial_ai.core.security import CallerContext, require_service_token
from industrial_ai.schemas.analysis import AnalysisAccepted, AnalysisCreate, AnalysisResult
from industrial_ai.schemas.events import SSE_HEARTBEAT

router = APIRouter(prefix="/maintenance/analyses", tags=["maintenance"])

_ANALYSIS_ID = Path(pattern=r"^ma_[0-9a-f]{32}$")


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=AnalysisAccepted)
async def create_analysis(
    body: AnalysisCreate,
    idempotency_key: str | None = Header(default=None, max_length=128),
    caller: CallerContext = Depends(require_service_token),
    c: Container = Depends(get_container),
) -> AnalysisAccepted:
    return await c.maintenance.create(body, caller, idempotency_key)


@router.get("/{analysis_id}", response_model=AnalysisResult)
async def get_analysis(
    analysis_id: str = _ANALYSIS_ID,
    _: CallerContext = Depends(require_service_token),
    c: Container = Depends(get_container),
) -> AnalysisResult:
    return await c.maintenance.get(analysis_id)


@router.get("/{analysis_id}/events", response_class=StreamingResponse)
async def analysis_events(
    request: Request,
    analysis_id: str = _ANALYSIS_ID,
    after_seq: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None),
    _: CallerContext = Depends(require_service_token),
    c: Container = Depends(get_container),
) -> StreamingResponse:
    await c.maintenance.get(analysis_id)  # 404 before the stream starts
    start = after_seq if after_seq is not None else _parse_last_event_id(last_event_id)
    heartbeat = c.settings.sse_heartbeat_seconds

    async def body() -> AsyncIterator[str]:
        async for item in c.maintenance.stream_events(analysis_id, start, heartbeat):
            if item is None:
                if await request.is_disconnected():
                    return
                yield SSE_HEARTBEAT
            else:
                yield item.to_sse()

    return sse_response(body())


def _parse_last_event_id(value: str | None) -> int:
    if value is None or value == "":
        return 0
    if not value.isdigit():
        raise AppError("invalid_last_event_id", "Last-Event-ID 必须为非负整数", status_code=422)
    return int(value)
