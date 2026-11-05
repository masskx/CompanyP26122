#!/usr/bin/env python
"""Generate simulated radiation dose-rate data (synthetic values, not plant data).

  # 48 h of history into IoTDB, then keep generating live readings until Ctrl+C
  python scripts/simulate_data.py

  # history only, then exit
  python scripts/simulate_data.py --backfill-hours 24 --no-realtime

  # run for 30 minutes, then stop
  python scripts/simulate_data.py --duration 30

  # publish to the MQTT broker instead (exercise the full MQTT path with mqtt_ingest.py)
  python scripts/simulate_data.py --target mqtt

  # print instead of storing
  python scripts/simulate_data.py --target stdout --backfill-hours 0.1 --no-realtime

  # remove the simulated series from IoTDB
  python scripts/simulate_data.py --purge --yes

Devices, baselines, scripted events and (simulation-only) thresholds come from the scenario
file (SIMULATOR_SCENARIO_PATH, default examples/simulation/radiation_scenario.json). The
same seed gives the same data. Stop at any time with Ctrl+C: buffered points are flushed.
"""

import argparse
import sys
from pathlib import Path

import _common

from industrial_ai.core.runtime import run_async, run_until_stopped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    _common.add_env_arg(parser)
    parser.add_argument("--target", choices=["iotdb", "mqtt", "stdout"], default="iotdb")
    parser.add_argument("--scenario", type=Path, help="scenario file (SIMULATOR_SCENARIO_PATH)")
    parser.add_argument("--backfill-hours", type=float, help="history to generate first")
    parser.add_argument("--no-realtime", action="store_true", help="exit after the history")
    parser.add_argument("--duration", type=float, metavar="MINUTES", help="stop after N minutes")
    parser.add_argument("--seed", type=int, help="random seed (same seed, same data)")
    parser.add_argument("--init-schema", action="store_true",
                        help="create the IoTDB time series first")
    parser.add_argument("--purge", action="store_true",
                        help="delete the scenario's time series from IoTDB and exit")
    parser.add_argument("--yes", action="store_true", help="confirm --purge")
    args = parser.parse_args()

    scenario_path = args.scenario
    settings = _common.load_settings(
        args, simulator_scenario_path=scenario_path,
        simulator_backfill_hours=args.backfill_hours,
        simulator_realtime=False if args.no_realtime else None,
        simulator_seed=args.seed, collector_source="simulator",
        # Accept every simulated device even if the catalog differs from the scenario.
        equipment_catalog_path=scenario_path)
    if settings.simulator_scenario_path is None:
        sys.exit("set SIMULATOR_SCENARIO_PATH or pass --scenario")

    from industrial_ai.collector.simulation import load_scenario

    scenario = load_scenario(settings.simulator_scenario_path)
    series = [(d.site_id, d.equipment_id, d.metric) for d in scenario.simulation.devices]

    if args.purge:
        if not args.yes:
            sys.exit(f"--purge deletes {len(series)} time series under "
                     f"{settings.iotdb_root_path}; add --yes to confirm")
        from industrial_ai.storage.iotdb_client import delete_series

        print(f"deleted {delete_series(settings, series)} time series")
        return

    if args.init_schema and args.target == "iotdb":
        from industrial_ai.storage.iotdb_client import ensure_schema

        print("schema:", ensure_schema(settings, series))

    duration = args.duration * 60 if args.duration else None
    mode = "history only" if not settings.simulator_realtime else "history + live"
    print(f"simulating {len(series)} devices -> {args.target} ({mode}, "
          f"{settings.simulator_backfill_hours} h history, seed={settings.simulator_seed}). "
          "Ctrl+C to stop.", file=sys.stderr)

    if args.target == "mqtt":
        from industrial_ai.collector.main import publish_simulation

        run_async(run_until_stopped(publish_simulation(settings), duration_seconds=duration))
        return

    from industrial_ai.collector.factory import build_pipeline
    from industrial_ai.collector.writer import StdoutWriter

    writer = StdoutWriter() if args.target == "stdout" else None
    if args.target == "iotdb" and settings.timeseries_backend != "iotdb":
        sys.exit("TIMESERIES_BACKEND is not iotdb; use --target stdout, or run the API with "
                 "API_EMBEDDED_COLLECTOR=true for the in-memory backend")
    pipeline = build_pipeline(settings, writer=writer)

    async def run() -> None:
        try:
            await run_until_stopped(pipeline.run(), duration_seconds=duration)
        finally:
            await pipeline.close()

    run_async(run())
    s = pipeline.stats
    print(f"written={s.written} rejected={dict(s.rejected)} write_failures={s.write_failures}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
