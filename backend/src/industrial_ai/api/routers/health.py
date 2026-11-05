"""Liveness / readiness / model status. Reports state only, never addresses or credentials."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from industrial_ai.api.deps import Container, get_container
from industrial_ai.storage.base import StorageUnavailableError

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready(c: Container = Depends(get_container)) -> JSONResponse:
    s = c.settings
    try:
        await c.timeseries.ping()
        timeseries_status = "ok"
    except StorageUnavailableError:
        timeseries_status = "unavailable"

    components = {
        "timeseries": {"backend": s.timeseries_backend, "status": timeseries_status},
        "analysis_store": {"backend": s.analysis_store_backend, "status": "ok",
                           "persistent": s.analysis_store_backend != "memory"},
        "session_store": {"backend": s.session_store_backend, "status": "ok",
                          "persistent": s.session_store_backend != "memory"},
        "llm": {"provider": s.llm_provider,
                "status": "not_configured" if s.llm_provider == "none" else "configured_unchecked"},
        "vector_db": {"status": "not_configured"},
        "embedding": {"status": "not_configured"},
        "rerank": {"status": "not_configured"},
    }
    is_ready = timeseries_status == "ok"
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={
            "status": "ready" if is_ready else "not_ready",
            "knowledge_index_version": s.knowledge_index_version,
            "components": components,
        },
    )


@router.get("/model")
async def model(c: Container = Depends(get_container)) -> dict:
    s = c.settings
    return {
        "provider": s.llm_provider,
        "name": s.resolved_llm_model,
        "precision": s.llm_model_precision,
        "revision": s.llm_model_revision,
        "status": "not_configured" if s.llm_provider == "none" else "configured_unchecked",
        "last_inference_check": None,
    }
