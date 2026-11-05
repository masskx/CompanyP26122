#!/usr/bin/env python
"""MQTT -> IoTDB ingestion (production path). Runs until Ctrl+C / SIGTERM.

  python scripts/ingest_mqtt.py                                   # settings from .env
  python scripts/ingest_mqtt.py --dry-run                         # print, store nothing
  python scripts/ingest_mqtt.py --host 10.0.0.5 --topics "plant/#" --parser mapping \
      --mapping my_mapping.json --dry-run                         # inspect the real feed
  python scripts/ingest_mqtt.py --file captured.jsonl --dry-run --parser mapping \
      --mapping my_mapping.json                                   # test offline

Options override .env for this run. The broker password comes from MQTT_PASSWORD only.
"""

import json
import sys
from pathlib import Path

import _common

from app.ingestion.parsers import build_parser
from app.ingestion.pipeline import (
    FileSource,
    MqttSource,
    Pipeline,
    StdoutWriter,
    run,
    run_until_stopped,
)
from app.timeseries.devices import DeviceCatalog
from app.timeseries.iotdb import IoTDB


def main() -> None:
    p = _common.parser(__doc__)
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--topics", help="comma-separated, e.g. 'plant/#'")
    p.add_argument("--parser", help="simulator | mapping | module:Class")
    p.add_argument("--mapping", type=Path, help="mapping file for --parser mapping")
    p.add_argument("--file", type=Path, help="replay captured JSON Lines instead of MQTT")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--accept-unknown-devices", action="store_true")
    p.add_argument("--duration", type=float, metavar="MIN")
    args = p.parse_args()
    s = _common.settings(args, mqtt_host=args.host, mqtt_port=args.port,
                         mqtt_topics=args.topics.split(",") if args.topics else None,
                         ingest_parser=args.parser, ingest_mapping_file=args.mapping)
    known = None if args.accept_unknown_devices or not s.ingest_reject_unknown_devices else \
        {d.device_id for d in DeviceCatalog.load(s.devices_file).all()}
    if args.file:
        source = FileSource(args.file)
    else:
        try:
            import aiomqtt  # noqa: F401
        except ImportError:
            sys.exit('MQTT support missing: pip install -e ".[mqtt]"')
        source = MqttSource(s)
        print(f"subscribing {s.mqtt_topics} at {s.mqtt_host}:{s.mqtt_port} "
              f"(parser={s.ingest_parser}); Ctrl+C to stop", file=sys.stderr)
    db = IoTDB(s)
    pipeline = Pipeline(source, build_parser(s), StdoutWriter() if args.dry_run else db,
                        known_devices=known, batch_size=s.ingest_batch_size,
                        flush_seconds=s.ingest_flush_seconds)

    async def go() -> None:
        try:
            await run_until_stopped(pipeline.run(), args.duration * 60 if args.duration else None)
        finally:
            await db.close()

    run(go())
    print("stats: " + json.dumps(pipeline.stats.as_dict(), ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
