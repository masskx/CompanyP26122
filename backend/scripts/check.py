#!/usr/bin/env python
"""Check every dependency, and optionally a running API end to end.

  python scripts/check.py                    # IoTDB, openGauss, Milvus, embedding, LLM
  python scripts/check.py --no-paid          # skip the LLM / embedding API calls
  python scripts/check.py --mqtt             # also the MQTT broker
  python scripts/check.py --api http://127.0.0.1:8000   # + end-to-end test of the server
                                             (runs / re-uses yesterday's analysis batch)

Exit code 0 when everything configured works.
"""

import sys
from datetime import UTC, datetime

import _common
import httpx
from _common import fail, ok, skip

from app.ingestion.pipeline import run

FAILED: list[str] = []


def check(cond: bool, msg: str) -> bool:
    (ok if cond else fail)(msg)
    if not cond:
        FAILED.append(msg)
    return cond


def services(s, paid: bool, mqtt: bool) -> None:
    from app.stations.store import load_file
    from app.timeseries.iotdb import IoTDB

    print(f"== IoTDB {s.iotdb_host}:{s.iotdb_port}")
    db = IoTDB(s)
    try:
        run(db.ping())
        ok("connected")
        for st in load_file(s.stations_file):
            if not st.devices:
                continue
            d = st.devices[0]
            p = run(db.latest(st.station_id, d.device_id, "dose_rate"))
            ok(f"{st.station_id}/{d.device_id}: latest {p.value if p else '-'} at "
               f"{p.time.isoformat() if p else 'no data yet (run scripts/simulate.py)'}")
    except Exception as exc:
        check(False, f"IoTDB: {exc}")
    finally:
        run(db.close())

    print("== openGauss")
    if s.opengauss_dsn is None:
        skip("OPENGAUSS_DSN not set (conversations in memory)")
    else:
        import psycopg

        try:
            with psycopg.connect(s.opengauss_dsn.get_secret_value(), connect_timeout=5) as c:
                versions = [r[0] for r in c.execute("SELECT version FROM app_migrations")]
                n = c.execute("SELECT COUNT(*) FROM station_config").fetchone()[0] \
                    if "002_station_analysis" in versions else 0
            check({"001_chat", "002_station_analysis"} <= set(versions),
                  f"connected; migrations {versions}; {n} stations configured")
        except Exception as exc:
            check(False, f"{exc} (run scripts/init_db.py)")

    print(f"== Milvus {s.milvus_uri or '-'}")
    if not s.milvus_uri:
        skip("MILVUS_URI not set (knowledge search off)")
    else:
        from app.rag.store import KnowledgeStore

        store = KnowledgeStore(s.milvus_uri, s.kb_name)
        try:
            active = store.active_version()
            ok(f"connected; active version: {active or 'none (run scripts/build_knowledge.py)'}")
            if active:
                built = store.metadata(active).get("embedding")
                want = {"model": s.embedding_model, "dimensions": s.embedding_dimensions}
                check(built == want, f"embedding of the index {built} vs config {want}")
        except Exception as exc:
            check(False, f"Milvus: {exc}")
        finally:
            store.close()

    print(f"== Embedding ({s.embedding_provider})")
    if s.embedding_provider == "none" or not paid:
        skip("not configured" if s.embedding_provider == "none" else "--no-paid")
    else:
        from app.rag.embeddings import GLMEmbeddings

        try:
            v = GLMEmbeddings.from_settings(s).embed_query("连接测试")
            ok(f"{len(v)}-dim vector")
        except Exception as exc:
            check(False, f"embedding: {exc}")

    print(f"== LLM ({s.llm_provider} {s.llm_model_name or ''})")
    if s.llm_provider == "none" or not paid:
        skip("not configured" if s.llm_provider == "none" else "--no-paid")
    else:
        from app.agent.llm import build_llm

        try:
            reply = build_llm(s).invoke("只回复：OK")
            ok(f"replied {reply.content[:20]!r}")
        except Exception as exc:
            check(False, f"LLM: {exc}")

    if mqtt:
        print(f"== MQTT {s.mqtt_host}:{s.mqtt_port}")
        try:
            import aiomqtt

            async def probe() -> None:
                async with aiomqtt.Client(hostname=s.mqtt_host, port=s.mqtt_port):
                    pass

            run(probe())
            ok("connected")
        except Exception as exc:
            check(False, f"MQTT: {exc}")


