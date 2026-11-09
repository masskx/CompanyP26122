#!/usr/bin/env python
"""Initialise openGauss and IoTDB. Safe to re-run.

  python scripts/init_db.py --admin-dsn "host=... port=... dbname=postgres user=... password=..."
  python scripts/init_db.py                     # later runs: migrations, new stations, IoTDB series
  python scripts/init_db.py --update-stations   # also overwrite stations from STATIONS_FILE
  python scripts/init_db.py --only iotdb

openGauss: with --admin-dsn, creates the user / DBCOMPATIBILITY 'PG' database / schema from
OPENGAUSS_DSN; then applies app/db/migrations/*.sql (001 conversations, 002 station
configuration + analysis batch / record / event) and inserts the stations of STATIONS_FILE
that are not in station_config yet (existing rows are kept unless --update-stations).
IoTDB: creates the database and the DOUBLE series of every station and device.
The vector database is initialised separately: scripts/build_knowledge.py
"""

import sys

import _common

from app.db.migrate import CompatibilityError, bootstrap, migrate
from app.db.pool import PgPool
from app.ingestion.pipeline import run
from app.stations.store import PgStationStore, load_file
from app.timeseries.iotdb import IoTDB


async def seed(dsn: str, path, overwrite: bool) -> dict[str, str]:
    pool = PgPool(dsn)
    try:
        return await PgStationStore(pool).upsert(load_file(path), overwrite=overwrite)
    finally:
        await pool.close()


async def server_encoding(dsn: str) -> str:
    from psycopg import AsyncConnection

    async with await AsyncConnection.connect(dsn) as conn:
        return (await (await conn.execute("SHOW server_encoding")).fetchone())[0]


async def stations_in_db(dsn: str):
    pool = PgPool(dsn)
    try:
        return await PgStationStore(pool).all(include_disabled=True)
    finally:
        await pool.close()


def main() -> None:
    p = _common.parser(__doc__)
    p.add_argument("--admin-dsn", help="openGauss admin login (first run only)")
    p.add_argument("--only", choices=["opengauss", "iotdb"])
    p.add_argument("--update-stations", action="store_true",
                   help="overwrite existing station rows from STATIONS_FILE")
    args = p.parse_args()
    s = _common.settings(args)
    failed = False
    dsn = s.opengauss_dsn.get_secret_value() if s.opengauss_dsn else None

    if args.only in (None, "opengauss"):
        print("== openGauss")
        if dsn is None:
            _common.skip("OPENGAUSS_DSN not set (stations from file, results in memory)")
        else:
            try:
                if args.admin_dsn:
                    actions = run(bootstrap(args.admin_dsn, dsn))
                    _common.ok("bootstrap: " + (", ".join(actions) or "already set up"))
                applied = run(migrate(dsn))
                _common.ok("migrations: " + (", ".join(applied) or "up to date"))
                encoding = run(server_encoding(dsn))
                if encoding.upper().replace("-", "") != "UTF8":
                    print(f"  [WARN] database encoding is {encoding}, not UTF8: Chinese station "
                          "names and analysis text may fail to store. Recreate the database "
                          "with ENCODING 'UTF8' (init_db.py --admin-dsn does this for new ones)")
                result = run(seed(dsn, s.stations_file, args.update_stations))
                _common.ok(f"stations from {s.stations_file.name}: " +
                           ", ".join(f"{k} {v}" for k, v in result.items()))
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
            if dsn:
                try:
                    known = {st.station_id for st in stations}
                    stations += [st for st in run(stations_in_db(dsn))
                                 if st.station_id not in known]
                except Exception as exc:
                    _common.skip(f"station_config not readable ({type(exc).__name__}); "
                                 "using the file only")
            series = [x for st in stations for x in st.series()]
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
