# MQTT: ghmes/signal

This is the format the MQTT administrator sent. It is read by `app/ingestion/ghmes.py`
(`INGEST_PARSER=ghmes`, the default), and the profile `data/mqtt/ghmes.json`
(`INGEST_GHMES_FILE`) decides how. `data/mqtt/ghmes.example.jsonl` holds two messages: the
administrator's sample and a synthetic normal snapshot.

## Message

Topic `ghmes/signal`. The payload is a JSON array with one **signal** per monitored object:

| Key | Example | Use |
| --- | --- | --- |
| `signalCode` | `sig-001` | optional `station_map` key |
| `signalName` | `AS01-三废区` | the station id is its start (`AS01`) |
| `bizSystem` | `固定子站` / `环境监测车` / `气象塔` | station, or auxiliary source |
| `dataTime` | `2026-11-23 20:16:16` | sample time; no zone, so read in `Asia/Shanghai` |
| `channels[]` | `fieldName`, `value`, `unit`, `kind`, `status`, `commStatus` | one reading each |

## Mapping

**Fixed stations (`固定子站`)**, into the station's units in `data/stations.json`:

| fieldName | Stored as | Notes |
| --- | --- | --- |
| `gamma_instant` | `<site>-DET-01.dose_rate` | unit taken from the message (μGy/h, nGy/h, mGy/h); Sv refused |
| `gamma_avg`, `gamma_std` | `dose_rate_avg`, `dose_rate_std` | |
| `gamma_status` | detector `status` | codes 0–4 → normal / h1 / h2 / fault / offline (**to confirm**) |
| `total_status` | `<site>-ENV-01.station_status` | same codes (**to confirm**) |
| `power_status` | `<site>-BAT-01.power_status` (raw code) and `ac_power` | 0 = mains, 1 = battery (**to confirm**) |
| `carbon_status`, `tritium_status` | `<site>-ENV-01.carbon_sampler`, `tritium_sampler` | raw codes |

**`commStatus`:**
- `通讯故障` (or another `comm_fault` text): the values are placeholders, so they are not
  stored. The detector gets `comm = 0` and `status = offline`, and the station gets
  `station_status = offline`. Without this, the zeros would look like real readings and
  trigger the stuck-reading rule wrongly.
- `通讯正常`: `comm = 1`.
- Any other text keeps the values and is counted as `unknown_comm_status`.

**Monitoring vehicle (`环境监测车`) and weather tower (`气象塔`)** are auxiliary sources (`M01`,
`MET01` in `data/stations.json` → `auxiliary_sources`):
- Every channel is stored as reported, at `root.mes.M01.M01-VEH-01.<fieldName>`.
- A `comm` series is stored with them.
- They have no maintenance rules.
- They are read through `GET /api/v1/auxiliary/{id}` and `/auxiliary/{id}/telemetry?field=`.

**Problems are counted, not fatal.** One bad channel or signal never discards the rest of the
message. The counts appear in the ingester's statistics:
- `unmapped_field`
- `unknown_code`
- `unknown_unit`
- `missing_value`
- `unmapped_signal`

**Changing the mapping:** edit `data/mqtt/ghmes.json`, for example:
- a renamed field;
- a new code;
- `station_map` when a signal name doesn't start with the station id;
- `ignore_fields` for fields deliberately not stored.

Then check it with
`python scripts/ingest_mqtt.py --file <capture>.jsonl --dry-run`.

## Watermark

The feed has no `source_sequence`, so the consumption watermark is the newest `dataTime`
consumed, in epoch milliseconds. It never moves backwards: replays and late messages leave it
unchanged.

The main server's `mqtt_watermark` in `POST /internal/v1/maintenance/analyses` can be either:
- the same epoch-ms number; or
- an ISO time with a time zone, e.g. its `data_cutoff`.

The analysis then waits until the ingester has consumed data up to that time. This assumes
one message carries all stations with the same `dataTime`, as in the sample.

## To confirm with the MQTT administrator

1. **Status codes.** What do the codes of `total_status`, `gamma_status`, `power_status`,
   `carbon_status` and `tritium_status` mean? The sample only shows 0. The assumed meanings
   are in `ghmes.json` (`_codes`); raw power and sampler codes are also stored unchanged.
2. **Backup-power and shelter data are missing.**
   - **What's missing:** battery voltage, charging state and reference runtime; shelter
     temperature, humidity and door; per-station rainfall.
   - **Effect:** without them the backup-power rules can't run (`insufficient_data: no
     battery voltage data`), and rain correlation can't run for stations without a gauge.
   - **What the simulator does:** `ghmes.json` proposes fieldNames for them (`battery_voltage`,
     `charge_status`, `runtime_hours`, `shelter_temp`, `shelter_humidity`, `door_status`,
     `rainfall`), and the simulator publishes them so the demo works. Rename them to the real
     names once they exist.
3. **Units.**
   - The weather tower's channels have no units in the sample; they are stored as reported.
   - The vehicle reports dose rate in μSv/h, which is stored unconverted. It isn't compared
     with the stations' μGy/h thresholds.
4. **Timing.**
   - Is `dataTime` the sampling time or the publishing time?
   - Is the snapshot published periodically, and how often?
   - Does a message always contain every station?
5. **The channel `status` field.** It was empty in the sample, and it is ignored for now.
6. **Several detectors per station.** Each station currently maps to `<site>-DET-01`. If a
   station has several detectors, how are their channels told apart?