def api(base: str, s) -> None:
    headers = {"X-User-Id": "check-script"}
    if s.api_key:
        headers["X-API-Key"] = s.api_key.get_secret_value()
    c = httpx.Client(base_url=f"{base.rstrip('/')}/api/v1", headers=headers, timeout=180)
    print(f"== API {base}")
    try:
        h = c.get("/health").json()
    except httpx.HTTPError as exc:
        sys.exit(f"cannot reach {base}: {exc} (start it with scripts/run.py)")
    check(h["status"] == "ok", f"health: {h['status']} {h['checks']}")
    stations = c.get("/stations").json()
    check(len(stations) > 0, f"stations: {[x['station_id'] for x in stations]}")
    st = stations[0]
    dev = st["devices"][0]["device_id"] if st["devices"] else None
    t = c.get(f"/stations/{st['station_id']}/telemetry",
              params={"device_id": dev, "interval": 600}).json()
    check(len(t.get("points", [])) > 0, f"telemetry {st['station_id']}/{dev}: "
          f"{len(t.get('points', []))} buckets")
    status = c.get("/analysis/status").json()
    ok(f"analysis status: yesterday={status['yesterday']} schedule={status['schedule']}")
    r = c.post("/analysis/batches", json={})
    check(r.status_code in (200, 202), f"POST /analysis/batches -> {r.status_code}")
    batch = r.json()["batch"]
    import time

    for _ in range(120):
        if batch["status"] != "running":
            break
        time.sleep(2)
        batch = c.get(f"/analysis/batches/{batch['batch_id']}").json()
    check(batch["status"] in ("succeeded", "partial"),
          f"batch {batch['batch_id']}: {batch['status']} {batch['counts']}")
    recs = c.get("/analysis/records", params={"batch_id": batch["batch_id"]}).json()
    check(recs["total"] > 0 or not batch["counts"].get("devices_analysable"),
          f"records: {recs['total']} {[(x['device_id'], x['level']) for x in recs['items']]}")
    if recs["items"]:
        detail = c.get(f"/analysis/records/{recs['items'][0]['record_id']}").json()
        ex = detail.get("explanation") or {}
        check(len(detail["events"]) > 0, f"record detail: {len(detail['events'])} events, "
              f"explanation {ex.get('method')} confidence {ex.get('confidence')}")

    if h["checks"]["llm"]["status"] != "configured":
        skip("chat (LLM_PROVIDER=none)")
        return
    r = c.post("/chat", json={"message": "昨天的分析结果中哪些设备需要检修？",
                              "stream": False})
    if check(r.status_code == 200, f"chat (data question) -> {r.status_code}"):
        m = r.json()["message"]
        print(f"         answer: {m['content'][:200]!r}")
        check(any(t.startswith(("get_", "list_")) for t in m["tools_used"]),
              f"agent used data tools {m['tools_used']}; citations "
              f"{[x['title'] for x in m['citations']]}")
        sid = r.json()["session_id"]
        msgs = c.get(f"/sessions/{sid}/messages").json()
        check(len(msgs) == 2, "question + answer stored")
    if h["checks"]["knowledge"]["status"] == "ok":
        r = c.post("/chat", json={"message": "剂量率单位 mGy/h 和 µGy/h 如何换算？请给出依据。",
                                  "stream": False})
        if check(r.status_code == 200, f"chat (knowledge question) -> {r.status_code}"):
            m = r.json()["message"]
            print(f"         answer: {m['content'][:200]!r}")
            check("search_knowledge" in m["tools_used"]
                  and any(x["kind"] == "knowledge" for x in m["citations"]),
                  f"agent searched Milvus; cited {[x.get('doc_id') for x in m['citations']]}")
    else:
        skip("knowledge question (knowledge base not ready)")


def main() -> None:
    p = _common.parser(__doc__)
    p.add_argument("--no-paid", action="store_true")
    p.add_argument("--mqtt", action="store_true")
    p.add_argument("--api", metavar="URL")
    args = p.parse_args()
    s = _common.settings(args)
    print(f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC  env={s.app_env}")
    services(s, not args.no_paid, args.mqtt)
    if args.api:
        api(args.api, s)
    print("\nALL CHECKS PASSED" if not FAILED else f"\n{len(FAILED)} CHECK(S) FAILED")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
