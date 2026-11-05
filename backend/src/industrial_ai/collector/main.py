"""Collector command line.

    industrial-ai-collector run           # COLLECTOR_SOURCE (simulator | mqtt) -> IoTDB
    industrial-ai-collector publish-sim   # simulator -> MQTT broker (stand-in for devices)

Configuration comes from environment variables / .env (see .env.example).
"""

import argparse
import logging

from industrial_ai.collector.factory import build_pipeline, build_simulated_source, mqtt_config
from industrial_ai.collector.models import RawMessage
from industrial_ai.collector.sources.mqtt import MqttPublisher
from industrial_ai.core.config import Settings
from industrial_ai.core.logging import configure_logging
from industrial_ai.core.runtime import run_async, run_until_stopped

logger = logging.getLogger("industrial_ai.collector")


async def run_pipeline(settings: Settings) -> None:
    pipeline = build_pipeline(settings)
    try:
        await pipeline.run()
    finally:
        await pipeline.close()


async def publish_simulation(settings: Settings) -> None:
    """Simulator -> MQTT broker: stands in for the real devices."""
    source = build_simulated_source(settings)
    published = 0
    async with MqttPublisher(mqtt_config(settings)) as publisher:
        async def emit(message: RawMessage) -> None:
            nonlocal published
            await publisher.publish(message)
            published += 1

        try:
            await source.run(emit)
        finally:
            logger.info("simulator publisher stopped", extra={"fields": {"published": published}})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="industrial-ai-collector")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "publish-sim"])
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level, settings.app_env)
    coro = run_pipeline(settings) if args.command == "run" else publish_simulation(settings)
    run_async(run_until_stopped(coro))


if __name__ == "__main__":
    main()
