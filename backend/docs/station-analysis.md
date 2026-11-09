# Fixed-station daily analysis — design

Scope of this part of the predictive-maintenance system: collect station / device / dose-rate /
communication / battery / shelter data from the data source, analyse the previous calendar
day once a night, create one record per problem device (and per station battery), let the
language model write the explanation text, store everything in openGauss, and serve it to the
frontend through GET APIs during the day.

Out of scope: other pages, work orders, manual confirmation, C-14 / H-3 / aerosol + iodine /
fallout-dust sampling channels.

Version 1 runs on the **simulator**. The two blockers for real use are the **real data source**
(MQTT or MES interface) and the **per-station threshold table**; until both exist, everything
below is a simulated demonstration.

---

## 1. Data contract

### 1.1 Three levels

| Level | Fields | Stored in |
| --- | --- | --- |
| Station (configuration) | `station_id`, `name`, `comm_address`, `sampling_interval_s` (30 / 60 / 300), `calibration_factor`, `dose_l1`, `dose_l2` (µGy/h), `battery_warn_v`, `battery_alarm_v`, optional `battery_drop_v` + `battery_drop_minutes`, `battery_charge_minutes`, `has_rain_gauge`, `enabled`, device directory | openGauss `station_config` (seeded from `data/stations.json`) |
| Device (configuration) | `device_id`, `name`, `enabled` (≤ 20 per station) | `station_config.devices` (JSON) |
| Device (samples) | `dose_rate` µGy/h, `status` (normal / h1 / h2 / fault / offline), `comm` (ok / interrupted) | IoTDB `root.mes.<station>.<device>.<metric>` |
| Station (samples) | `battery_voltage` V, `battery_level` % (if reported), `ac_power` (on/off), `charging`, `shelter_temp` °C, `shelter_humidity` %RH, `door` (open/closed), `rainfall` mm (if a rain gauge exists) | IoTDB `root.mes.<station>._station.<metric>` |

Statuses and flags are stored as numeric codes so every metric is one DOUBLE series
(`status`: 0 normal, 1 H1, 2 H2, 3 fault, 4 offline; flags 1 / 0). See
`app/stations/model.py`.

### 1.2 What goes into the time-series database, and what does not

**IoTDB:** every raw sample above, exactly as received (dose rate normalised to µGy/h, *not*
multiplied by the calibration factor). Reported statuses are stored on every sample, not only
on change, so gaps and status runs are reconstructed the same way.

**openGauss, not IoTDB:** station configuration, thresholds, calibration factors, the device
directory, and analysis results. The thresholds and factors change rarely, are needed as
snapshots, and must be auditable.

**Not stored at all:** mean, standard deviation, completeness and other statistics are always
computed from the raw samples at analysis time. They exist only inside analysis events, where
they are frozen with the batch.

**Excluded:** sampling channels (C-14, H-3, aerosol / iodine, fallout dust), unconfigured
channels (`enabled: false`) and data of unknown devices (rejected at ingestion).

### 1.3 Units and calibration

- Dose rate is µGy/h everywhere. Ingestion accepts nGy/h, µGy/h and mGy/h. It **rejects
  Sv-based units**, because converting Sv to Gy needs a coefficient that the plant must
  confirm.
- Analysis uses `reported value × calibration_factor` from the configuration snapshot. The
  default of 1.0 means the source already sends calibrated values (**to be confirmed on site**;
  see §9).

---

## 2. Rules (all determinations are made by code)

Analysis window: `[00:00, 24:00)` of the previous calendar day in `ANALYSIS_TIMEZONE`
(default Asia/Shanghai).

| Event kind | Rule | Source | Level |
| --- | --- | --- | --- |
| `dose_h1` / `dose_h2` | 3 consecutive valid samples `> dose_l1` / `> dose_l2` (after calibration) | rule | watch / inspect |
| `reported_h1` / `reported_h2` | device reports H1 / H2 and no rule interval covers that time | source_reported | watch / inspect |
| `device_fault` | device reports fault | source_reported | inspect (`ANALYSIS_FAULT_LEVEL`, to be confirmed) |
| `offline` / `comm_interruption` | device reports offline / comm interrupted (any length) | source_reported | insufficient_data |
| `sampling_gap` | no usable sample for > 3 sampling intervals | data_quality | insufficient_data |
| `no_data` | no usable sample in the whole window | data_quality | insufficient_data |
| `stuck_detector` | identical readings (std = 0) for ≥ 60 min | data_quality | insufficient_data |
| `low_completeness` | daily completeness < 90 % without any gap event (scattered losses) | data_quality | insufficient_data |
| `battery_low_warn` / `battery_low_alarm` | 3 consecutive samples `< battery_warn_v` / `< battery_alarm_v` | rule | watch / inspect |
| `battery_drop_on_ac` | voltage falls by ≥ `battery_drop_v` within `battery_drop_minutes` while mains is on and the battery is not charging (10 min settling after a charge is ignored) | rule | watch |
| `slow_charging` | after mains is restored, charging lasts longer than `ANALYSIS_CHARGE_FACTOR` (1.5) × `battery_charge_minutes` | rule | watch |

