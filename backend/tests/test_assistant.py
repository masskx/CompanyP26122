"""Assistant preset workflows (LangGraph router + workflow nodes) with a scripted model:
routing, stage / result events, Excel reports, conversation ownership and claiming."""

import asyncio
import io
import json

import pytest
from langchain_core.messages import AIMessage
from openpyxl import load_workbook

from app.api.chat import _with_heartbeat

from .conftest import parse_sse

pytestmark = pytest.mark.anyio
CLIENT_A = {"X-Client-Id": "browser-a"}


def params(**kw) -> AIMessage:
    base = {"site_ids": [], "start": None, "end": None, "metrics": [], "unit": None,
            "decimals": None, "granularity": None}
    return AIMessage(content=json.dumps({**base, **kw}, ensure_ascii=False))


async def chat(client, message, headers=CLIENT_A, **kw):
    r = await client.post("/chat", json={"message": message, **kw}, headers=headers)
    assert r.status_code == 200, r.text
    return parse_sse(r.text, keep_seq=True)


async def test_workflow_list(make_client):
    client, _ = await make_client(None)
    items = (await client.get("/assistant/workflows")).json()
    assert [i["id"] for i in items] == ["monitoring_report", "system_info", "dose_assessment",
                                        "maintenance_advice"]


async def test_system_info_workflow(make_client):
    client, llm = await make_client([params(), AIMessage(content="共 18 个站点 [1]。")])
    events = await chat(client, "请汇总全网站点运行状态")   # keyword route
    kinds = [k for k, _ in events]
    assert kinds[0] == "session" and kinds[-1] == "done" and "tool" not in kinds
    seqs = [d["seq"] for _, d in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    stages = [d["label"] for k, d in events if k == "stage"]
    assert stages[:2] == ["识别意图", "工作流：MES 系统信息"] and stages[-1] == "生成回答"
    result = next(d for k, d in events if k == "result")
    assert result["type"] == "system_info" and result["counts"]["total"] == 18
    assert result["counts"]["fault"] == 1 and result["counts"]["on_battery"] == 1
    done = events[-1][1]
    assert done["workflow"] == "system_info" and done["message"]["citations"][0]["ref"] == 1
    facts = llm.calls[-1]["messages"][-1].content
    assert "全网站点运行状态" in facts and '"AS08"' in facts and "设备故障" in facts
    sessions = (await client.get("/sessions", headers=CLIENT_A)).json()
    assert sessions[0]["workflow"] == "system_info"


async def test_report_workflow_and_download(make_client, store):
    end = store.now.astimezone(store.now.tzinfo)
    client, llm = await make_client([
        params(site_ids=["AS01", "XX99"], metrics=["dose_rate", "battery_voltage"], decimals=2,
               granularity="hour",
               start=(end.replace(minute=0, second=0, microsecond=0)).strftime("%Y-%m-%d %H:%M")),
        AIMessage(content="报表已生成 [1]。")], settings_=None)
    # explicit workflow (entry card), start given, end missing -> yesterday default replaced
    events = await chat(client, "帮我导出 AS01 的数据", workflow="monitoring_report")
    result = next(d for k, d in events if k == "result")
    assert result["type"] == "report" and result["download_url"].startswith("/api/v1/reports/")
    path = result["download_url"].removeprefix("/api/v1")
    r = await client.get(path, headers=CLIENT_A)
    assert r.status_code == 200 and r.headers["content-type"].startswith(
        "application/vnd.openxmlformats")
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["报表说明", "监测统计", "数据质量"]
    rows = list(wb["监测统计"].iter_rows(values_only=True))
    assert rows[0][:6] == ("站点", "站点名称", "设备", "指标", "单位", "时间")
    assert {row[2] for row in rows[1:]} == {"AS01-DET-01", "AS01-BAT-01"}
    assert all(row[6] is None or round(row[6], 2) == row[6] for row in rows[1:])  # 2 decimals
    info = dict(wb["报表说明"].iter_rows(values_only=True))
    assert info["保留小数位"] == 2 and info["统计口径"] == "小时值"
    listed = (await client.get("/reports", headers=CLIENT_A)).json()
    assert [x["report_id"] for x in listed] == [result["report_id"]]
    assert (await client.get(path, headers={"X-Client-Id": "browser-b"})).status_code == 404
    assert (await client.get(path)).status_code == 404  # anonymous


async def test_dose_assessment_needs_a_station(make_client):
    client, llm = await make_client([params(), AIMessage(content="请告诉我站点。")])
    await chat(client, "做一次剂量率评估", workflow="dose_assessment")
    assert "缺少参数：评估站点" in llm.calls[-1]["messages"][-1].content


async def test_dose_assessment_comparisons(make_client):
    client, llm = await make_client([params(site_ids=["AS06"]), AIMessage(content="评估 [1]。")])
    events = await chat(client, "评估 AS06 最近1天的剂量率，对比上月与去年同期")
    assert events[-1][1]["workflow"] == "dose_assessment"
    facts = llm.calls[-1]["messages"][-1].content
    data = json.loads(facts[facts.index("[{"):])
    entry = data[0]
    assert entry["设备"] == "AS06-DET-01" and entry["本期"]["样本数"] > 1000
    assert entry["上月同期"]["状态"].startswith("无历史数据")       # never 0
    assert entry["阈值"] == {"一级": 0.25, "二级": 0.4} and entry["本底值"] == 0.064


async def test_maintenance_advice_workflow(make_client):
    client, llm = await make_client([params(), AIMessage(content="优先 AS03 [1]。")])
    events = await chat(client, "逐站分析设备健康，给出维修建议与优先级")
    result = next(d for k, d in events if k == "result")
    assert result["counts"] == {"inspect": 3, "watch": 5, "insufficient_data": 2, "normal": 8}
    facts = llm.calls[-1]["messages"][-1].content
    order = [line for line in facts.split('"站点": "')[1:]]
    assert order[0].startswith("AS03") or order[0].startswith("AS06") \
        or order[0].startswith("AS08")   # inspect first
    assert len(llm.calls) == 2  # parameters + narration: no model call per station


async def test_model_routing(make_client, settings):
    s = settings.model_copy(update={"assistant_intent_model": True})
    client, llm = await make_client([AIMessage(content='{"workflow": "dose_assessment"}'),
                                     params(site_ids=["AS01"]), AIMessage(content="好。")],
                                    settings_=s)
    events = await chat(client, "AS01 上个月和去年比怎么样")
    assert events[-1][1]["workflow"] == "dose_assessment"
    client, llm = await make_client([AIMessage(content="不是 JSON"), AIMessage(content="你好！")],
                                    settings_=s)
    events = await chat(client, "你好")
    assert events[-1][1]["workflow"] == "general" and llm.calls[-1]["tools"] is True


async def test_client_identity_and_claim(make_client):
    client, _ = await make_client([AIMessage(content="一。"), AIMessage(content="二。")])
    await chat(client, "问题一", workflow="general")
    await chat(client, "问题二", headers={"X-Client-Id": "browser-b"}, workflow="general")
    mine = (await client.get("/sessions", headers=CLIENT_A)).json()
    assert [s["title"] for s in mine] == ["问题一"]
    assert (await client.get("/sessions")).json() == []
    # claim needs an identified user
    assert (await client.post("/sessions/claim", json={"client_id": "browser-a"},
                              headers=CLIENT_A)).status_code == 403
    r = await client.post("/sessions/claim", json={"client_id": "browser-a"},
                          headers={"X-User-Id": "zhang.san"})
    assert r.json() == {"sessions": 1, "reports": 0}
    assert (await client.get("/sessions", headers=CLIENT_A)).json() == []
    assert [s["title"] for s in (await client.get("/sessions", headers={
        "X-User-Id": "zhang.san"})).json()] == ["问题一"]
    assert (await client.get("/sessions", headers={"X-Client-Id": "bad id!"})).status_code \
        == 400


async def test_internal_alias(make_client):
    client, _ = await make_client([AIMessage(content="好。")])
    client.base_url = "http://t/internal/v1/assistant"
    r = await client.post("/chat", json={"message": "hi", "stream": False,
                                         "workflow": "general"}, headers=CLIENT_A)
    assert r.status_code == 200 and r.json()["workflow"] == "general"


async def test_heartbeat():
    async def slow():
        yield "a"
        await asyncio.sleep(0.25)
        yield "b"

    out = [x async for x in _with_heartbeat(slow(), 0.1)]
    assert out[0] == "a" and out[-1] == "b" and ": ping\n\n" in out
