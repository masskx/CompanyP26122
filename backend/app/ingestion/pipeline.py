"""source -> parser -> validate -> batch -> write (IoTDB, or stdout for dry runs).

Sources (all deliver RawMessage(topic, payload)):
  SimulatedSource   app/ingestion/simulator.py
  MqttSource        subscribes to the broker (reconnects with backoff)
  FileSource        replays captured messages from a JSON Lines file
Swapping the simulator for the real MQTT feed changes only the source and the parser.
"""

import asyncio
import contextlib
import json
import logging
import math
import signal
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from app.config import Settings
from app.ingestion.parsers import Parser, RawMessage, Rejected
from app.timeseries.iotdb import Measurement, TimeSeriesError

logger = logging.getLogger(__name__)
Emit = Callable[[RawMessage], Awaitable[None]]


class Source(Protocol):
    async def run(self, emit: Emit) -> None: ...


class Writer(Protocol):
    async def write(self, batch: list[Measurement]) -> None: ...


class MqttSource:
    def __init__(self, s: Settings) -> None:
        self.s = s

    async def run(self, emit: Emit) -> None:
        import ssl

        import aiomqtt

        delay = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.s.mqtt_host, port=self.s.mqtt_port,
                    username=self.s.mqtt_username,
                    password=self.s.mqtt_password.get_secret_value()
                    if self.s.mqtt_password else None,
                    identifier=self.s.mqtt_client_id,
                    tls_context=ssl.create_default_context() if self.s.mqtt_tls else None,
                    # A fixed client id keeps a persistent session: QoS1 messages are queued
                    # by the broker while the ingester restarts.
                    clean_session=False if self.s.mqtt_client_id else None,
                ) as client:
                    for topic in self.s.mqtt_topics:
                        await client.subscribe(topic, qos=self.s.mqtt_qos)
                    logger.info("mqtt connected to %s:%s %s", self.s.mqtt_host, self.s.mqtt_port,
                                self.s.mqtt_topics)
                    delay = 1.0
                    async for msg in client.messages:
                        payload = msg.payload if isinstance(msg.payload, bytes) else \
                            str(msg.payload or "").encode()
                        await emit(RawMessage(msg.topic.value, payload))
            except aiomqtt.MqttError as exc:
                logger.warning("mqtt connection lost (%s); retry in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)


class MqttPublisher:
    """Publishes messages to the broker (lets the simulator stand in for real devices)."""

    def __init__(self, s: Settings) -> None:
        self.s = s

    async def run(self, source: Source) -> int:
        import aiomqtt

        count = 0
        async with aiomqtt.Client(hostname=self.s.mqtt_host, port=self.s.mqtt_port,
                                  username=self.s.mqtt_username,
                                  password=self.s.mqtt_password.get_secret_value()
                                  if self.s.mqtt_password else None) as client:
            async def emit(m: RawMessage) -> None:
                nonlocal count
                await client.publish(m.topic, m.payload, qos=self.s.mqtt_qos)
                count += 1

            await source.run(emit)
        return count


class FileSource:
    """JSON Lines, one {"topic": "...", "payload": {...} | "..."} per line."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def run(self, emit: Emit) -> None:
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                item = json.loads(line)
                payload = item["payload"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ValueError(f"{self.path}:{n}: expected {{topic, payload}}") from exc
            body = payload if isinstance(payload, str) else json.dumps(payload,
                                                                      ensure_ascii=False)
            await emit(RawMessage(item["topic"], body.encode()))


class StdoutWriter:
    async def write(self, batch: list[Measurement]) -> None:
        for m in batch:
            print(json.dumps({"site": m.site, "device_id": m.device_id, "metric": m.metric,
                              "time": m.time.isoformat(), "value": m.value},
                             ensure_ascii=False))


@dataclass
class Stats:
    received: int = 0
    written: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    write_failures: int = 0
    dropped: int = 0

    def as_dict(self) -> dict:
        return {"received": self.received, "written": self.written,
                "rejected": dict(self.rejected), "write_failures": self.write_failures,
                "dropped": self.dropped}


class Pipeline:
    """Bounded queue (backpressure), size/time batching, retry with backoff while storage is
    down, bounded buffer (oldest dropped and counted), per-reason rejection counts."""

    def __init__(self, source: Source, parser: Parser, writer: Writer, *,
                 known_devices: set[str] | None = None, batch_size: int = 500,
                 flush_seconds: float = 1.0, max_buffer: int = 100_000,
                 max_future_skew: timedelta = timedelta(minutes=5)) -> None:
        self.source, self.parser, self.writer = source, parser, writer
        self.known = known_devices
        self.batch_size, self.flush_seconds = batch_size, flush_seconds
        self.max_buffer, self.max_skew = max_buffer, max_future_skew
        self.stats = Stats()
        self._queue: asyncio.Queue[RawMessage | None] = asyncio.Queue(maxsize=10_000)
        self._buffer: list[Measurement] = []

    def _check(self, m: Measurement) -> None:
        if self.known is not None and m.device_id not in self.known:
            raise Rejected("unknown_device", m.device_id)
        if not math.isfinite(m.value) or (m.metric == "dose_rate" and m.value < 0):
            raise Rejected("bad_value", str(m.value))
        if m.time > datetime.now(UTC) + self.max_skew:
            raise Rejected("future_timestamp", m.time.isoformat())

    def _accept(self, msg: RawMessage) -> None:
        self.stats.received += 1
        try:
            measurements = self.parser.parse(msg)
        except Rejected as exc:
            self.stats.rejected[exc.reason] += 1
            return
        finally:
            partial = getattr(self.parser, "partial_rejections", None)
            if partial:
                self.stats.rejected.update(partial)
                partial.clear()
        for m in measurements:
            try:
                self._check(m)
                self._buffer.append(m)
            except Rejected as exc:
                self.stats.rejected[exc.reason] += 1
        if len(self._buffer) > self.max_buffer:
            overflow = len(self._buffer) - self.max_buffer
            del self._buffer[:overflow]
            self.stats.dropped += overflow

    async def _flush(self) -> None:
        backoff = 1.0
        while self._buffer:
            batch = self._buffer[: self.batch_size]
            try:
                await self.writer.write(batch)
            except TimeSeriesError as exc:
                self.stats.write_failures += 1
                logger.warning("write failed (%s); retry in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            del self._buffer[: len(batch)]
            self.stats.written += len(batch)

    async def run(self) -> None:
        async def produce() -> None:
            try:
                await self.source.run(self._queue.put)
            finally:
                await self._queue.put(None)

        producer = asyncio.create_task(produce())
        loop = asyncio.get_running_loop()
        last_flush = loop.time()
        try:
            while True:
                timeout = max(0.0, self.flush_seconds - (loop.time() - last_flush))
                try:
                    msg = await asyncio.wait_for(self._queue.get(), timeout)
                except TimeoutError:
                    msg = ...
                if msg is None:
                    break
                if isinstance(msg, RawMessage):
                    self._accept(msg)
                if len(self._buffer) >= self.batch_size or \
                        loop.time() - last_flush >= self.flush_seconds:
                    await self._flush()
                    last_flush = loop.time()
            await self._flush()
            if producer.done() and producer.exception():
                raise producer.exception()  # type: ignore[misc]
        finally:
            producer.cancel()
            with contextlib.suppress(BaseException):
                await producer
            if self._buffer:  # best effort on shutdown
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(self._flush(), 5)


async def run_until_stopped(coro: Coroutine[Any, Any, Any],
                            duration_seconds: float | None = None) -> bool:
    """Run until done, Ctrl+C / SIGTERM, or the duration elapses (task is cancelled so
    pipelines flush). Returns True if it finished by itself."""
    task = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, task.cancel)
    timer = loop.call_later(duration_seconds, task.cancel) if duration_seconds else None
    try:
        await task
        return True
    except asyncio.CancelledError:
        return False
    finally:
        if timer:
            timer.cancel()


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """asyncio.run with the selector loop psycopg needs on Windows."""
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)
