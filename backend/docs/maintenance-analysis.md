# Predictive maintenance analysis

Implements `docs/项目规划/设计文档.md` §3.1 and §4.2, with the additions from `Question.txt`.

## Who does what

```text
data acquisition -> main server DB -> MQTT maintenance/v1/{site}/{equipment}/telemetry -> this
service's IoTDB (+ consumption watermark)

main server timer -> POST /internal/v1/maintenance/analyses (window, data_cutoff, watermark)
                  <- 200 structured result (health_level, explanation, findings, actions,
                     limitations, data_snapshot, versions)
main server: validates, applies the 24 h suppression window, creates or updates the internal
work order, publishes the result to the frontend, stores the human review
main server -> PUT /internal/v1/maintenance/work-orders/{event_id}   (copy kept here)
```

**What this service stores:**
- IoTDB holds the time series and the consumption watermark.
- openGauss holds only the conversations and the work-order copy (`Question.txt`).
- Analysis results are not stored. The idempotency cache is in memory, for
  `ANALYSIS_IDEMPOTENCY_TTL_SECONDS`.

## Interface

`POST /internal/v1/maintenance/analyses`

- **Headers:** `Authorization: Bearer <COMPUTE_SERVICE_TOKEN>` (or `X-API-Key`),
  `Idempotency-Key`, `X-Request-Id`.
- **Request:** `analysis_id`, `site_id`, `equipment_ids` (optional), `analysis_type`
  (`detector` | `backup_power` | `general`), `observation_window {start, end}`,
  `history_lookback_days`, `data_cutoff`, `mqtt_watermark`.
- **Processing order:**
  1. **Idempotency.** The same key returns the same result, and concurrent duplicates share one
     execution. The same key with a different body gives 409 `idempotency_conflict`.
  2. **Watermark.** The service waits up to `ANALYSIS_WATERMARK_WAIT_SECONDS` for the local
     watermark to reach `mqtt_watermark`. If it doesn't, the answer is 503
     `watermark_not_reached` (`retryable: true`, with both watermarks). Without
     `mqtt_watermark` the analysis runs on what is stored (development).
  3. **Window.** The analysis covers `[start, min(end, data_cutoff)]`, at most
     `ANALYSIS_MAX_WINDOW_DAYS`.
- **Response:** exactly the design document's §4.2 format, in snake_case; the main server maps
  it to the frontend's camelCase `AiAnalysis`. Two additions:
  - `health_level: "normal"` when no rule fires. The main server creates no work order for it,
    as the design states.
  - `limitations` can also appear on watch, inspect and normal results.
- **Errors:** sanitised JSON `{code, message, retryable, analysis_id, request_id}`.
  - 404 `site_not_found` / `equipment_not_found`
  - 409 `idempotency_conflict`
  - 422 window problems
  - 503 `watermark_not_reached` / `timeseries_unavailable`

**Contract invariants,** enforced by `app/maintenance/schemas.py`:
- **watch / inspect:** at least 1 finding and at least 1 action, and the first action is the
  primary.
- **insufficient_data:** no actions, at least 1 limitation, template text.
- **Limits:** at most 3 findings, 3 actions and 1 limitation.

**Work-order copy (reserved interface):**
- `PUT /internal/v1/maintenance/work-orders/{event_id}`: the main server pushes each work order
  after creating or updating it and after the human review. A push older than the stored copy
  (`updated_at`) is ignored and reported as `"stale"`. Reviewer identity is not accepted.
- `GET /internal/v1/maintenance/work-orders[/{event_id}]` reads the copy.

## Rules (`app/maintenance/detect.py`, `rules.py`)

Every finding carries `rule_id`, `code`, `severity`, `metric`, `observed`, `baseline`,
`change_percent`, `window`, `message` and `source_refs`.
- **Baseline:** the previous `ANALYSIS_BASELINE_DAYS` (前两周). If there's no history, the
  station's background value (本底值) or nominal battery runtime.
- **Ranking:** inspect before watch, then rule priority; the top 3 become findings. Every hit is
  logged with the `analysis_id`.

