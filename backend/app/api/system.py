"""Health and knowledge-base status."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.deps import Resources, get_resources

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(r: Resources = Depends(get_resources)):
    """Status of every dependency. 503 if IoTDB, openGauss (conversations / work orders) or
    the station directory is unavailable."""
    s = r.settings
    checks: dict[str, dict] = {}
    for name, probe in (("timeseries", r.timeseries.ping), ("work_orders", r.work_orders.ping),
                        ("conversations", r.chat_store.ping)):
        try:
            await probe()
            checks[name] = {"status": "ok"}
        except Exception as exc:  # report, don't crash
            checks[name] = {"status": "error", "detail": str(exc)[:200]}
    checks["timeseries"]["backend"] = s.timeseries_backend
    checks["conversations"]["backend"] = "opengauss" if s.opengauss_dsn else "memory"
    checks["work_orders"]["backend"] = "opengauss" if s.opengauss_dsn else "memory"
    try:
        checks["stations"] = {"status": "ok", "count": len(await r.stations.all()),
                              "version": r.stations.version}
    except Exception as exc:
        checks["stations"] = {"status": "error", "detail": str(exc)[:200]}
    try:
        checks["mqtt_watermark"] = {"status": "ok", "value": await r.station_data.watermark()}
    except Exception as exc:
        checks["mqtt_watermark"] = {"status": "error", "detail": str(exc)[:200]}
    checks["knowledge"] = await r.retriever.status() if r.retriever else {"status": "disabled"}
    checks["llm"] = {"status": "configured" if r.chat.graph else "disabled",
                     "model": r.chat.model}
    checks["maintenance"] = {"rule_set": s.maintenance_rule_set_version,
                             "action_catalog": r.maintenance.catalog.version,
                             "narrator": r.maintenance.model_name,
                             "history": "mes_api" if s.mes_api_base_url else "work_order_copy"}
    ok = all(checks[k]["status"] == "ok" for k in ("timeseries", "work_orders", "conversations",
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
