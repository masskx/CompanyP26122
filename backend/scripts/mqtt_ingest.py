#!/usr/bin/env python
"""MQTT ingestion: subscribe -> parse -> validate -> write to IoTDB. Runs until Ctrl+C.

  # production-style ingestion with the settings in .env (MQTT_*, COLLECTOR_PARSER, IOTDB_*)
  python scripts/mqtt_ingest.py

  # look at a real feed without storing anything (prints parsed measurements)
  python scripts/mqtt_ingest.py --dry-run --host 10.0.0.5 --topics "plant/#" \
      --parser mapping --parser-config my_mapping.json --accept-unknown-devices

  # test a parser offline against captured messages (JSON Lines: {"topic":..,"payload":..})
  python scripts/mqtt_ingest.py --from-file captured.jsonl --dry-run \
      --parser mapping --parser-config my_mapping.json

Command-line options override .env for this run only. The broker password is read from
MQTT_PASSWORD (never pass secrets on the command line).
"""

import argparse
import json
import sys
from pathlib import Path

import _common

from industrial_ai.core.runtime import run_async, run_until_stopped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    _common.add_env_arg(parser)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--topics", help="comma-separated subscriptions, e.g. 'plant/#,lab/#'")
    parser.add_argument("--username")
    parser.add_argument("--client-id")
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--parser", help="provisional_json | mapping | module:Class")
    parser.add_argument("--parser-config", type=Path, help="mapping file for --parser mapping")
    parser.add_argument("--topic-prefix", help="for the provisional_json parser")
    parser.add_argument("--from-file", type=Path, help="replay captured JSON Lines instead")
    parser.add_argument("--dry-run", action="store_true", help="print, do not write to IoTDB")
    parser.add_argument("--accept-unknown-devices", action="store_true",
                        help="do not reject devices missing from EQUIPMENT_CATALOG_PATH")
    parser.add_argument("--duration", type=float, metavar="MINUTES")
    args = parser.parse_args()

    settings = _common.load_settings(
        args, collector_source="mqtt", mqtt_host=args.host, mqtt_port=args.port,
        mqtt_topics=[t.strip() for t in args.topics.split(",")] if args.topics else None,
        mqtt_username=args.username, mqtt_client_id=args.client_id,
        mqtt_tls=True if args.tls else None, collector_parser=args.parser,
        collector_parser_config=args.parser_config, collector_topic_prefix=args.topic_prefix,
        collector_reject_unknown_equipment=False if args.accept_unknown_devices else None)

    from industrial_ai.collector.factory import build_pipeline
    from industrial_ai.collector.sources.file import JsonLinesFileSource
    from industrial_ai.collector.writer import StdoutWriter

    source = JsonLinesFileSource(args.from_file) if args.from_file else None
    if source is None:
        try:
            import aiomqtt  # noqa: F401
        except ImportError:
            sys.exit('MQTT support is not installed: pip install -e ".[mqtt]"')
        print(f"subscribing to {settings.mqtt_topics} at {settings.mqtt_host}:"
              f"{settings.mqtt_port} (parser={settings.collector_parser}); Ctrl+C to stop",
              file=sys.stderr)
    writer = StdoutWriter() if args.dry_run else None
    pipeline = build_pipeline(settings, source=source, writer=writer)

    async def run() -> None:
        try:
            await run_until_stopped(pipeline.run(),
                                    duration_seconds=args.duration * 60 if args.duration else None)
        finally:
            await pipeline.close()

    run_async(run())
    print("stats: " + json.dumps(pipeline.stats.snapshot(), ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