**Detector rules:**

| Rule | Severity | Fires on |
| --- | --- | --- |
| `DETECTOR-CONSECUTIVE-ALARM` `detector.dose_rate_alarm` | inspect | N consecutive samples above `dose_l2`, after calibration |
| `DETECTOR-CONSECUTIVE-WARNING` `detector.dose_rate_warning` | watch | N consecutive samples above `dose_l1` |
| `DETECTOR-ALARM-POINT` `detector.alarm_point` | watch | samples above `dose_l2`, but fewer than N in a row |
| `DETECTOR-DEVICE-ALARM` `detector.device_alarm` | H2 inspect, H1 watch | the device reports H1 / H2 and no threshold rule explains it |
| `DETECTOR-DEVICE-FAULT` `detector.device_fault` | `ANALYSIS_FAULT_LEVEL` (inspect) | the device reports a fault |
| `DETECTOR-STUCK-READING` `detector.stuck_reading` | watch | identical readings for at least `ANALYSIS_STUCK_MINUTES` |
| `DETECTOR-RAIN-CORRELATION` / `DETECTOR-DOSE-RISE` | watch | an hourly mean at least `ANALYSIS_RISE_WATCH_PCT` above the baseline, with or without rain in the same or previous hour (only when no threshold rule fired) |

**Backup-power rules:**

| Rule | Severity | Fires on |
| --- | --- | --- |
| `BACKUP-VOLTAGE-ALARM` / `BACKUP-VOLTAGE-LOW` | inspect / watch | N consecutive samples below `battery_alarm_v` / `battery_warn_v` |
| `BACKUP-RUNTIME-DROP` `backup.runtime_drop` | inspect at ≥ 25 %, watch at ≥ 15 % | the reported reference runtime is below its baseline |
| `BACKUP-ON-BATTERY` `backup.mains_lost` | watch | mains is still lost at the end of the window |
| `BACKUP-VOLTAGE-DROP-ON-AC` | watch | the voltage falls by `battery_drop_v` within `battery_drop_minutes` while mains is on and the battery isn't charging |
| `BACKUP-SLOW-RECHARGE` | watch | recharging after mains returns takes longer than `ANALYSIS_CHARGE_FACTOR` × `battery_charge_minutes` |

**History rule:** `HISTORY-REPEAT` `history.repeated_event` (watch) fires when at least 2 earlier
watch / inspect work orders exist for the same equipment within `history_lookback_days`. It's
added only when there are current findings. The source is the main server's history API if
`MES_API_BASE_URL` is set, otherwise the work-order copy.

**Data quality, which becomes the single merged limitation:**
- **Blocking (gives `insufficient_data`):**
  - no usable data;
  - completeness below `ANALYSIS_MIN_COMPLETENESS`;
  - missing dose or battery thresholds;
  - no battery voltage data.
- **Non-blocking:**
  - offline or communication interruption (in minutes);
  - sampling gaps;
  - an expired calibration;
  - no reported runtime or charge state;
  - history unavailable.

**Health level:** blocking gap → `insufficient_data`; otherwise any inspect finding → `inspect`;
any finding → `watch`; no finding → `normal`.

## Actions and explanation

- **Actions:** `data/maintenance/actions.json` (versioned) maps each finding code to ordered
  preset actions, and up to 3 distinct candidates are taken in finding order.
  - Per `Question.txt` ("建议动作是模型在预置好的三种里面输出一种主建议"), the model picks the
    primary action among those candidates.
  - The texts always come from the catalogue.
  - If the model's choice is invalid or the model is unavailable, the catalogue order decides.
- **Explanation (`app/maintenance/narrator.py`):**
  - **Input:** the model gets only the compressed rule facts (findings, limitation, candidate
    actions), with no raw series.
  - **Output:** `{"explanation", "primary_action"}`.
  - **Validation:** valid JSON, at most `MAINTENANCE_NARRATOR_MAX_CHARS` characters, and every
    number, time and date must appear in the facts.
  - **Fallback:** one retry with the error, then the rule template (`explanation_source:
    template`).
  - `insufficient_data` and `normal` never call the model.
  - `MAINTENANCE_NARRATOR_MODEL_NAME` can point to a different model than the assistant's.

