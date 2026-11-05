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

from app.api import chat, devices, system
from app.config import Settings, get_settings
from app.deps import Resources, build_resources, check_api_key

logger = logging.getLogger("app")


def create_app(settings: Settings | None = None, resources: Resources | None = None) -> FastAPI:
    settings = settings or (resources.settings if resources else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=settings.log_level.upper(),
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        app.state.resources = resources or build_resources(settings)
        demo = None
        if settings.demo_simulation:  # simulated data inside the API process
            demo = asyncio.create_task(_demo_simulation(app.state.resources))
        try:
            yield
        finally:
            if demo:
                demo.cancel()
                with contextlib.suppress(BaseException):
                    await demo
            await app.state.resources.close()

    app = FastAPI(title="Radiation monitoring AI backend", version="0.2.0", lifespan=lifespan,
                  docs_url=None if settings.app_env == "production" else "/docs")
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                           allow_methods=["*"], allow_headers=["*"])
    deps = [Depends(check_api_key)]
    app.include_router(system.router, prefix="/api/v1", dependencies=deps)
    app.include_router(devices.router, prefix="/api/v1", dependencies=deps)
    app.include_router(chat.router, prefix="/api/v1", dependencies=deps)
    return app


async def _demo_simulation(r: Resources) -> None:
    from app.ingestion.parsers import SimulatorFormat
    from app.ingestion.pipeline import Pipeline
    from app.ingestion.simulator import SimulatedSource

    s = r.settings
    source = SimulatedSource(r.devices.catalog.all(), backfill_hours=s.simulator_backfill_hours,
                             seed=s.simulator_seed, topic_prefix=s.ingest_topic_prefix)
    await Pipeline(source, SimulatorFormat(s.ingest_topic_prefix), r.timeseries).run()


app = create_app()
