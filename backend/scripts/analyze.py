#!/usr/bin/env python
"""Run the daily station analysis once (the API also runs it every day at ANALYSIS_RUN_AT).

  python scripts/analyze.py                     # yesterday (ANALYSIS_TIMEZONE)
  python scripts/analyze.py --date 2026-11-08
  python scripts/analyze.py --force             # new revision even if the day was analysed
  python scripts/analyze.py --show              # print every record with its events

Results go to openGauss (OPENGAUSS_DSN); without it they are only printed.
Uses the configured LLM for explanations (LLM_PROVIDER); --no-llm forces templates.
"""

import sys
from datetime import date, timedelta

import _common

from app.deps import build_resources
from app.ingestion.pipeline import run


async def go(args, s) -> int:
    r = build_resources(s)
    try:
        day = date.fromisoformat(args.date) if args.date else \
            r.analysis.today() - timedelta(days=1)
        batch = await r.analysis.run_now(day, force=args.force)
        if batch is None:
            print("batch not found")
            return 1
        print(f"batch {batch.batch_id}: {batch.status.value} (trigger {batch.trigger}, "
              f"attempt {batch.attempts})")
        print(f"  counts: {batch.counts}")
        for g in batch.config_gaps:
            print(f"  gap    {g['station_id']} {g['scope']}: {g['message']}")
        for e in batch.errors:
            print(f"  error  {e.get('station_id')}/{e.get('device_id')}: {e['error']}")
        records, _ = await r.analysis.store.list_records(batch.batch_id, {}, 200, 0)
        for rec in records:
            ex = rec.explanation
            print(f"  [{rec.level.value:17}] {rec.station_id}/{rec.device_id:8} {rec.title}"
                  f"  ({ex.method if ex else '-'}, confidence {ex.confidence if ex else '-'})")
            if args.show:
                full = await r.analysis.store.get_record(rec.record_id)
                for ev in full.events or []:
                    tz = r.analysis.tz
                    print(f"      {ev.code} {ev.start.astimezone(tz):%H:%M:%S}-"
                          f"{ev.end.astimezone(tz):%H:%M:%S} {ev.kind} {ev.level.value} "
                          f"{ev.source.value} n={ev.sample_count} max={ev.max}")
                if ex:
                    print(f"      summary: {ex.summary}")
                    if ex.error:
                        print(f"      fallback reason: {ex.error}")
        return 0 if batch.status.value in ("succeeded", "partial") else 1
    finally:
        await r.close()


def main() -> None:
    p = _common.parser(__doc__)
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    p.add_argument("--force", action="store_true")
    p.add_argument("--show", action="store_true")
    p.add_argument("--no-llm", action="store_true")
    args = p.parse_args()
    s = _common.settings(args, analysis_use_llm=False if args.no_llm else None)
    sys.exit(run(go(args, s)))


if __name__ == "__main__":
    main()