## Station directory and MQTT contract

- **Station directory:** `data/stations.json` (`STATIONS_FILE`) holds a snapshot of the main
  server's configuration. It's re-read when the file changes, and its hash appears as
  `stations.version` in `/health`. It isn't in openGauss, because openGauss holds only
  conversations and work orders.
  - **Fields** (from the prototype's system-management page): number, name, type,
    communication type and ID, GPS, sampling period, calibration factor, background value,
    dose L1 / L2, battery thresholds and runtime, rain gauge, detectors (serial, calibration
    valid-until).
  - **Equipment IDs:** `<site>-DET-xx` for detectors, `<site>-BAT-01` for backup power,
    `<site>-ENV-01` for the shelter / environment unit.
- **Telemetry, topic `maintenance/v1/{site_id}/{equipment_id}/telemetry`, QoS 1:**
  - **Payload:** `schema_version`, `message_id`, `source_sequence`, `site_id`, `equipment_id`,
    `event_time`, and `points[{metric, value, unit, quality}]`.
  - **Metrics:**

    | Unit | Metrics |
    | --- | --- |
    | Detector | `dose_rate`, `dose_rate_avg`, `dose_rate_std`, `status` (normal / h1 / h2 / fault / offline), `comm` |
    | Backup power | `battery_voltage`, `battery_level`, `ac_power`, `charging`, `runtime_hours` |
    | Environment | `shelter_temp`, `shelter_humidity`, `door`, `rainfall` |

  - **Units:** dose rates in nGy/h, µGy/h or mGy/h, stored as µGy/h. Sievert units are
    rejected (the plant must confirm the conversion).
  - **Quality:** `bad` points are dropped and counted.
- **Watermark:** after each successful write, the ingester stores the highest
  `source_sequence` it has consumed (rejected messages count as consumed) in
  `root.mes._sys.ingest.watermark`.

## Demo scenario (`data/stations.json`, synthetic, relative to the simulator start)

| Station | Scripted | Analysis of the last 24 h |
| --- | --- | --- |
| AS01 三废区 | 0.30 / 0.29 µGy/h for 1 h, twice; rain | watch: 2 × consecutive warning |
| AS03 升压站南部 | 0.33 then 0.46 µGy/h | inspect: consecutive alarm + warning |
| AS06 监测站 06 | on battery for 5 h, runtime 24 → 18 h, rain-correlated rise | inspect: runtime drop, on battery, low voltage (`general`) |
| AS12 高温堆西南角 | rain 3 mm/h | watch: rain-correlated rise |
| AS08 监测站 08 | device fault | inspect |
| AS04 监测站 04 | frozen reading for 3 h | watch: stuck reading |
| BS11 东钱家村 | mains lost for 3 h, recharge 2.5 × slower | watch: slow recharge |
| AS05 监测站 05 | charger fault (voltage falls with mains on), comm loss | watch (`backup_power` / `general`) |
| BS13 东墩村 | no data | insufficient_data |
| AS13 高温堆厂前区 | no thresholds configured | insufficient_data |
| BS02, BS15 | offline for 3 h / 2 h | normal + data limitation |
| AS11 | calibration expired | normal + limitation |

## To confirm

1. **Work-order copy in openGauss.** `Question.txt` asks for it; the design document says the
   compute server keeps none. The copy is implemented behind a reserved interface (`PUT`), and
   the main server decides whether to push.
2. **The primary action is chosen by the model** among at most 3 preset candidates
   (`Question.txt`). The design document lets the catalogue order decide; the catalogue order is
   the fallback.
3. **Thresholds are synthetic.** The real per-station thresholds, background values, runtimes
   and charge times must come from the main server.
4. **The main server's MQTT publisher** must provide the monotonically increasing
   `source_sequence` that the watermark relies on.
5. **`health_level: normal`** is returned when no rule fires. Should the main server receive it,
   or an empty 204?
