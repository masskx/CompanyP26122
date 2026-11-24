#!/usr/bin/env python
"""Simulated fixed-station data (synthetic, not plant data) in the main server's MQTT format
for local testing: ghmes/signal snapshots (INGEST_PARSER=ghmes, the default) or the
telemetry_v1 contract (INGEST_PARSER=telemetry_v1).

  python scripts/simulate.py                      # 48 h history into IoTDB, then live until Ctrl+C
  python scripts/simulate.py --hours 24 --no-live # history only, then exit
  python scripts/simulate.py --hours 200 --no-live  # 8 days: enables day-on-day / 7-day comparisons
  python scripts/simulate.py --duration 30        # stop after 30 minutes
  python scripts/simulate.py --target mqtt        # publish to the broker (use with ingest_mqtt.py)
  python scripts/simulate.py --target stdout --hours 0.1 --no-live
  python scripts/simulate.py --purge --yes        # delete ALL series under IOTDB_ROOT

Stations and the scripted scenario: STATIONS_FILE (data/stations.json); events are placed
relative to the start (ago_hours), so the last 24 h always contain them. Same --seed, same
data; re-running overwrites identical timestamps (telemetry_v1 continues the source sequence
after the stored consumption watermark; with ghmes the watermark is the newest data time).
"""

import sys

import _common

from app.ingestion.parsers import TelemetryV1Format, build_format
from app.ingestion.pipeline import (
    MqttPublisher,
    Pipeline,
    StdoutWriter,
    run,
    run_until_stopped,
)
from app.ingestion.simulator import SimulatedSource
from app.stations.store import load_directory
from app.timeseries.iotdb import WATERMARK_SERIES, IoTDB


def main() -> None:
    p = _common.parser(__doc__)
    p.add_argument("--target", choices=["iotdb", "mqtt", "stdout"], default="iotdb")
    p.add_argument("--hours", type=float, help="history to generate first (default 48)")
    p.add_argument("--no-live", action="store_true", help="exit after the history")
    p.add_argument("--duration", type=float, metavar="MIN", help="stop after N minutes")
    p.add_argument("--station", action="append",
                   help="only these stations (repeatable; no weather tower / vehicle)")
    p.add_argument("--no-auxiliary", action="store_true",
                   help="leave out the weather tower and the monitoring vehicle")
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

    all_stations, auxiliary = load_directory(s.stations_file)
    stations = [st for st in all_stations if not args.station or st.station_id in args.station]
    if args.station or args.no_auxiliary:
        auxiliary = []
    fmt = build_format(s)
    stored = None
    if args.target != "stdout":
        try:  # continue after what was already consumed, so the watermark never goes back
            latest = run(db.latest(*WATERMARK_SERIES))
            stored = int(latest.value) if latest and latest.value is not None else None
        except Exception as exc:
            print(f"(watermark not readable: {type(exc).__name__})", file=sys.stderr)
    first = stored + 1 if stored is not None and isinstance(fmt, TelemetryV1Format) else 1
    source = SimulatedSource(stations, timezone=s.analysis_timezone,
                             backfill_hours=s.simulator_backfill_hours,
                             realtime=not args.no_live, seed=s.simulator_seed,
                             topic_prefix=s.ingest_topic_prefix, first_sequence=first, fmt=fmt,
                             auxiliary=auxiliary)
    print(f"simulating {len(stations)} stations + {len(auxiliary)} auxiliary sources "
          f"({type(fmt).__name__}) -> {args.target} "
          f"({s.simulator_backfill_hours} h history{', then live' if not args.no_live else ''}); "
          "Ctrl+C to stop", file=sys.stderr)
    duration = args.duration * 60 if args.duration else None

    if args.target == "mqtt":
        run(run_until_stopped(MqttPublisher(s).run(source), duration))
        return
    writer = StdoutWriter() if args.target == "stdout" else db
    pipeline = Pipeline(source, build_format(s), writer, batch_size=s.ingest_batch_size,
                        watermark=stored if args.target == "iotdb" else None)

    async def go() -> None:
        try:
            await run_until_stopped(pipeline.run(), duration)
        finally:
            await db.close()

    run(go())
    print(f"done: {pipeline.stats.as_dict()} watermark={pipeline.watermark}", file=sys.stderr)


if __name__ == "__main__":
    main()
