#!/usr/bin/env python
"""Simulated fixed-station data (synthetic, not plant data) for local testing.

  python scripts/simulate.py                      # 48 h history into IoTDB, then live until Ctrl+C
  python scripts/simulate.py --hours 24 --no-live # history only, then exit
  python scripts/simulate.py --hours 200 --no-live  # 8 days: enables day-on-day / 7-day comparisons
  python scripts/simulate.py --duration 30        # stop after 30 minutes
  python scripts/simulate.py --target mqtt        # publish to the broker (use with ingest_mqtt.py)
  python scripts/simulate.py --target stdout --hours 0.1 --no-live
  python scripts/simulate.py --purge --yes        # delete ALL series under IOTDB_ROOT

Stations, devices and the scripted scenario: STATIONS_FILE (data/stations.json). Scripted
events are placed on "yesterday" (ANALYSIS_TIMEZONE), the day the nightly analysis looks at.
Same --seed, same data; re-running overwrites identical timestamps.
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
from app.stations.store import load_file
from app.timeseries.iotdb import IoTDB


def main() -> None:
    p = _common.parser(__doc__)
    p.add_argument("--target", choices=["iotdb", "mqtt", "stdout"], default="iotdb")
    p.add_argument("--hours", type=float, help="history to generate first (default 48)")
    p.add_argument("--no-live", action="store_true", help="exit after the history")
    p.add_argument("--duration", type=float, metavar="MIN", help="stop after N minutes")
    p.add_argument("--station", action="append", help="only these stations (repeatable)")
    p.add_argument("--seed", type=int)
    p.add_argument("--purge", action="store_true")
    p.add_argument("--yes", action="store_true")
    args = p.parse_args()
    s = _common.settings(args, simulator_backfill_hours=args.hours, simulator_seed=args.seed)
    db = IoTDB(s)

    if args.purge:
        if not args.yes:
            sys.exit(f"--purge deletes every series under {s.iotdb_root} (including old demo "
                     "data); add --yes to confirm")
        run(db.purge())
        run(db.close())
        print(f"deleted all series under {s.iotdb_root}; run scripts/init_db.py --only iotdb")
        return

    stations = [st for st in load_file(s.stations_file)
                if not args.station or st.station_id in args.station]
    source = SimulatedSource(stations, timezone=s.analysis_timezone,
                             backfill_hours=s.simulator_backfill_hours,
                             realtime=not args.no_live, seed=s.simulator_seed,
                             topic_prefix=s.ingest_topic_prefix)
    print(f"simulating {len(stations)} stations -> {args.target} "
          f"({s.simulator_backfill_hours} h history{', then live' if not args.no_live else ''}); "
          "Ctrl+C to stop", file=sys.stderr)
    duration = args.duration * 60 if args.duration else None

    if args.target == "mqtt":
        run(run_until_stopped(MqttPublisher(s).run(source), duration))
        return
    writer = StdoutWriter() if args.target == "stdout" else db
    pipeline = Pipeline(source, SimulatorFormat(s.ingest_topic_prefix), writer,
                        batch_size=s.ingest_batch_size)

    async def go() -> None:
        try:
            await run_until_stopped(pipeline.run(), duration)
        finally:
            await db.close()

    run(go())
    print(f"done: {pipeline.stats.as_dict()}", file=sys.stderr)


if __name__ == "__main__":
    main()
