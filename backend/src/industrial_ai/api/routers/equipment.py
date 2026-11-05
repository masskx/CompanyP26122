from fastapi import APIRouter, Depends, Query

from industrial_ai.api.deps import Container, get_container
from industrial_ai.core.security import CallerContext, require_service_token
from industrial_ai.schemas.equipment import HealthOverview

router = APIRouter(prefix="/equipment", tags=["equipment"])


@router.get("/health", response_model=HealthOverview)
async def equipment_health(
    site_id: str = Query(pattern=r"^[A-Za-z0-9_\-]+$"),
    category: str | None = Query(default=None, max_length=64),
    _: CallerContext = Depends(require_service_token),
    c: Container = Depends(get_container),
) -> HealthOverview:
    return await c.equipment.health_overview(site_id, category)
