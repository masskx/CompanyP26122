#!/usr/bin/env python
"""Run the maintenance analysis locally, the way the main server calls it
(POST /internal/v1/maintenance/analyses): useful to try rules and explanations without the
main server.

  python scripts/analyze.py --site AS06                       # general, last 24 h
  python scripts/analyze.py --site AS03 --type detector --hours 12
  python scripts/analyze.py --all                             # every station, summary table
  python scripts/analyze.py --site AS06 --json                # the exact response JSON
  python scripts/analyze.py --all --no-llm                    # rule templates only

Nothing is stored (the main server owns results and work orders). Uses the configured LLM
for explanations (LLM_PROVIDER); the current MQTT watermark is sent as mqtt_watermark.
"""

import sys
import uuid
from datetime import UTC, datetime, timedelta

import _common

from app.deps import build_resources
from app.ingestion.pipeline import run
from app.maintenance.schemas import AnalysisError, AnalysisRequest


async def go(args, s) -> int:
    r = build_resources(s)
    try:
        sites = [st.station_id for st in await r.stations.all()] if args.all else [args.site]
        end = datetime.now(UTC)
        start = end - timedelta(hours=args.hours)
        watermark = await r.station_data.watermark()
        failed = 0
        for site in sites:
            req = AnalysisRequest(analysis_id=f"cli-{uuid.uuid4().hex[:10]}", site_id=site,
                                  analysis_type=args.type,
                                  observation_window={"start": start, "end": end},
                                  mqtt_watermark=str(watermark) if watermark else None)
            try:
                res = await r.maintenance.analyze(req)
            except AnalysisError as exc:
                print(f"{site:6} ERROR {exc.status} {exc.code}: {exc.message}")
                failed += 1
                continue
            if args.json:
                print(res.model_dump_json(indent=2))
                continue
            print(f"{site:6} {res.health_level:17} {res.explanation_source:8} "
                  f"completeness={res.data_snapshot.completeness:.3f}")
            for f in res.findings:
                print(f"         finding  {f.severity:7} {f.rule_id}: {f.message}")
            for a in res.actions:
                print(f"         action   {a.priority:9} {a.text}")
            for lim in res.limitations:
                print(f"         limit    {lim.text}")
            if not args.all:
                print(f"         explain  {res.explanation}")
        return 1 if failed else 0
    finally:
        await r.close()


def main() -> None:
    p = _common.parser(__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--site", help="station id, e.g. AS06")
    g.add_argument("--all", action="store_true", help="every station")
    p.add_argument("--type", choices=["detector", "backup_power", "general"], default="general")
    p.add_argument("--hours", type=float, default=24, help="observation window (default 24)")
    p.add_argument("--json", action="store_true", help="print the response JSON")
    p.add_argument("--no-llm", action="store_true", help="rule template explanations")
    args = p.parse_args()
    s = _common.settings(args, maintenance_use_llm=False if args.no_llm else None)
    sys.exit(run(go(args, s)))


if __name__ == "__main__":
    main()
