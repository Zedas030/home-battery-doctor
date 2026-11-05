# Battery Doctor

**Track the health of your home batteries over months and years – vendor-independent, from the data Home Assistant
already records.** Battery Doctor measures the real usable capacity with a confidence range, counts full cycles,
shows the ageing trend and a forecast, analyses stacked master/slave units, watches the warranty and creates a
warranty dossier (PDF + raw data) on demand.

> Deutsche Anleitung: [README.de.md](README.de.md)

**Battery Doctor is read-only.** It never controls a battery, never changes an output power and never touches an
automation. In Home Assistant it only writes its own display sensors (`sensor.battery_doctor_*`) and sends
notifications. Every other service call is refused in code before it reaches the network (`akd/ha.py`, covered by a
test).

It was built for a balcony storage with two stacked Growatt NOAH 2000 units (master with inverter + expansion
battery, data via [noah-mqtt](https://github.com/mtrossbach/noah-mqtt)), but works with any storage that exposes a
state of charge and charge/discharge power in Home Assistant.

---

## Contents

1. [Features](#features)
2. [Quick start](#quick-start)
3. [Secrets: where the token goes and how to rotate it](#secrets-where-the-token-goes-and-how-to-rotate-it)
4. [Configuration](#configuration)
5. [What you get](#what-you-get)
6. [How it is calculated](#how-it-is-calculated)
7. [Master/slave analysis](#masterslave-analysis)
8. [Warranty](#warranty)
9. [Self-monitoring and alerts](#self-monitoring-and-alerts)
10. [Data, retention, backup](#data-retention-backup)
11. [Commands](#commands)
12. [Tests](#tests)
13. [Troubleshooting](#troubleshooting)
14. [License](#license)

## Features

- **Usable capacity in kWh with a 95 % range**, from clean charge/discharge windows. If the data is too thin or too
  coarse, it says **"no reliable statement"** instead of showing false precision.
- **Capacity per unit** for stacked batteries, although the power is only measured for the whole system.
- **Full-cycle equivalents**, measured **efficiency** and the **ageing trend** with a forecast such as
  "80 % expected in May 2028".
- **Master/slave analysis**: SOC difference, slave wake-up delay, charge rate per unit, automatic detection of a
  wiring change and a **before/after comparison with confidence**.
- **Warranty watchdog** based on the manufacturer's terms, including the manufacturer's reference curve.
- **Alerts** via a Home Assistant notify service, **HA sensors**, **Prometheus metrics**, a **Grafana dashboard**.
- **Monthly report** (HTML) and a **warranty dossier** (PDF + ZIP with all raw data as CSV and SHA-256 checksums).
- **Self-monitoring**: missing or renamed entities, changed units or integrations, data gaps, frozen SOC.
- English and German (`language: en | de`).

## Quick start

Requirements: Docker with Compose, Home Assistant with the storage entities, optionally Prometheus with the HA
exporter for fine-grained history.

```bash
git clone https://github.com/Zedas030/home-battery-doctor.git
cd home-battery-doctor
cp config/config.example.yaml config/config.yaml   # adapt, see below
cp .env.example .env && chmod 600 .env              # put in the token, see "Secrets"
docker build --target test .                         # run the tests (optional, ~1 min, ~5 min on a Raspberry Pi)
docker compose up -d --build
docker exec battery-doctor python -m akd selftest
```

Web UI: `http://<host>:8104`. On the first start Battery Doctor imports the existing history (Prometheus per
minute, HA recorder, HA long-term statistics per hour) and analyses it. That takes a few minutes on a Raspberry Pi.
No notifications are sent for this history, so there is no flood of pushes.

Autostart: `restart: unless-stopped`. Resources: 384 MB RAM, 0.5 CPU. Logs: at most 3 × 10 MB.

## Secrets: where the token goes and how to rotate it

There is exactly one secret: **`HA_TOKEN`**, a long-lived access token of Home Assistant.

- **Where:** in the file `.env` next to `docker-compose.yml`, permissions `600`. Never in `config.yaml`, never in git
  (`.env` is in `.gitignore`).
- **Create:** in Home Assistant, open your profile (bottom left) → tab **Security** → **Long-lived access tokens** →
  **Create token**, name it e.g. "Battery Doctor". Put the value into `.env`: `HA_TOKEN=...`
- **Rotate:** create a new token as above, replace it in `.env`, run `docker compose up -d` (re-reads `.env`), then
  delete the old token in HA under "Long-lived access tokens".
- **Check:** `docker exec battery-doctor python -m akd selftest` reports "token rejected (401)" if it does not fit.
- The other entries in `.env` are optional and not secret: `AKD_BACKUP_DIR` (backup target) and `AKD_UID`/`AKD_GID`
  (user of the container, e.g. `1000` if the backup target is a CIFS share mounted for uid 1000).

## Configuration

Everything is documented in `config/config.yaml` (template `config/config.example.yaml`, German template
`config/config.example.de.yaml`). After changes: `docker compose restart battery-doctor`. If you change estimation
parameters, re-analyse everything: `docker exec battery-doctor python -m akd analyze`.

| Section | Meaning |
|---|---|
| `language` | `en` or `de`: notifications, web UI, reports, dossier, names of the HA sensors |
| `system` | total SOC and **total** charge/discharge power (stacked units often report power only for all units together) |
| `system.battery_count` | optional: number of reported batteries; windows only count while all units are present |
| `units` | per unit: SOC, temperature, nominal capacity, commissioning date (= start of warranty), serial number |
| `warranty` | the manufacturer's terms (template: Growatt NOAH 2000) |
| `estimation` | rules for clean measurement windows and for "no reliable statement" |
| `master_slave` | optional: unit names, throttle flag of an existing automation, date of a wiring change or `auto` |
| `monitoring` | poll interval, when "no data" is reported, quiet hours |
| `home_assistant.notify` | notify service, e.g. `notify.mobile_app_phone` |

A single battery works too: one unit, no `master_slave` section.

## What you get

**Web UI** (`:8104`): overview per storage (capacity ± range, SOH, full cycles, trend, warranty), capacity history,
master/slave, all measurement windows with the reason for every rejected one, self-monitoring (every entity with the
age of its last value, unit, integration), reports, dossier button, CSV export.

**Sensors in Home Assistant** (set every 5 minutes; after an HA restart they are back within 5 minutes):

| Sensor | Content |
|---|---|
| `sensor.battery_doctor_status` | ok / warning / critical / error, open alerts as attribute |
| `sensor.battery_doctor_<scope>_capacity` | usable kWh, attributes lower/upper bound, windows, reason when `unknown` |
| `sensor.battery_doctor_<scope>_soh` | % of the nominal capacity |
| `sensor.battery_doctor_<scope>_full_cycles` | full-cycle equivalents since the start of the data |
| `sensor.battery_doctor_<scope>_forecast_80` | date "80 % expected", text as attribute |
| `sensor.battery_doctor_<scope>_warranty` | warranty status, notes, manufacturer reference value |
| `sensor.battery_doctor_ms_soc_difference` | SOC difference master−slave today (daily mean) |
| `sensor.battery_doctor_ms_wake_delay` | minutes the slave wakes up after the master in the morning |
| `sensor.battery_doctor_ms_replug_effect` | change of the difference after a wiring change, with range and summary |

`<scope>` is `system` or a unit key (e.g. `master`, `slave`). A value of `unknown` means **no reliable statement**; the
reason is in the attribute `reason`. With `language: de` the sensors keep the German names of version 1.0
(`sensor.akku_doktor_system_kapazitaet` …).

**Prometheus:** `http://<host>:8104/metrics` (prefix `akd_`). Scrape job:

```yaml
  - job_name: battery_doctor
    scrape_interval: 60s
    static_configs:
      - targets: ['localhost:8104']
```

**Grafana:** import `grafana/battery-doctor.json` (German titles: `battery-doctor.de.json`) or put it into the
provisioning folder (uid `battery-doctor`). Regenerate with `python grafana/make_dashboard.py [--lang de]`.

**Monthly report:** on the 1st at 09:00 as HTML (kept under `data/reports/`), plus a short version as a push.

**Warranty dossier:** web UI → "Reports & dossiers" → "Create dossier" (or `python -m akd dossier master`). Result:
a PDF with result, capacity history, monthly values, temperature proof, method, data sources and all used measurement
windows, plus a ZIP with the PDF, all raw data as CSV (minutes, 15 minutes, hours, windows, estimates, daily values)
and SHA-256 checksums.

## How it is calculated

Goal: measure the **usable** capacity from normal operation, without test cycles and without trusting the BMS.

1. **Minute grid.** All values are put on a minute grid. A value is held only briefly (power 4 min, SOC 11 min,
   because many cloud integrations send battery details only every 5 min). If the cloud delivers nothing new while
   HA and Prometheus keep showing the old value, this is detected: all power values stay exactly the same for 20
   minutes **and** the SOC does not move although the shown power should have changed it by at least 2 percentage
   points. (Constant power alone is not enough – at night with a fixed output setpoint the storage really reports
   identical values.) A sleeping unit reporting **0 %** counts as "no data", not as empty. Impossible temperatures
   (error value −273 °C, and 0 °C at the same time as 0 % SOC) are ignored.
2. **Measurement windows.** A window is a contiguous period with only discharging (or only charging). It starts and
   ends **exactly at a change of the whole-number total SOC**. The SOC is then known at both edges up to the step
   position, and the rounding error of whole percent largely cancels out.
3. **Clean or not.** A window is only used if all of these hold:
   - at least 30 percentage points swing and at least 30 minutes
   - no gap over 10 min (shorter gaps are bridged with the held value and go into the uncertainty)
   - no SOC jump over 3 percentage points (BMS calibration, a unit waking up)
   - edges only between 3 and 98 % SOC (the BMS calibrates at the top and bottom)
   - battery temperature between 10 and 35 °C
   - the SOC moves only in flow direction
   - the SOC is not reported more coarsely than in 5 % steps
   - all batteries are reported (optional `battery_count`)
   - plausible result (30–160 % of the nominal capacity)

   Every rejected window is visible with its reason (web UI "Measurement windows", `windows.csv`).
4. **Per window:** capacity = Σ(power × time) / ΔSOC × 100. The uncertainty of a window combines:
   - the step position at both edges: step/√12 per edge
   - the random error of the power reading: 2 %
   - the share of bridged gaps
5. **Per period** (current: last 60 days; also per calendar month):
   - Outliers are rejected robustly (more than 3.5 robust standard deviations from the median).
   - The result is the weighted mean (weight 1/σ²).
   - The 95 % range comes from a bootstrap over the windows, at least from the window uncertainty.
   - A possible **common calibration deviation of the power sensor (±3 %)** is added. It shifts all values equally,
     so it does not change the trend, but it counts for the absolute capacity and the warranty limit.
6. **Staying honest.** "No reliable statement" is given in these cases, each with a reason:
   - fewer than 5 clean windows
   - range wider than ±8 %
   - SOC only reported in coarse steps
7. **Usable vs. charged.** The main number comes from **discharge windows**: the energy that actually comes out. Charge
   windows give the energy that has to go in. Their ratio is the measured efficiency, a health value of its own.
8. **Per unit.** Many stacked systems only measure power for all units together. So for every window
   E ≈ k_master·ΔSOC_master + k_slave·ΔSOC_slave (k = Wh per percentage point). Over many windows this is solved
   robustly (Huber regression), the range comes from a bootstrap. This works because the units move by different
   amounts of percent in the windows. If they don't, the result is "split not identifiable".
   - Charge windows enter via the measured efficiency.
   - Only the part of a window in which all units report is used.
9. **Older history.** Before the Prometheus period only HA long-term statistics (mean/min/max per hour) exist. Coarse
   windows are built from them:
   - ΔSOC from the hourly means
   - energy from half of the edge hours plus the full hours in between
   - much larger uncertainty (1.5 percentage points per edge, 4 % energy)

   They are marked as source "hourly" and only used where no minute values exist.
10. **Full-cycle equivalents.**
    - System: discharged energy / nominal capacity.
    - Per unit: sum of the downward SOC steps / 100. Across sleep and data gaps the distance between valid values
      counts.
11. **Trend and forecast.** Robust line (Theil-Sen) over the reliable monthly values, range of the slope by bootstrap.
    This gives "80 % expected in …" and the same for the warranty limit.
    - Forecasts only after 6 months with estimates; before that it says "too early".
    - Without a measurable loss it says "not foreseeable".

This is checked on synthetic batteries with known capacity (see [Tests](#tests)). The simulation contains clouds,
efficiencies, whole percent, battery details every 5 min, a sleeping slave, gaps, calibration jumps and artificial
ageing. Result of `python -m akd synthetic`: every capacity (system and per unit) within ±2.3 % of the truth with the
truth inside the range; an ageing of 8 %/year is found as −7.9 (true −7.8) percentage points per year.

**Limits:**

- **Capacity per unit:** if both units charge and discharge in lockstep, the jointly measured energy can hardly be
  split, and Battery Doctor honestly says "no reliable statement" for the single units. The system capacity is not
  affected. If one unit ages faster, the SOC values drift apart – and exactly then the split becomes identifiable.
- The accuracy depends on the power sensor of the storage. A systematic deviation cannot be detected without an
  external meter, so it is included in the range as ±3 %.
- In winter there are fewer and smaller windows. Then "no reliable statement" becomes more likely.

## Master/slave analysis

Every day these metrics are computed:

- mean and largest SOC difference (master − slave), also during sun only
- minutes of throttling by an existing SOC automation (`throttle_flag`, only read)
- wake-up time: master = first minute after 03:00 with solar power, slave = first stable valid minute after that
- wake-up delay in minutes
- charge rate per unit (%/h in pure charging minutes)

**Wiring change ("re-plug"):**

- `master_slave.replug_date: auto` searches the search range for the day from which the mean difference changes most
  clearly (two means, least squares).
- The break is checked with a permutation test that searches the best break again in every permutation (otherwise p
  would be too optimistic). If the break is not clear, there is no date and no comparison, but a request to enter the
  date.

**Before/after:** up to 28 days per side. For every metric:

- difference of the means with a 95 % range (bootstrap) and p-value (permutation test)
- rating "significant", "not significant" or "preliminary" (fewer than 5 days on one side)
- additionally a season-adjusted value: comparison only between days with similar solar yield

**Alerts:**

- The difference grows over weeks: the last 14 days are at least 3 percentage points above the days before, p < 0.05.
- One unit has 5 % less usable capacity than the other and the ranges are separated.

## Warranty

The template uses the terms of the Growatt NOAH 2000 ("NOAH 2000 Warranty Terms and Conditions"):

- 10 years or 4000 full cycles, whichever comes first.
- Claim only if the capacity is **below 50 %** of 2.048 kWh per battery.
- Basis 300 cycles per year; above that the warranty shrinks proportionally.
- Charging at 0–45 °C, discharging at −20–45 °C.

Adapt `warranty` to your manufacturer. Battery Doctor also shows the **manufacturer reference curve**:
`95 % × (1 − 1.5 %·years − 0.011667 %·cycles) × (1 − reduction for > 300 cycles/year)`.

The **warranty watchdog** warns in these cases (at most every 30 days again):

- The lower bound of the range is below 55 % (limit + 5).
- The forecast reaches 50 % before the warranty ends.
- 12 months before the end, as a reminder to create a dossier.
- 4000 cycles or more than 300 cycles per year are foreseeable.

## Self-monitoring and alerts

Better an alert than a silent gap:

| Event | Alert |
|---|---|
| entity missing | immediately, with similar names |
| entity renamed (same `unique_id`) | Battery Doctor keeps reading under the new name automatically and asks you to update the YAML |
| integration of an entity changed | notice (check the values) |
| unit changed (e.g. kW instead of W) | values are **not** taken over until clarified (then `python -m akd reset-baseline`) |
| HA update | recorded as an event |
| no fresh values for more than 2 h | during the day or while the SOC is above the discharge limit |
| SOC stands still for 3 h although ≥ 300 Wh flow | battery details of the cloud are stuck |
| slave without values during a whole sunny day | notice the next day |
| temperature outside the warranty limits | **immediately, also at night**; notice from 40 °C |
| capacity drop | last 14 days ≥ 8 % below the days before, ranges separated; immediately |

Every alert is sent once; when it resolves, an all-clear follows. Non-urgent alerts wait during quiet hours (22–08).
If HA is not reachable, alerts stay in a queue and are delivered later.

## Data, retention, backup

- Everything lives in `data/battery-doctor.sqlite`, plus `data/reports/` and `data/dossiers/`.
- **Minute values** are kept for 180 days and then compacted to **15-minute values** (mean, min, max, first/last).
  The 15-minute values are kept forever.
- **Measurement windows, estimates, daily values, alerts and reports** are never deleted (a few MB per year).
- After compaction, periods older than 180 days cannot be searched for new windows again; the windows and results
  found back then are kept completely.
- **Backup:** every day at 03:20 a consistent copy of the database goes to `/backup` (e.g. a NAS share), plus reports
  and dossiers; 30 days are kept. The database is backed up locally first and then copied as a plain file, because
  writing SQLite directly onto a CIFS share can hang on file locks. A failure raises an alert.
- Databases of version 1.0 (German internal codes, file `akku-doktor.sqlite`) are migrated automatically.

## Commands

```bash
docker exec battery-doctor python -m akd status          # short status
docker exec battery-doctor python -m akd selftest        # check connection, entities, notify service
docker exec battery-doctor python -m akd analyze         # re-analyse everything (after changing parameters)
docker exec battery-doctor python -m akd prelearn        # import the history again + re-analyse
docker exec battery-doctor python -m akd report 2026-11  # create a monthly report
docker exec battery-doctor python -m akd dossier system  # warranty dossier (system | <unit>)
docker exec battery-doctor python -m akd synthetic       # estimation on synthetic batteries (acceptance proof)
docker exec battery-doctor python -m akd reset-baseline  # re-learn remembered units/integrations of the entities
```

Update after a code change: `docker build --target test . && docker compose up -d --build`.

## Tests

`docker build --target test .` runs all tests (locally: `python -m pytest -q tests/`).

- `tests/test_estimation.py`: synthetic charge curves with known capacity:
  - system capacity within 2 %, truth inside the range, efficiency
  - capacity per unit (one unit 15 % weaker) within 4 %, with the alert "unequal ageing"
  - artificial ageing of 8 %/year: trend, range and 80 % forecast
  - too little data and coarse SOC steps: "no reliable statement"
  - data gap and calibration jump: not used; sleeping slave: 0 % does not count as empty
  - hourly statistics only: coarse but correct; battery count; constant night power; placeholder temperatures
  - manufacturer formula, warranty watchdog, compaction
- `tests/test_masterslave.py`: a real wiring change is detected (date ±2 days, improvement significant); without a
  change there is no false finding; with too few days after it the result is "preliminary"; wake-up delay.
- `tests/test_e2e.py`: fake Home Assistant and Prometheus:
  - import history, estimate, HA sensors, no flood of pushes at the start
  - all pages of the web UI, `/metrics`, dossier PDF and ZIP, monthly report, export
  - immediate temperature alert; rename followed, missing entity, changed unit, HA update
  - compaction and backup
  - **read-only guarantee**: only `notify` and own sensors; control commands are refused
- `tests/test_german.py`: German configuration keys of version 1.0, German texts with unchanged sensor IDs, migration
  of a version 1.0 database.

## Troubleshooting

- **Web UI shows "first analysis is still running":** wait a few minutes after the first start, `docker logs battery-doctor`.
- **All capacities `unknown`:** the reason is in the sensor attribute `reason` and on the overview. In winter or after
  data gaps there are often too few clean windows – that is on purpose, not an error.
- **"token rejected":** see [Secrets](#secrets-where-the-token-goes-and-how-to-rotate-it).
- **Backup fails:** is the share mounted? Write permissions for `AKD_UID`?
- **Entity renamed:** Battery Doctor follows automatically. Enter the new name in `config.yaml` so it stays correct.

## License

MIT, see [LICENSE](LICENSE).
