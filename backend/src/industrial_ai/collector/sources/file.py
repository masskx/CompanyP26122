"""Replay captured messages from a JSON Lines file (offline parser testing).

Each line: {"topic": "...", "payload": <object or string>}
Capture real traffic once (e.g. `mosquitto_sub -v` output converted to this format, or the
vendor's sample messages) and replay it through the configured parser with --dry-run to see
exactly what would be stored, before touching IoTDB.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from industrial_ai.collector.models import RawMessage
from industrial_ai.collector.sources.base import Emit


class JsonLinesFileSource:
    def __init__(self, path: Path) -> None:
        self._path = path

    async def run(self, emit: Emit) -> None:
        with self._path.open(encoding="utf-8") as f:
            for number, line in enumerate(f, start=1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    item = json.loads(line)
                    topic = item["topic"]
                    payload = item["payload"]
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    raise ValueError(f"{self._path}:{number}: expected "
                                     '{"topic": ..., "payload": ...}') from exc
                body = payload if isinstance(payload, str) else json.dumps(payload,
                                                                           ensure_ascii=False)
                await emit(RawMessage(topic=topic, payload=body.encode("utf-8"),
                                      received_at=datetime.now(UTC)))
