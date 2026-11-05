#!/usr/bin/env python
"""Initialise openGauss (conversations) and IoTDB (device time series). Safe to re-run.

  python scripts/init_db.py --admin-dsn "host=... port=... dbname=postgres user=... password=..."
  python scripts/init_db.py                 # later runs: migrations + IoTDB schema only
  python scripts/init_db.py --only iotdb

openGauss: with --admin-dsn, creates the user / DBCOMPATIBILITY 'PG' database / schema from
OPENGAUSS_DSN, then applies app/db/migrations/*.sql.
IoTDB: creates the database and one DOUBLE series per metric of every device in DEVICES_FILE.
The vector database is initialised separately: scripts/build_knowledge.py
"""

import sys

import _common

from app.db.migrate import CompatibilityError, bootstrap, migrate
from app.ingestion.pipeline import run
from app.timeseries.devices import DeviceCatalog
from app.timeseries.iotdb import IoTDB


def main() -> None:
    p = _common.parser(__doc__)
    p.add_argument("--admin-dsn", help="openGauss admin login (first run only)")
    p.add_argument("--only", choices=["opengauss", "iotdb"])
    args = p.parse_args()
    s = _common.settings(args)
    failed = False

    if args.only in (None, "opengauss"):
        print("== openGauss")
        if s.opengauss_dsn is None:
            _common.skip("OPENGAUSS_DSN not set (conversations stay in memory)")
        else:
            dsn = s.opengauss_dsn.get_secret_value()
            try:
                if args.admin_dsn:
                    actions = run(bootstrap(args.admin_dsn, dsn))
                    _common.ok("bootstrap: " + (", ".join(actions) or "already set up"))
                applied = run(migrate(dsn))
                _common.ok("migrations: " + (", ".join(applied) or "up to date"))
            except CompatibilityError as exc:
                _common.fail(str(exc))
                failed = True
            except Exception as exc:
                _common.fail(f"{type(exc).__name__}: {exc}")
                print("         first run? add --admin-dsn to create the user/database")
                failed = True

    if args.only in (None, "iotdb"):
        print(f"== IoTDB {s.iotdb_host}:{s.iotdb_port}")
        catalog = DeviceCatalog.load(s.devices_file)
        db = IoTDB(s)
        try:
            result = run(db.ensure_schema(catalog.series()))
            _common.ok(f"{s.iotdb_root}: series created={result['created']} "
                       f"existing={result['existing']}")
        except Exception as exc:
            _common.fail(f"{type(exc).__name__}: {exc}")
            failed = True
        finally:
            run(db.close())
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
