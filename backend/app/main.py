"""FastAPI application.

    uvicorn app.main:app --reload          (or: python scripts/run.py)
    API docs: http://127.0.0.1:8000/docs
"""

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, maintenance, stations, system
from app.config import Settings, get_settings
from app.deps import Resources, build_resources, check_api_key

logger = logging.getLogger("app")


def create_app(settings: Settings | None = None, resources: Resources | None = None) -> FastAPI:
    settings = settings or (resources.settings if resources else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=settings.log_level.upper(),
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        r = app.state.resources = resources or build_resources(settings)
        background = []
        if settings.demo_simulation:
            background.append(asyncio.create_task(_demo_simulation(r)))
        try:
            yield
        finally:
            for task in background:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            await r.close()

    app = FastAPI(title="Radiation monitoring AI backend", version="0.4.0", lifespan=lifespan,
                  docs_url=None if settings.app_env == "production" else "/docs")
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                           allow_methods=["*"], allow_headers=["*"])
    deps = [Depends(check_api_key)]
    app.include_router(system.router, prefix="/api/v1", dependencies=deps)
    app.include_router(stations.router, prefix="/api/v1", dependencies=deps)
    app.include_router(maintenance.router, prefix="/internal/v1", dependencies=deps)
    app.include_router(chat.router, prefix="/api/v1", dependencies=deps)
    app.include_router(chat.router, prefix="/internal/v1/assistant", dependencies=deps,
                       include_in_schema=False)
    return app


async def _demo_simulation(r: Resources) -> None:
    """Database-free demo: write the simulated history first, then keep publishing live."""
    from app.ingestion.parsers import TelemetryV1Format
    from app.ingestion.pipeline import Pipeline
    from app.ingestion.simulator import SimulatedSource
    from app.stations.store import load_file

    s = r.settings
    stations = load_file(s.stations_file)  # simulation profiles live in the file
    fmt = TelemetryV1Format(s.ingest_topic_prefix)
    history = SimulatedSource(stations, timezone=s.analysis_timezone,
                              backfill_hours=s.simulator_backfill_hours, realtime=False,
                              seed=s.simulator_seed, topic_prefix=s.ingest_topic_prefix)
    await Pipeline(history, fmt, r.timeseries, batch_size=5000).run()
    logger.info("demo simulation: %.0f h of history written", s.simulator_backfill_hours)
    live = SimulatedSource(stations, timezone=s.analysis_timezone, backfill_hours=0,
                           seed=s.simulator_seed, topic_prefix=s.ingest_topic_prefix,
                           first_sequence=history.last_sequence + 1)
    await Pipeline(live, fmt, r.timeseries).run()


app = create_app()
