"""Run async entry points the same way on Linux, macOS and Windows.

psycopg's async mode (openGauss) cannot use Windows' default ProactorEventLoop, so
command-line tools and scripts use a SelectorEventLoop there. The API server gets the same
via `uvicorn --loop asyncio:SelectorEventLoop` (scripts/run_api.py does this).
"""

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    if sys.platform == "win32":
        if sys.version_info >= (3, 12):
            return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    return asyncio.run(coro)


async def run_until_stopped(coro: Coroutine[Any, Any, Any], *,
                            duration_seconds: float | None = None) -> bool:
    """Run `coro` until it finishes, Ctrl+C / SIGTERM arrives, or `duration_seconds` passes.

    Stopping cancels the task, so pipelines get to flush buffered data in their `finally`.
    Returns True if the coroutine finished by itself.
    """
    import contextlib
    import signal

    task = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):  # Windows
            loop.add_signal_handler(sig, task.cancel)
    timer = loop.call_later(duration_seconds, task.cancel) if duration_seconds else None
    try:
        await task
        return True
    except asyncio.CancelledError:
        return False
    finally:
        if timer is not None:
            timer.cancel()
