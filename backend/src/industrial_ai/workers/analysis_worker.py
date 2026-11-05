"""In-process background runner for analysis tasks.

Analyses run independently of the SSE connection, so a client disconnect never cancels
or blocks the work; clients reconnect and replay events from the store. A dedicated task
queue can replace this once deployment topology is fixed.
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class AnalysisRunner:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def submit(self, name: str, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("analysis task crashed", exc_info=task.exception(),
                         extra={"fields": {"task": task.get_name()}})

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
