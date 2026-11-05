"""MQTT subscription source (and a publisher for feeding simulated data into a broker).

Requires the optional extra: `pip install -e ".[mqtt]"` (aiomqtt / paho-mqtt).
Reconnects with exponential backoff; the payload format is handled by the parser.
"""

import asyncio
import logging
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from industrial_ai.collector.models import RawMessage
from industrial_ai.collector.sources.base import Emit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int = 1883
    topics: tuple[str, ...] = ("sim/radiation/#",)
    qos: int = 1
    client_id: str | None = None
    username: str | None = None
    password: str | None = None
    keepalive: int = 60
    tls: bool = False
    ca_file: str | None = None
    # Persistent session (clean_session=False) lets the broker queue QoS1 messages while the
    # collector restarts; requires a stable client_id.
    persistent_session: bool = False
    reconnect_max_seconds: float = 30.0


def _client(config: MqttConfig) -> Any:
    import aiomqtt

    tls_context = None
    if config.tls:
        tls_context = ssl.create_default_context(cafile=config.ca_file)
    return aiomqtt.Client(
        hostname=config.host, port=config.port, username=config.username,
        password=config.password, identifier=config.client_id, keepalive=config.keepalive,
        tls_context=tls_context,
        clean_session=None if not config.persistent_session else False,
    )


class MqttSource:
    def __init__(self, config: MqttConfig) -> None:
        if config.persistent_session and not config.client_id:
            raise ValueError("persistent MQTT sessions need MQTT_CLIENT_ID")
        self._config = config

    async def run(self, emit: Emit) -> None:
        import aiomqtt

        delay = 1.0
        while True:
            try:
                async with _client(self._config) as client:
                    for topic in self._config.topics:
                        await client.subscribe(topic, qos=self._config.qos)
                    logger.info("mqtt connected", extra={"fields": {
                        "host": self._config.host, "topics": list(self._config.topics)}})
                    delay = 1.0
                    async for message in client.messages:
                        payload = message.payload
                        if isinstance(payload, str):
                            payload = payload.encode()
                        elif not isinstance(payload, bytes | bytearray):
                            payload = b"" if payload is None else str(payload).encode()
                        await emit(RawMessage(topic=message.topic.value, payload=bytes(payload),
                                              received_at=datetime.now(UTC)))
            except aiomqtt.MqttError as exc:
                logger.warning("mqtt connection lost; reconnecting", extra={"fields": {
                    "error": str(exc), "retry_in_s": delay}})
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._config.reconnect_max_seconds)


class MqttPublisher:
    """Publishes RawMessages to a broker. Used to drive the full MQTT path with simulated data."""

    def __init__(self, config: MqttConfig) -> None:
        self._config = config
        self._client: Any | None = None

    async def __aenter__(self) -> "MqttPublisher":
        self._client = _client(self._config)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._client is not None
        await self._client.__aexit__(*exc)

    async def publish(self, message: RawMessage) -> None:
        assert self._client is not None
        await self._client.publish(message.topic, message.payload, qos=self._config.qos)
