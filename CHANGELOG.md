# Changelog

## 1.1.0 – 2026-11-05

First public release.

- English and German (`language: en | de`): notifications, web UI, monthly report, warranty dossier, HA sensor names.
- English configuration keys; the German keys of 1.0 are still accepted. English CLI commands (German aliases kept).
- Databases of 1.0 (German internal codes) are migrated automatically.
- Grafana dashboard in English and German; metrics `akd_noah_*` renamed to `akd_ms_*`.
- Battery count gate: windows only count while all units are reported.
- Placeholder temperatures (0 °C at 0 % SOC, −273 °C) are ignored.
- Backup to network shares: local SQLite backup first, then a plain file copy.

## 1.0.0 – 2026-11-05

Initial version (German only, private):

- Capacity from clean measurement windows with confidence range, per system and per unit.
- Full-cycle equivalents, trend and forecast.
- Master/slave analysis with detection of a wiring change and a before/after comparison.
- Warranty watchdog, self-monitoring, notifications via HA, HA sensors, Prometheus metrics and a Grafana dashboard.
- Monthly report, warranty dossier (PDF + ZIP), CSV export, compaction of old data, daily backup.
