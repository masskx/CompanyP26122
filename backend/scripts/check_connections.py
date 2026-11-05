#!/usr/bin/env python
"""Check every configured dependency and print what works.

  python scripts/check_connections.py            # everything configured in .env
  python scripts/check_connections.py --no-paid  # skip calls to the LLM / embedding APIs

Exit code 0 when every configured service is reachable, 1 otherwise. Services that are not
configured (e.g. EMBEDDING_PROVIDER=none) are reported as SKIP.
"""

import argparse
import sys
import time
from datetime import UTC, datetime

import _common
from _common import fail, header, ok, skip

from industrial_ai.core.runtime import run_async


def check_iotdb(s) -> bool:
    header(f"IoTDB  {s.iotdb_host}:{s.iotdb_port}  (TIMESERIES_BACKEND={s.timeseries_backend})")
    if s.timeseries_backend != "iotdb":
        skip("memory backend configured")
        return True
    from industrial_ai.storage.catalog import StaticEquipmentCatalog
    from industrial_ai.storage.iotdb_client import IoTDBTimeSeriesStore, count_points

    store = IoTDBTimeSeriesStore.from_settings(s)
    try:
        run_async(store.ping())
        ok("connected")
    except Exception as exc:
        fail(f"cannot connect: {exc.__cause__ or exc}")
        return False
    finally:
        run_async(store.close())
    if s.equipment_catalog_path:
        catalog = StaticEquipmentCatalog.from_file(s.equipment_catalog_path)
        for site_id, equipment_id in sorted(catalog.keys())[:3]:
            equipment = run_async(catalog.get(site_id, equipment_id))
            for metric in equipment.metrics[:1]:
                n, latest = count_points(s, site_id, equipment_id, metric.name)
                when = datetime.fromtimestamp(latest / 1000, UTC).isoformat() if latest else "-"
                ok(f"{site_id}/{equipment_id}/{metric.name}: {n} points, latest {when}")
    return True


def check_opengauss(s) -> bool:
    header(f"openGauss  (SESSION_STORE_BACKEND={s.session_store_backend})")
    if s.session_store_backend != "opengauss":
        skip("memory backend configured (conversations are not persisted)")
        return True
    from industrial_ai.storage.opengauss import applied_migrations

    try:
        versions = run_async(applied_migrations(s.opengauss_dsn.get_secret_value()))
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        print("         (run: python scripts/init_databases.py --opengauss ...)")
        return False
    ok(f"connected; migrations: {', '.join(versions) or 'none'}")
    return True


def check_milvus(s) -> bool:
    header(f"Milvus  {s.milvus_uri or '-'}  alias={s.kb_alias}")
    if not s.milvus_uri:
        skip("MILVUS_URI not set (knowledge search disabled)")
        return True
    from industrial_ai.knowledge.cli import build_store

    store = build_store(s)
    try:
        active = store.active_version()
        ok(f"connected; active knowledge version: {active or 'none yet'}")
        if active:
            meta = store.metadata(active)
            built = meta.get("embedding")
            expected = {"model": s.embedding_model, "dimensions": s.embedding_dimensions}
            if s.embedding_provider == "none":
                skip(f"built with {built}; EMBEDDING_PROVIDER=none so queries are disabled")
            elif built == expected:
                ok(f"embedding matches: {built}, {meta.get('chunks')} chunks")
            else:
                fail(f"index built with {built} but config is {expected}: rebuild the index")
                return False
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return False
    finally:
        store.close()
    return True


def check_embedding(s, paid: bool) -> bool:
    header(f"Embedding  (EMBEDDING_PROVIDER={s.embedding_provider}, {s.embedding_model})")
    if s.embedding_provider == "none":
        skip("not configured")
        return True
    if not paid:
        skip("--no-paid")
        return True
    from industrial_ai.integrations.embedding_client import EmbeddingClient, EmbeddingConfig

    async def probe() -> list[float]:
        client = EmbeddingClient(EmbeddingConfig.from_settings(s))
        try:
            return await client.embed_one("连接测试")
        finally:
            await client.aclose()

    try:
        t0 = time.perf_counter()
        vector = run_async(probe())
        ok(f"{len(vector)}-dim vector in {round((time.perf_counter() - t0) * 1000)} ms")
        return True
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return False


def check_llm(s, paid: bool) -> bool:
    header(f"LLM  (LLM_PROVIDER={s.llm_provider}, {s.resolved_llm_model or '-'})")
    if s.llm_provider == "none":
        skip("not configured (assistant chat returns 503)")
        return True
    if not paid:
        skip("--no-paid")
        return True
    from industrial_ai.integrations.llm_client import LLMClient, LLMConfig

    async def probe() -> str:
        # Streaming, the same path the assistant uses.
        client = LLMClient(LLMConfig.from_settings(s))
        try:
            items = [i async for i in client.stream_chat(
                [{"role": "user", "content": "只回复：OK"}], max_tokens=16)]
            return items[-1].content
        finally:
            await client.aclose()

    try:
        t0 = time.perf_counter()
        reply = run_async(probe())
        ok(f"streamed {reply[:20]!r} in {round((time.perf_counter() - t0) * 1000)} ms")
        return True
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return False


def check_mqtt(s, force: bool) -> bool:
    header(f"MQTT  {s.mqtt_host}:{s.mqtt_port}")
    if s.collector_source != "mqtt" and not force:
        skip("COLLECTOR_SOURCE is not mqtt (use --mqtt to check anyway)")
        return True
    try:
        from industrial_ai.collector.factory import mqtt_config
        from industrial_ai.collector.sources.mqtt import MqttPublisher
    except ImportError:
        fail('aiomqtt not installed: pip install -e ".[mqtt]"')
        return False

    async def probe() -> None:
        async with MqttPublisher(mqtt_config(s)):
            pass

    try:
        run_async(probe())
        ok("connected")
        return True
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    _common.add_env_arg(parser)
    parser.add_argument("--no-paid", action="store_true", help="skip LLM / embedding API calls")
    parser.add_argument("--mqtt", action="store_true", help="check the MQTT broker as well")
    args = parser.parse_args()
    s = _common.load_settings(args)
    print(f"APP_ENV={s.app_env}  identity={s.user_identity_mode}  "
          f"token={'set' if s.compute_service_token else 'NOT SET (API will refuse requests)'}")
    results = [check_iotdb(s), check_opengauss(s), check_milvus(s),
               check_embedding(s, not args.no_paid), check_llm(s, not args.no_paid),
               check_mqtt(s, args.mqtt)]
    print("\nAll configured services are reachable." if all(results)
          else "\nSome checks failed (see above).")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
