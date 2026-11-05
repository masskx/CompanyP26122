"""Health and knowledge-base status."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.deps import Resources, get_resources

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(r: Resources = Depends(get_resources)):
    """Status of every dependency. 503 if IoTDB or the conversation store is down."""
    s = r.settings
    checks: dict[str, dict] = {}
    for name, probe in (("timeseries", r.timeseries.ping), ("conversations", r.chat_store.ping)):
        try:
            await probe()
            checks[name] = {"status": "ok"}
        except Exception as exc:  # report, don't crash
            checks[name] = {"status": "error", "detail": str(exc)[:200]}
    checks["timeseries"]["backend"] = s.timeseries_backend
    checks["conversations"]["backend"] = "opengauss" if s.opengauss_dsn else "memory"
    checks["knowledge"] = await r.retriever.status() if r.retriever else {"status": "disabled"}
    checks["llm"] = {"status": "configured" if r.chat.graph else "disabled",
                     "model": r.chat.model}
    ok = all(checks[k]["status"] == "ok" for k in ("timeseries", "conversations"))
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)


@router.get("/knowledge")
async def knowledge(r: Resources = Depends(get_resources)):
    """Knowledge-base versions (read-only; the knowledge base is built offline)."""
    if r.knowledge is None:
        return {"status": "disabled", "versions": []}
    import asyncio

    return {**(await r.retriever.status()),  # type: ignore[union-attr]
            "versions": await asyncio.to_thread(r.knowledge.versions)}
