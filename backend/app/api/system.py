"""Health and knowledge-base status."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.deps import Resources, get_resources

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(r: Resources = Depends(get_resources)):
    """Status of every dependency. 503 if IoTDB, the analysis store or the conversation store
    is down."""
    s = r.settings
    checks: dict[str, dict] = {}
    for name, probe in (("timeseries", r.timeseries.ping), ("analysis", r.analysis.store.ping),
                        ("conversations", r.chat_store.ping)):
        try:
            await probe()
            checks[name] = {"status": "ok"}
        except Exception as exc:  # report, don't crash
            checks[name] = {"status": "error", "detail": str(exc)[:200]}
    checks["timeseries"]["backend"] = s.timeseries_backend
    checks["conversations"]["backend"] = "opengauss" if s.opengauss_dsn else "memory"
    checks["analysis"]["backend"] = "opengauss" if s.opengauss_dsn else "memory"
    try:
        checks["stations"] = {"status": "ok", "count": len(await r.stations.all())}
    except Exception as exc:
        checks["stations"] = {"status": "error", "detail": str(exc)[:200]}
    checks["knowledge"] = await r.retriever.status() if r.retriever else {"status": "disabled"}
    checks["llm"] = {"status": "configured" if r.chat.graph else "disabled",
                     "model": r.chat.model}
    checks["scheduler"] = {"status": "running" if r.scheduler else "disabled",
                           "run_at": s.analysis_run_at, "timezone": s.analysis_timezone}
    ok = all(checks[k]["status"] == "ok" for k in ("timeseries", "analysis", "conversations",
                                                    "stations"))
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
