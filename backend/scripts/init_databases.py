#!/usr/bin/env python
"""Initialise the databases used by the backend. Safe to run repeatedly.

  python scripts/init_databases.py --all
  python scripts/init_databases.py --opengauss --opengauss-admin-dsn \
      "host=127.0.0.1 port=15432 dbname=postgres user=gaussdb password=..."
  python scripts/init_databases.py --milvus --sample-knowledge

IoTDB:     creates the database (IOTDB_ROOT_PATH) and one DOUBLE time series per metric of
           every device in EQUIPMENT_CATALOG_PATH (needed if auto schema creation is off).
openGauss: optionally creates the app user / PG-compatible database / schema from an admin
           login, then applies the migrations (tables for users, sessions, messages, ...).
Milvus:    checks the connection and lists knowledge versions; --sample-knowledge builds the
           example knowledge base (needs EMBEDDING_PROVIDER configured).
"""

import argparse
import sys

import _common
from _common import fail, header, ok, skip

from industrial_ai.core.runtime import run_async


def init_iotdb(settings) -> bool:
    header(f"IoTDB  {settings.iotdb_host}:{settings.iotdb_port}  root={settings.iotdb_root_path}")
    from industrial_ai.storage.catalog import StaticEquipmentCatalog
    from industrial_ai.storage.iotdb_client import ensure_schema

    if settings.equipment_catalog_path is None:
        fail("EQUIPMENT_CATALOG_PATH is not set; cannot know which series to create")
        return False
    catalog = StaticEquipmentCatalog.from_file(settings.equipment_catalog_path)
    series = []
    for site_id, equipment_id in sorted(catalog.keys()):
        equipment = run_async(catalog.get(site_id, equipment_id))
        series += [(site_id, equipment_id, m.name) for m in equipment.metrics]
    try:
        result = ensure_schema(settings, series)
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return False
    ok(f"database {settings.iotdb_root_path}; time series created={result['created']} "
       f"existing={result['existing']}")
    return True


def init_opengauss(settings, admin_dsn: str | None) -> bool:
    header("openGauss")
    from industrial_ai.storage.opengauss import (
        DatabaseCompatibilityError,
        applied_migrations,
        bootstrap,
        migrate,
    )

    if settings.opengauss_dsn is None:
        fail("OPENGAUSS_DSN is not set")
        return False
    dsn = settings.opengauss_dsn.get_secret_value()
    try:
        if admin_dsn:
            actions = run_async(bootstrap(admin_dsn, dsn))
            ok("bootstrap: " + (", ".join(actions) if actions else "user/database/schema exist"))
        applied = run_async(migrate(dsn))
        ok("migrations applied: " + (", ".join(applied) if applied else "none (up to date)"))
        ok("schema version: " + ", ".join(run_async(applied_migrations(dsn))))
    except DatabaseCompatibilityError as exc:
        fail(str(exc))
        return False
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        if not admin_dsn:
            print("         (first run? add --opengauss-admin-dsn to create the user/database)")
        return False
    return True


def init_milvus(settings, sample_knowledge: bool) -> bool:
    header(f"Milvus  {settings.milvus_uri}  alias={settings.kb_alias}")
    if not settings.milvus_uri:
        skip("MILVUS_URI is not set")
        return True
    from industrial_ai.knowledge.cli import build_embedder, build_store

    store = build_store(settings)
    try:
        versions = store.versions()
        ok(f"connected; versions={[v['version'] for v in versions]} "
           f"active={store.active_version()}")
        manifest = _common.BACKEND_DIR / "examples" / "knowledge" / "manifest.json"
        sample_version = None
        if sample_knowledge:
            from industrial_ai.knowledge.manifest import load_manifest

            sample_version = load_manifest(manifest)[0].version
        if sample_knowledge and store.exists(sample_version):
            if store.active_version() is None:
                store.activate(sample_version)
            ok(f"sample knowledge already built (version {sample_version}); "
               "use industrial-ai-kb to manage versions")
        elif sample_knowledge:
            from industrial_ai.knowledge.builder import build_knowledge_base

            async def build():
                # One event loop for the whole build: the HTTP client is bound to it.
                embedder = build_embedder(settings)
                try:
                    return await build_knowledge_base(
                        manifest, store, embedder, chunk_chars=settings.kb_chunk_chars,
                        chunk_overlap=settings.kb_chunk_overlap)
                finally:
                    await embedder.aclose()

            report = run_async(build())
            ok(f"sample knowledge built: version={report.version} chunks={report.chunks} "
               f"embedding={report.embedding} active")
    except SystemExit:
        raise
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return False
    finally:
        store.close()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    _common.add_env_arg(parser)
    parser.add_argument("--all", action="store_true", help="IoTDB + openGauss + Milvus")
    parser.add_argument("--iotdb", action="store_true")
    parser.add_argument("--opengauss", action="store_true")
    parser.add_argument("--milvus", action="store_true")
    parser.add_argument("--opengauss-admin-dsn",
                        help="admin login used once to create the app user/database/schema")
    parser.add_argument("--sample-knowledge", action="store_true",
                        help="build examples/knowledge into Milvus (calls the embedding model)")
    args = parser.parse_args()
    if not (args.all or args.iotdb or args.opengauss or args.milvus):
        parser.error("choose --all or at least one of --iotdb / --opengauss / --milvus")
    settings = _common.load_settings(args)

    results = []
    if args.all or args.iotdb:
        results.append(init_iotdb(settings))
    if args.all or args.opengauss:
        results.append(init_opengauss(settings, args.opengauss_admin_dsn))
    if args.all or args.milvus:
        results.append(init_milvus(settings, args.sample_knowledge))
    print("\nDone." if all(results) else "\nSome steps failed (see above).")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
