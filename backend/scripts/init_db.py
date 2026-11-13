#!/usr/bin/env python
"""Initialise openGauss and IoTDB. Safe to re-run.

  python scripts/init_db.py --admin-dsn "host=... port=... dbname=postgres user=... password=..."
  python scripts/init_db.py                 # later runs: migrations + IoTDB series
  python scripts/init_db.py --only iotdb

openGauss stores only conversations (sessions, messages, report records) and the copy of
the main server's work orders. With --admin-dsn this creates the user / DBCOMPATIBILITY 'PG'
database / schema from OPENGAUSS_DSN; then it applies app/db/migrations/*.sql
(003 removes the tables of version 0.3: station_config, analysis_*).
IoTDB: creates the database, the series of every station in STATIONS_FILE (detector,
backup-power and environment units) and the MQTT consumption-watermark series.
The vector database is initialised separately: scripts/build_knowledge.py
"""

import sys

import _common

from app.db.migrate import CompatibilityError, bootstrap, migrate
from app.ingestion.pipeline import run
from app.stations.store import load_file
from app.timeseries.iotdb import WATERMARK_SERIES, IoTDB


async def server_encoding(dsn: str) -> str:
    from psycopg import AsyncConnection

    async with await AsyncConnection.connect(dsn) as conn:
        return (await (await conn.execute("SHOW server_encoding")).fetchone())[0]


def main() -> None:
    p = _common.parser(__doc__)
    p.add_argument("--admin-dsn", help="openGauss admin login (first run only)")
    p.add_argument("--only", choices=["opengauss", "iotdb"])
    args = p.parse_args()
    s = _common.settings(args)
    failed = False
    dsn = s.opengauss_dsn.get_secret_value() if s.opengauss_dsn else None

    if args.only in (None, "opengauss"):
        print("== openGauss")
        if dsn is None:
            _common.skip("OPENGAUSS_DSN not set (conversations and work orders in memory)")
        else:
            try:
                if args.admin_dsn:
                    actions = run(bootstrap(args.admin_dsn, dsn))
                    _common.ok("bootstrap: " + (", ".join(actions) or "already set up"))
                applied = run(migrate(dsn))
                _common.ok("migrations: " + (", ".join(applied) or "up to date"))
                encoding = run(server_encoding(dsn))
                if encoding.upper().replace("-", "") != "UTF8":
                    print(f"  [WARN] database encoding is {encoding}, not UTF8: Chinese text may "
                          "fail to store. Recreate the database with ENCODING 'UTF8' "
                          "(init_db.py --admin-dsn does this for new ones)")
            except CompatibilityError as exc:
                _common.fail(str(exc))
                failed = True
            except Exception as exc:
                _common.fail(f"{type(exc).__name__}: {exc}")
                print("         first run? add --admin-dsn to create the user/database")
                failed = True

    if args.only in (None, "iotdb"):
        print(f"== IoTDB {s.iotdb_host}:{s.iotdb_port}")
        db = IoTDB(s)
        try:
            stations = load_file(s.stations_file)
            series = [x for st in stations for x in st.series()] + [WATERMARK_SERIES]
            result = run(db.ensure_schema(series))
            _common.ok(f"{s.iotdb_root}: {len(stations)} stations, series "
                       f"created={result['created']} existing={result['existing']}")
        except Exception as exc:
            _common.fail(f"{type(exc).__name__}: {exc}")
            failed = True
        finally:
            run(db.close())
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