**Interval semantics** (`app/analysis/rules.py::threshold_spans`). This matches the
workstation page:

- A level starts after N = 3 consecutive samples beyond its threshold. The interval begins at
  the first of those samples.
- One continuous alarm is one interval. Dropping back to a lower level that is still alarming
  (H2 → H1) stays in the same interval.
- **Escalation** (3 consecutive samples beyond the higher threshold) closes the interval and
  opens a new one.
- **Recovery** requires 3 consecutive samples back inside the limits. The interval ends at the
  last abnormal sample. Another alarm after recovery opens a new interval.
- A gap longer than 3 sampling intervals ends the interval. Continuity cannot be claimed
  across missing data, and the gap is reported separately.
- `open_at_start` / `open_at_end` mark intervals that were already active at 00:00 or were
  still active at 24:00, so their true duration is longer than shown.

**Valid samples.** A dose sample takes part in the threshold rules only when its status is
neither fault nor offline and communication is not interrupted. Samples taken while the device
reports a fault are kept in the fault event, not judged against thresholds.

**Rule results vs device reports.** When the device's reported H1 / H2 overlaps a rule
interval, the rule event carries `corroborated_by_source = true` (same level) or `false`. A
reported alarm that our rule does not see becomes its own `reported_*` event. A rule interval
that the device did not report is kept with `corroborated_by_source = false`, and that shows
up in the uncertainties.

**Configuration gaps** (recorded once per batch, never as device records):
- no `dose_l1` / `dose_l2` → no device records for that station
- no battery voltages → no battery record
- missing drop or charge parameters → that rule is skipped
- no rain gauge → informational

**No data is never assumed normal.** A device with no samples gets an explicit `no_data`
record. An IoTDB error is a processing error (batch `partial` / `failed`), never "no data".

**Device level** = the most severe event: inspect > watch > insufficient_data. Normal devices
get no record.

---

## 3. Records and events

One **record** per abnormal device (`equipment_type = detector`) plus one per station battery
(`backup_power`, `device_id = "BATTERY"`). A record holds:

- level (+ Chinese label), title, determination sources
- data status (`complete` / `partial` / `missing`)
- day statistics (expected / received / valid samples, completeness, min / max / mean / std,
  status counts, AC outages, charge sessions)
- explanation

Each **event** (`E1`, `E2`, … within the record) holds:

- kind, level, source, start / end / duration, open flags
- sample count, expected samples, completeness
- min / max / mean / std (calibrated)
- the rule with its thresholds and parameters
- the four comparisons
- context: AC power during the event; rainfall total (or `unavailable` with a reason); shelter
  temperature, humidity and door
- the **samples inside the interval** (`values`, up to `ANALYSIS_MAX_EVENT_VALUES`), so the
  frontend can draw them

Times are ISO-8601 UTC. `batch.timezone` is the display time zone.

**Frontend mapping** (`frontend/src/features/maintenance/types.ts`):

| Backend | Frontend |
| --- | --- |
| `level` watch / inspect / insufficient_data | `RiskLevel` (same values) |
| `equipment_type` detector / backup_power | `EquipmentType` (same values) |
| event `source` source_reported / rule / data_quality | `EventSource` device_alarm / monitoring_anomaly + run_trend / *(no value yet — the frontend needs one for data quality)* |
| `explanation.summary`, `evidence[] {event_ids, observation, metric, time_range, source}`, `recommendations[] {priority, action, rationale, event_ids, requires_human_confirmation}`, `uncertainties[]`, `confidence`, `model`, `generated_at` | `AiAnalysis` summary / evidence / recommendations / dataGaps / confidence / modelName / generatedAt |
| event `values` (interval samples) or `GET /stations/{id}/telemetry` | `detectorTrend.doseRate`, `backupTrend.battery` (stop synthesising curves in JS) |

### Comparisons

`previous_day`, `previous_7_days` (same interval on each of the 7 days before; needs ≥ 4 days
with data), `previous_month_same_day` and `previous_year_same_day`.

