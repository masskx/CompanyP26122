"""Configurable parser (COLLECTOR_PARSER=mapping) against the example vendor format, the
JSON Lines replay source, and the dry-run writer."""

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from industrial_ai.collector.mapping_parser import MappingConfig, MappingParser
from industrial_ai.collector.models import RawMessage, RejectedMessage
from industrial_ai.collector.parser import build_parser
from industrial_ai.collector.pipeline import IngestionPipeline
from industrial_ai.collector.sources.file import JsonLinesFileSource
from industrial_ai.collector.validator import MeasurementValidator
from industrial_ai.collector.writer import StdoutWriter
from industrial_ai.core.config import Settings

EXAMPLES = Path(__file__).parent.parent / "examples" / "mqtt"
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=UTC)


def msg(topic: str, payload) -> RawMessage:
    return RawMessage(topic, json.dumps(payload).encode(), NOW)


def example_parser() -> MappingParser:
    return MappingParser.from_file(EXAMPLES / "mapping.example.json")


def test_example_format_maps_devices_units_and_time():
    [a, b] = example_parser().parse(msg("plant/AS01/radiation/gw-1", {"readings": [
        {"deviceId": "GM-0001", "time": "2026-11-05T10:00:00+08:00", "doseRate": 512,
         "unit": "nSv/h", "state": "NORMAL"},
        {"deviceId": "GM-0002", "time": "2026-11-05T10:00:00+08:00", "doseRate": "1.21",
         "unit": "uSv/h", "state": "OK"}]}))
    assert (a.site_id, a.equipment_id, a.metric) == ("AS01", "AS01-ISO-RM-01", "dose_rate")
    assert a.value == pytest.approx(0.512) and b.value == pytest.approx(1.21)
    assert a.timestamp == datetime(2026, 11, 5, 2, 0, tzinfo=UTC)


def test_one_bad_record_does_not_drop_the_good_ones():
    parser = example_parser()
    out = parser.parse(msg("plant/AS01/radiation/gw-1", {"readings": [
        {"deviceId": "GM-0001", "time": "2026-11-05T10:00:00+08:00", "doseRate": 1,
         "state": "FAULT"},
        {"deviceId": "GM-0002", "time": "2026-11-05T10:00:00+08:00", "doseRate": 100,
         "state": "NORMAL"}]}))
    assert [m.equipment_id for m in out] == ["AS01-ISO-RM-02"]
    assert parser.drain_rejections() == {"device_status": 1}


@pytest.mark.parametrize("topic, payload, reason", [
    ("plant/AS01/status/gw-1", {"readings": []}, "bad_topic"),
    ("plant/AS01/radiation/gw-1", {"other": 1}, "bad_payload"),
    ("plant/AS01/radiation/gw-1", {"readings": [{"deviceId": "GM-9999", "time":
     "2026-11-05T10:00:00+08:00", "doseRate": 1, "state": "OK"}]}, "unmapped_device"),
    ("plant/AS01/radiation/gw-1", {"readings": [{"deviceId": "GM-0001", "time": 1700000000,
     "doseRate": 1, "state": "OK"}]}, "bad_timestamp"),
    ("plant/AS01/radiation/gw-1", {"readings": [{"deviceId": "GM-0001", "time":
     "2026-11-05T10:00:00+08:00", "doseRate": "n/a", "state": "OK"}]}, "bad_value"),
    ("plant/AS01/radiation/gw-1", {"readings": [{"deviceId": "GM-0001", "time":
     "2026-11-05T10:00:00+08:00", "doseRate": 1, "unit": "rem/h", "state": "OK"}]},
     "unknown_unit"),
])
def test_rejections(topic, payload, reason):
    with pytest.raises(RejectedMessage) as exc:
        example_parser().parse(msg(topic, payload))
    assert exc.value.reason == reason


def test_passthrough_single_object_format():
    parser = MappingParser(MappingConfig.model_validate({
        "topic_pattern": r"^rad/(?P<site_id>\w+)/(?P<device>[\w-]+)/(?P<metric>\w+)$",
        "fields": {"timestamp": "t", "value": "v"}, "timestamp_format": "epoch_ms",
        "default_unit": "uSv/h", "unmapped_devices": "passthrough"}))
    [m] = parser.parse(msg("rad/AS01/AS01-ENV-01/dose_rate", {"t": 1762311600000, "v": 0.09}))
    assert (m.site_id, m.equipment_id, m.metric, m.value) == ("AS01", "AS01-ENV-01",
                                                              "dose_rate", 0.09)


class SettingsAwareParser:
    """Shape of a custom parser loaded via COLLECTOR_PARSER=module:Class."""

    def __init__(self, settings: Settings) -> None:
        self.prefix = settings.collector_topic_prefix

    def parse(self, message: RawMessage) -> list:
        return []


def test_build_parser_options(tmp_path):
    base = Settings(_env_file=None)
    assert type(build_parser(base)).__name__ == "ProvisionalJsonCodec"
    mapped = build_parser(base.model_copy(update={
        "collector_parser": "mapping",
        "collector_parser_config": EXAMPLES / "mapping.example.json"}))
    assert isinstance(mapped, MappingParser)
    custom = build_parser(base.model_copy(update={
        "collector_parser": "tests.test_mapping_parser:SettingsAwareParser"}))
    assert type(custom).__name__ == "SettingsAwareParser" and custom.prefix == "sim/radiation"
    with pytest.raises(ValueError, match="COLLECTOR_PARSER_CONFIG"):
        build_parser(base.model_copy(update={"collector_parser": "mapping"}))
    with pytest.raises(ValueError, match="unknown parser"):
        build_parser(base.model_copy(update={"collector_parser": "nope"}))


@pytest.mark.anyio
async def test_replay_captured_file_dry_run():
    out = io.StringIO()
    pipeline = IngestionPipeline(
        JsonLinesFileSource(EXAMPLES / "captured.example.jsonl"), example_parser(),
        MeasurementValidator(clock=lambda: datetime(2026, 11, 5, 3, 0, tzinfo=UTC)),
        StdoutWriter(out))
    await pipeline.run()
    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["equipment_id"] for r in rows] == ["AS01-ISO-RM-01", "AS01-ISO-RM-02",
                                                 "AS01-ENV-01"]
    assert [r["value"] for r in rows] == pytest.approx([0.512, 1.21, 0.086])
    assert pipeline.stats.rejected == {"device_status": 1, "unmapped_device": 1, "bad_topic": 1}
