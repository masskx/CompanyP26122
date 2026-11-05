#!/usr/bin/env python
"""End-to-end check of a running backend (start it with scripts/run_api.py first).

  python scripts/smoke_test.py
  python scripts/smoke_test.py --base-url http://127.0.0.1:8000 --skip-chat

Steps: health -> user identity -> equipment health from IoTDB -> maintenance analysis
(rules, SSE) -> assistant chat that must call the data tool -> (if a knowledge base is
active) a question that must retrieve from Milvus with a citation -> history in the
conversation store. Uses COMPUTE_SERVICE_TOKEN from .env.
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

import _common
import httpx

FAILURES: list[str] = []


def check(cond: bool, message: str) -> bool:
    print(f"  [{'OK' if cond else 'FAIL'}]   {message}")
    if not cond:
        FAILURES.append(message)
    return cond


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines()
                      if ": " in line and not line.startswith(":"))
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


def chat(client: httpx.Client, headers: dict, message: str, session_id: str | None = None):
    body = {"message": message, **({"session_id": session_id} if session_id else {})}
    resp = client.post("/internal/v1/assistant/chat", json=body, headers=headers, timeout=180)
    if resp.status_code != 200:
        return resp, [], None
    events = parse_sse(resp.text)
    result = next((d for k, d in events if k == "result"), None)
    return resp, events, result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    _common.add_env_arg(parser)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user", default="smoke-tester")
    parser.add_argument("--site", default="AS01")
    parser.add_argument("--equipment", default="AS01-ISO-RM-02")
    parser.add_argument("--analysis-type", default="isolation_room")
    parser.add_argument("--skip-chat", action="store_true", help="skip LLM calls")
    args = parser.parse_args()
    settings = _common.load_settings(args)
    if settings.compute_service_token is None:
        sys.exit("COMPUTE_SERVICE_TOKEN is not set in .env")
    headers = {"Authorization": f"Bearer {settings.compute_service_token.get_secret_value()}",
               "X-User-Id": args.user, "X-User-Name": "Smoke Tester"}
    client = httpx.Client(base_url=args.base_url, timeout=30)

    _common.header("1. Health")
    try:
        ready = client.get("/health/ready").json()
    except httpx.HTTPError as exc:
        sys.exit(f"cannot reach {args.base_url}: {exc}  (is scripts/run_api.py running?)")
    components = ready["components"]
    check(ready["status"] == "ready", f"ready: {ready['status']}")
    for name in ("timeseries", "session_store", "knowledge_base", "llm"):
        print(f"           {name}: {json.dumps(components.get(name), ensure_ascii=False)}")

    _common.header("2. User identity")
    me = client.get("/internal/v1/users/me", headers=headers)
    check(me.status_code == 200, f"users/me -> {me.status_code} {me.text[:120]}")

    _common.header("3. Equipment health (IoTDB data + rules)")
    health = client.get("/internal/v1/equipment/health", params={"site_id": args.site},
                        headers=headers).json()
    counts = health.get("counts", {})
    print(f"           counts: {counts}")
    with_data = [i for i in health.get("items", []) if i.get("last_seen")]
    if not check(bool(with_data), f"{len(with_data)} devices have data"):
        print("           -> generate data first: python scripts/simulate_data.py")

    _common.header("4. Maintenance analysis (SSE)")
    end = datetime.now(UTC)
    created = client.post("/internal/v1/maintenance/analyses", headers=headers, json={
        "site_id": args.site, "equipment_ids": [args.equipment],
        "analysis_type": args.analysis_type,
        "time_range": {"start": (end - timedelta(hours=6)).isoformat(), "end": end.isoformat()}})
    if check(created.status_code == 202, f"create -> {created.status_code} {created.text[:120]}"):
        aid = created.json()["analysis_id"]
        events = parse_sse(client.get(f"/internal/v1/maintenance/analyses/{aid}/events",
                                      headers=headers, timeout=120).text)
        check(events and events[-1][0] == "done", f"{len(events)} SSE events, ends with done")
        result = client.get(f"/internal/v1/maintenance/analyses/{aid}", headers=headers).json()
        check(result["status"] in ("completed", "partial"),
              f"status={result['status']} health_level={result['health_level']}")
        print(f"           summary: {result.get('risk_summary')}")
        source = f"model {result['model']['name']}" if result.get("model") else "rules"
        print(f"           text by: {source}")

    if args.skip_chat or components.get("llm", {}).get("status") == "not_configured":
        _common.header("5. Assistant  [SKIP] (--skip-chat or LLM_PROVIDER=none)")
    else:
        _common.header("5. Assistant: device data question (agent should use a data tool)")
        resp, events, result = chat(client, headers,
                                    f"{args.equipment} 最近6小时的剂量率情况如何？有没有异常？")
        if check(result is not None, f"chat -> {resp.status_code}; "
                 f"events={sorted({k for k, _ in events})}"):
            print(f"           answer: {result['content'][:300]}")
            print(f"           tools: {result['tools_used']}  "
                  f"citations: {[(c['ref'], c['kind'], c['title']) for c in result['citations']]}")
            check(any(t in result["tools_used"] for t in ("get_device_readings",
                                                          "get_equipment_health")),
                  "agent queried IoTDB through a data tool")
            session_id = result["session_id"]
            history = client.get(f"/internal/v1/assistant/sessions/{session_id}/messages",
                                 headers=headers).json()
            check(len(history.get("items", [])) == 2, "question and answer stored in history")

        if components.get("knowledge_base", {}).get("status") == "ok":
            _common.header("6. Assistant: knowledge question (agent should search Milvus)")
            resp, events, result = chat(client, headers,
                                        "剂量率单位 mSv/h 和 µSv/h 如何换算？请给出依据。")
            if check(result is not None, f"chat -> {resp.status_code}"):
                print(f"           answer: {result['content'][:300]}")
                check("search_knowledge_base" in result["tools_used"], "agent searched Milvus")
                check(any(c["kind"] == "knowledge" for c in result["citations"]),
                      f"knowledge citations: {[c.get('doc_id') for c in result['citations']]}")
        else:
            _common.header("6. Knowledge question  [SKIP] (no active knowledge base)")

    print("\nSMOKE TEST PASSED" if not FAILURES else f"\nSMOKE TEST FAILED: {len(FAILURES)}")
    sys.exit(0 if not FAILURES else 1)


if __name__ == "__main__":
    main()