Each comparison is either `available` (mean / min / max / samples, `delta_mean`, `delta_pct`)
or `unavailable` with a reason. Typical reasons: no history, reference completeness < 50 %, or
the reference date doesn't exist (e.g. 31 Feb). A missing value is **never** filled with 0.
Data-quality and charging events are `not_applicable`.

Year-on-year needs one year of IoTDB history: set the retention (TTL) accordingly.

---

## 4. Language model

- **Input:** the evidence summary of one record. It contains event codes, local times,
  durations (minutes and hours), statistics, completeness in %, rules and thresholds,
  comparisons and context. **No raw curves.** At most a few kilobytes.
- **Output (JSON):**

  ```json
  {"summary": "...", "details": "...",
   "evidence": [{"event_ids": ["E1"], "observation": "..."}],
   "recommendations": [{"priority": "high|medium|low", "action": "...", "rationale": "...", "event_ids": ["E1"]}],
   "uncertainties": ["..."]}
  ```

- **Validation** (`app/analysis/explain.py::validate`):
  - the output parses and matches the schema
  - every cited event code exists, and every event is cited in the evidence
  - every number, time and date in the text appears in the evidence (rounding is accepted only
    when ≥ 2 significant digits remain; a negative evidence value supports its magnitude)
- **Retry once** with the validation error. If the output is still invalid, or the model is
  unreachable, a **rule-based template** is used (`method = template_fallback`, the reason is
  kept in `error`). The record is always stored.
- **Missing-data records** (only offline / interruption / gap / no-data / low-completeness
  events) use the template directly, without a model call.
- **Confidence is computed by code**, never by the model: a weighted mean of threshold
  configuration (0.2), daily completeness (0.3), share of available comparisons (0.2) and event
  sources (0.3). Event-source scores: rule + device agree 1.0, source-reported 0.8, rule only
  0.7, data quality 0.6. Factors without data are left out and the weights renormalised. The
  factors are returned with the explanation.
- At 20 devices there are a single-digit number of model calls per night. A 30B model is
  enough and sub-agents are unnecessary. A later "investigate this anomaly" feature can give
  the model 2–3 rounds of read-only tools for one record.

---

## 5. Storage (openGauss, migration `002_station_analysis.sql`)

| Table | Content |
| --- | --- |
| `station_config` | one row per station, device directory as JSON, `config_version` bumped on every change |
| `analysis_batch` | date, version, revision, status, trigger, attempts, window, **configuration snapshot** (stations, thresholds, calibration, devices, rule parameters), counts, configuration gaps, errors |
| `analysis_record` | one per abnormal device / battery; level, title, sources, data status, day statistics, explanation |
| `analysis_event` | one per abnormal interval; statistics, rule, comparisons, context, interval samples |

A record and its events are written in one transaction. Later threshold changes never
reinterpret old records, because each batch carries its own snapshot.

---

## 6. Batch processing

- The **schedule** runs every day at `ANALYSIS_RUN_AT` (00:30) for the previous calendar day,
  inside the API process. Run **one API instance**; with more, move the scheduler to a single
  worker or add a database lock.
- **Idempotency key:** `(analysis_date, ANALYSIS_VERSION)`. A finished or running batch is
  returned instead of starting another. Bump `ANALYSIS_VERSION` when the rules change.
- **Manual re-run:** `POST /analysis/batches {"date": ..., "force": true}` creates a new
  **revision** with a fresh snapshot. The current result of a day is its highest successful
  revision.
- **Failure handling:**
  - A failed batch is retried in place every `ANALYSIS_RETRY_MINUTES`, up to
    `ANALYSIS_MAX_ATTEMPTS`, reusing its original snapshot.
  - Batches interrupted by a restart are marked failed and retried.
  - Missed days (`ANALYSIS_CATCHUP_DAYS`) run after a restart.
- **Partial:** if some stations or devices fail (e.g. an IoTDB query error), the batch becomes
  `partial` and the other records stay visible. If nothing could be analysed, the batch is
  `failed`.

---

## 7. API (`/api/v1`)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/analysis/status` | schedule and next run, yesterday's batch, latest available batch, running batches |
| GET | `/analysis/batches?date_from=&date_to=&status=&limit=&offset=` | batch list |
| GET | `/analysis/batches/{id}?include_snapshot=` | one batch; poll this after POST |
| POST | `/analysis/batches` `{"date": null, "force": false}` | run / re-run a finished day: 202 + `poll` URL, or 200 with the existing batch |
| GET | `/analysis/records?date=&batch_id=&station_id=&device_id=&equipment_type=&level=&limit=&offset=` | records, most severe first, no samples; default: latest day ("yesterday's analysis") |
| GET | `/analysis/records/{id}?include_values=true` | record with all events and interval samples |
| GET | `/stations`, `/stations/{id}`, `/stations/{id}/telemetry` | configuration, latest values, raw / aggregated series |

