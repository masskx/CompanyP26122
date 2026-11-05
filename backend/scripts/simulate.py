#!/usr/bin/env python
"""Simulated radiation dose-rate data (synthetic, not plant data) for local testing.

  python scripts/simulate.py                      # 48 h history into IoTDB, then live until Ctrl+C
  python scripts/simulate.py --hours 24 --no-live # history only, then exit
  python scripts/simulate.py --duration 30        # stop after 30 minutes
  python scripts/simulate.py --target mqtt        # publish to the broker (use with ingest_mqtt.py)
  python scripts/simulate.py --target stdout --hours 0.1 --no-live
  python scripts/simulate.py --purge --yes        # delete the simulated series from IoTDB

Devices, baselines, scripted events and (simulation-only) thresholds: DEVICES_FILE
(data/devices.json). Same --seed, same data; re-running overwrites identical timestamps.
"""

import sys

import _common

from app.ingestion.parsers import SimulatorFormat
from app.ingestion.pipeline import (
    MqttPublisher,
    Pipeline,
    StdoutWriter,
    run,
    run_until_stopped,
)
from app.ingestion.simulator import SimulatedSource
from app.timeseries.devices import DeviceCatalog
from app.timeseries.iotdb import IoTDB


def main() -> None:
    p = _common.parser(__doc__)
    p.add_argument("--target", choices=["iotdb", "mqtt", "stdout"], default="iotdb")
    p.add_argument("--hours", type=float, help="history to generate first (default 48)")
    p.add_argument("--no-live", action="store_true", help="exit after the history")
    p.add_argument("--duration", type=float, metavar="MIN", help="stop after N minutes")
    p.add_argument("--seed", type=int)
    p.add_argument("--purge", action="store_true")
    p.add_argument("--yes", action="store_true")
    args = p.parse_args()
    s = _common.settings(args, simulator_backfill_hours=args.hours, simulator_seed=args.seed)
    catalog = DeviceCatalog.load(s.devices_file)
    devices = [d for d in catalog.all() if d.simulation is not None]
    db = IoTDB(s)

    if args.purge:
        if not args.yes:
            sys.exit(f"--purge deletes {len(devices)} devices' series; add --yes to confirm")
        series = [(d.site, d.device_id, m.name) for d in devices for m in d.metrics]
        print(f"deleted {run(db.delete(series))} series")
        run(db.close())
        return

    source = SimulatedSource(devices, backfill_hours=s.simulator_backfill_hours,
                             realtime=not args.no_live, seed=s.simulator_seed,
                             topic_prefix=s.ingest_topic_prefix)
    print(f"simulating {len(devices)} devices -> {args.target} "
          f"({s.simulator_backfill_hours} h history{', then live' if not args.no_live else ''}); "
          "Ctrl+C to stop", file=sys.stderr)
    duration = args.duration * 60 if args.duration else None

    if args.target == "mqtt":
        run(run_until_stopped(MqttPublisher(s).run(source), duration))
        return
    writer = StdoutWriter() if args.target == "stdout" else db
    pipeline = Pipeline(source, SimulatorFormat(s.ingest_topic_prefix), writer)

    async def go() -> None:
        try:
            await run_until_stopped(pipeline.run(), duration)
        finally:
            await db.close()

    run(go())
    print(f"done: {pipeline.stats.as_dict()}", file=sys.stderr)


if __name__ == "__main__":
    main()
