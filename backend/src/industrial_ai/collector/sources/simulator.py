"""Simulated source: emits the same (topic, payload) messages an MQTT broker would deliver."""

import asyncio
import heapq
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from industrial_ai.collector.models import RawMessage
from industrial_ai.collector.parser import ProvisionalJsonCodec
from industrial_ai.collector.simulation import DeviceProfile, DeviceSimulator
from industrial_ai.collector.sources.base import Emit

logger = logging.getLogger(__name__)


class SimulatedSource:
    def __init__(
        self,
        devices: list[DeviceProfile],
        codec: ProvisionalJsonCodec,
        *,
        backfill: timedelta = timedelta(0),
        realtime: bool = True,
        seed: int | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._devices = devices
        self._codec = codec
        self._backfill = backfill
        self._realtime = realtime
        self._seed = seed
        self._clock = clock
        self._sleep = sleep

    def _simulators(self, anchor: datetime) -> list[DeviceSimulator]:
        sims = []
        for p in self._devices:
            # Per-device stream, so adding a device does not change the others' data.
            rng = random.Random(f"{self._seed}:{p.site_id}:{p.equipment_id}"
                                if self._seed is not None else None)
            sims.append(DeviceSimulator(p, rng, start=anchor - self._backfill, anchor=anchor))
        return sims

    async def _emit_step(self, sim: DeviceSimulator, emit: Emit) -> None:
        timestamp, value = sim.step()
        if value is None:
            return  # device silent (dropout): nothing is published
        p = sim.profile
        topic, payload = self._codec.encode(p.site_id, p.equipment_id, p.metric, timestamp,
                                            value, p.unit)
        await emit(RawMessage(topic=topic, payload=payload, received_at=self._clock()))

    async def run(self, emit: Emit) -> None:
        anchor = self._clock()
        sims = self._simulators(anchor)

        # Historical data first, so analyses have something to work on immediately.
        for sim in sims:
            while sim.next_time <= anchor:
                await self._emit_step(sim, emit)
        logger.info("simulator backfill complete", extra={"fields": {
            "devices": len(sims), "backfill_hours": self._backfill.total_seconds() / 3600}})
        if not self._realtime:
            return

        heap = [(s.next_time, i) for i, s in enumerate(sims)]
        heapq.heapify(heap)
        while True:
            due, index = heap[0]
            wait = (due - self._clock()).total_seconds()
            if wait > 0:
                await self._sleep(min(wait, 1.0))
                continue
            heapq.heappop(heap)
            await self._emit_step(sims[index], emit)
            heapq.heappush(heap, (sims[index].next_time, index))
