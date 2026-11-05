"""source -> parser -> validator -> batch -> writer.

* A bounded queue between source and processing applies backpressure to the source.
* Batches flush on size or time. A failed write keeps the batch and retries with
  exponential backoff; while retrying no new messages are taken, so the queue (and then
  the source) slows down. If the retained buffer exceeds `max_buffer` the oldest points
  are dropped and counted, so a long storage outage cannot exhaust memory.
* Bad messages are counted by reason and never stop the pipeline.
"""

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from industrial_ai.collector.models import Measurement, RawMessage, RejectedMessage
from industrial_ai.collector.parser import MessageParser
from industrial_ai.collector.sources.base import MessageSource
from industrial_ai.collector.validator import MeasurementValidator
from industrial_ai.collector.writer import MeasurementWriter
from industrial_ai.storage.base import StorageUnavailableError

logger = logging.getLogger(__name__)

_END = object()


@dataclass
class PipelineStats:
    received: int = 0
    written: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    write_failures: int = 0
    dropped: int = 0
    last_write_at: datetime | None = None

    def snapshot(self) -> dict:
        return {
            "received": self.received, "written": self.written,
            "rejected": dict(self.rejected), "write_failures": self.write_failures,
            "dropped": self.dropped,
            "last_write_at": self.last_write_at.isoformat() if self.last_write_at else None,
        }


class IngestionPipeline:
    def __init__(
        self,
        source: MessageSource,
        parser: MessageParser,
        validator: MeasurementValidator,
        writer: MeasurementWriter,
        *,
        batch_size: int = 500,
        flush_seconds: float = 1.0,
        queue_size: int = 10_000,
        max_buffer: int = 100_000,
        max_backoff_seconds: float = 30.0,
        stats_log_seconds: float = 60.0,
    ) -> None:
        self._source = source
        self._parser = parser
        self._validator = validator
        self._writer = writer
        self._batch_size = batch_size
        self._flush_seconds = flush_seconds
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=queue_size)
        self._max_buffer = max_buffer
        self._max_backoff = max_backoff_seconds
        self._stats_log_seconds = stats_log_seconds
        self._buffer: list[Measurement] = []
        self.stats = PipelineStats()

    async def _produce(self) -> None:
        try:
            await self._source.run(self._queue.put)
        finally:
            await self._queue.put(_END)

    def _accept(self, message: RawMessage) -> None:
        self.stats.received += 1
        try:
            parsed = self._parser.parse(message)
        except RejectedMessage as exc:
            self._reject(exc, message)
            return
        finally:
            drain = getattr(self._parser, "drain_rejections", None)
            if drain is not None:
                self.stats.rejected.update(drain())
        measurements = []
        for m in parsed:  # validate per measurement: keep the good ones
            try:
                self._validator.check(m)
                measurements.append(m)
            except RejectedMessage as exc:
                self._reject(exc, message)
        self._buffer.extend(measurements)
        overflow = len(self._buffer) - self._max_buffer
        if overflow > 0:
            del self._buffer[:overflow]
            self.stats.dropped += overflow

    def _reject(self, exc: RejectedMessage, message: RawMessage) -> None:
        self.stats.rejected[exc.reason] += 1
        if sum(self.stats.rejected.values()) <= 20:  # sample, avoid log floods
            logger.warning("message rejected", extra={"fields": {
                "reason": exc.reason, "detail": exc.detail, "topic": message.topic[:128]}})

    async def _flush(self) -> None:
        backoff = 1.0
        while self._buffer:
            batch = self._buffer[: self._batch_size]
            try:
                await self._writer.write(batch)
            except StorageUnavailableError:
                self.stats.write_failures += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            del self._buffer[: len(batch)]
            self.stats.written += len(batch)
            self.stats.last_write_at = datetime.now(UTC)
            backoff = 1.0

    async def close(self) -> None:
        await self._writer.close()

    async def run(self) -> None:
        """Run until the source is exhausted (finite sources) or the task is cancelled."""
        producer = asyncio.create_task(self._produce(), name="collector-source")
        last_flush = last_log = time.monotonic()
        try:
            while True:
                timeout = max(0.0, self._flush_seconds - (time.monotonic() - last_flush))
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout)
                except TimeoutError:
                    item = None
                if item is _END:
                    break
                if isinstance(item, RawMessage):
                    self._accept(item)
                now = time.monotonic()
                if len(self._buffer) >= self._batch_size or now - last_flush >= self._flush_seconds:
                    await self._flush()
                    last_flush = now
                if now - last_log >= self._stats_log_seconds:
                    logger.info("collector stats", extra={"fields": self.stats.snapshot()})
                    last_log = now
            await self._flush()
            if producer.done() and producer.exception() is not None:
                raise producer.exception()  # type: ignore[misc]
        finally:
            if not producer.done():
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
            # Best effort: persist what is buffered when stopping.
            if self._buffer:
                try:
                    await asyncio.wait_for(self._flush(), timeout=5)
                except (TimeoutError, asyncio.CancelledError):
                    logger.warning("unflushed measurements dropped on shutdown",
                                   extra={"fields": {"count": len(self._buffer)}})
            logger.info("collector stopped", extra={"fields": self.stats.snapshot()})