GET requests only read the database. They never run rules or the model. There is no SSE.

---

## 8. Demo scenario (`data/stations.json`, all synthetic)

Expected records for "yesterday":

| Station / device | Scripted | Record |
| --- | --- | --- |
| ST01 D02 | 0.32 µGy/h 02:00–03:00 (device reports H1), 0.30 µGy/h 08:00–09:00 (device silent) | watch: 2 × `dose_h1`, the second not corroborated |
| ST01 D03 | 0.35 µGy/h 10:00–10:30, then 0.65 µGy/h 10:30–11:10 | inspect: `dose_h1` + `dose_h2` (escalation = new interval) |
| ST01 D04 | frozen reading 13:00–16:00 | insufficient_data: `stuck_detector` |
| ST01 D05 | offline 04:00–05:30, nothing received 20:00–20:20 | insufficient_data: `offline`, `sampling_gap` |
| ST01 D06 | fault 15:00–15:45 | inspect: `device_fault` |
| ST01 D07 | disabled channel | none |
| ST01 battery | mains lost 16:00–18:30, recharge 2.5 × slower | watch: `battery_low_warn`, `slow_charging` |
| ST02 D03 | silent all day | insufficient_data: `no_data` (data status `missing`) |
| ST02 D04 | comm interrupted 06:00–06:30 | insufficient_data: `comm_interruption` |
| ST02 battery | charger fault 09:00–10:30 (voltage falls with mains on) | watch: `battery_drop_on_ac` |
| ST03 | no thresholds | none; two configuration gaps in the batch |

Rain falls at ST01 between 01:30 and 04:00 (context for D02's first event). ST02 and ST03 have
no rain gauge. With `simulate.py --hours 200` the day-on-day and 7-day comparisons become
available.

---

## 9. Decisions to confirm, and refinements made

Where the specification left room, version 1 decides as follows. Each item is a setting or a
one-line change.

1. **Device fault → inspect** (`ANALYSIS_FAULT_LEVEL`), as you leaned towards.
2. **"Exceeding" means `>`**, not `≥`. Recovery also needs 3 consecutive samples, which avoids
   one alarm flapping into many events. De-escalation stays in the same interval.
3. **Calibration** is applied by the analysis (`reported × factor`). If the field source
   already sends calibrated values, keep the factor at 1.0. Never apply it twice.
4. **Sv units are rejected** until the plant gives the Sv → Gy coefficient.
5. **Reported offline / communication interruption** is an event at any length. An
   unexplained gap must exceed 3 sampling intervals.
6. **Stuck detector** = identical values for ≥ 60 min, classed as data quality
   (insufficient_data). A frozen detector is arguably a fault; raise it to inspect if
   maintenance agrees.
7. **Low completeness** (< 90 % without any gap event) is reported once per device, to catch
   scattered losses.
8. **Stations without thresholds** get no device records at all, including no data-quality
   records, as specified. An offline device at such a station is therefore invisible until
   its thresholds are configured. We could still report missing data for those stations;
   your call.
9. **Late data:** the run at 00:30 may miss messages queued by the broker (persistent MQTT
   session) that arrive later. Use `POST /analysis/batches {"force": true}` for that day, or
   move `ANALYSIS_RUN_AT` later.
10. **Battery level** is used only for day statistics. Shelter temperature, humidity and door
    are context only: no thresholds exist yet. Rainfall uses the total within the event
    interval; a washout lag (rain in the hour before) could be added.
11. **IoTDB retention** must cover one year for year-on-year comparisons.
12. **Frontend:** `EventSource` needs a value for data-quality events, and the 24-hour curves
    should come from `/stations/{id}/telemetry` instead of being synthesised.

---

## 10. Replacing the simulator

Only the ingestion adapter changes. Storage, rules, batch processing and the API stay the
same.

- **MQTT with a describable JSON format:** write a mapping file (copy
  `data/mqtt/mapping.example.json`). It covers the topic regex, field paths, units, a vendor
  status map (stored, never dropped) and vendor id → station / device; `_station` is used for
  station-level metrics. Test offline with `scripts/ingest_mqtt.py --file ... --dry-run`.
- **Any other format, or an MES pull interface:** a parser class with
  `parse(RawMessage) -> list[Measurement]` (`INGEST_PARSER=module:Class`), or a new source
  that delivers messages to the same pipeline.
- **Real station table:** put it in `station_config`, via `data/stations.json` +
  `python scripts/init_db.py --update-stations`, or by writing the table directly. Leave
  thresholds empty until they are provided.
