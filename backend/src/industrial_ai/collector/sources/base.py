from collections.abc import Awaitable, Callable
from typing import Protocol

from industrial_ai.collector.models import RawMessage

Emit = Callable[[RawMessage], Awaitable[None]]


class MessageSource(Protocol):
    """Anything that delivers raw (topic, payload) messages.

    `run` calls `emit` for each message and returns when the source is exhausted (a
    finite simulation) or runs until cancelled (live sources). `emit` may block to apply
    backpressure.
    """

    async def run(self, emit: Emit) -> None: ...
