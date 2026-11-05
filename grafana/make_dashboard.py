"""Generates the Grafana dashboard.

    python grafana/make_dashboard.py            -> grafana/battery-doctor.json (English)
    python grafana/make_dashboard.py --lang de  -> grafana/battery-doctor.de.json (German titles)
"""
import json
import sys
from pathlib import Path

DE = "--lang" in sys.argv and sys.argv[sys.argv.index("--lang") + 1] == "de"


def T(en, de):
    return de if DE else en


DS = {"type": "prometheus", "uid": "${datasource}"}
panels = []


def panel(kind, title, exprs, x, y, w, h, unit=None, desc=None, extra=None):
    p = {"id": len(panels) + 1, "type": kind, "title": title, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [{"datasource": DS, "expr": e, "legendFormat": lf, "refId": chr(65 + i),
                      **({"instant": True} if kind in ("stat", "bargauge") else {})} for i, (e, lf) in enumerate(exprs)],
         "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []}, "options": {}}
    if desc:
        p["description"] = desc
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(p.get(k), dict):
            p[k].update(v)
        else:
            p[k] = v
    panels.append(p)


panel("stat", T("Usable capacity", "Nutzbare Kapazität"), [('akd_capacity_kwh{bound="est"}', "{{name}}")], 0, 0, 10, 5, "kwatth",
      T("Estimated usable capacity (current period). No value = no reliable statement.", "Geschätzte nutzbare Kapazität (aktueller Zeitraum). Fehlt der Wert, gibt es keine verlässliche Aussage."),
      {"fieldConfig": {"defaults": {"unit": "kwatth", "decimals": 2}}})
panel("stat", T("State of health (SOH)", "Gesundheit (SOH)"), [("akd_soh_percent", "{{name}}")], 10, 0, 8, 5, "percent", None,
      {"fieldConfig": {"defaults": {"unit": "percent", "decimals": 1, "thresholds": {"mode": "absolute", "steps": [
          {"color": "red", "value": None}, {"color": "orange", "value": 55}, {"color": "green", "value": 80}]}}}})
panel("stat", T("Open alerts", "Offene Meldungen"), [("akd_alerts_open", T("open", "offen"))], 18, 0, 3, 5, None, None,
      {"fieldConfig": {"defaults": {"thresholds": {"mode": "absolute", "steps": [
          {"color": "green", "value": None}, {"color": "orange", "value": 1}]}}}})
panel("stat", "Home Assistant", [("akd_ha_connected", "HA")], 21, 0, 3, 5, None, None,
      {"fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": {
          "0": {"text": T("disconnected", "getrennt"), "color": "red"}, "1": {"text": T("connected", "verbunden"), "color": "green"}}}]}}})
panel("timeseries", T("Capacity with 95 % range", "Kapazität mit 95-%-Bereich"), [('akd_capacity_kwh{bound="est"}', "{{name}}"),
                                                   ('akd_capacity_kwh{bound="lo"}', T("{{name}} lower", "{{name}} unten")),
                                                   ('akd_capacity_kwh{bound="hi"}', T("{{name}} upper", "{{name}} oben"))],
      0, 5, 16, 9, "kwatth")
panel("timeseries", T("Full cycles", "Vollzyklen"), [("akd_full_cycles", "{{storage}}")], 16, 5, 8, 9)
panel("timeseries", "SOC", [("akd_soc_percent", "{{role}}")], 0, 14, 12, 8, "percent")
panel("timeseries", T("Battery temperature", "Akkutemperatur"), [("akd_temperature_celsius", "{{unit}}")], 12, 14, 12, 8, "celsius")
panel("timeseries", T("Master/slave SOC difference (daily mean)", "SOC-Differenz Master/Slave (Tagesmittel)"), [("akd_ms_soc_diff_percent", T("difference", "Differenz"))], 0, 22, 8, 8, "percent")
panel("timeseries", T("Slave wake-up delay", "Aufwachverzug Slave"), [("akd_ms_wake_delay_minutes", T("minutes", "Minuten"))], 8, 22, 8, 8, "m")
panel("stat", T("Re-plug effect", "Effekt Umstecken"), [("akd_ms_replug_effect_pp", T("change of the difference", "Änderung der Differenz"))], 16, 22, 8, 8, "percent",
      T("Negative = difference got smaller (better).", "Negativ = Differenz kleiner geworden (besser)."))
panel("timeseries", T("Data age", "Alter der Daten"), [("akd_data_age_seconds", "{{role}}")], 0, 30, 12, 7, "s")
panel("stat", T("Clean measurement windows", "Saubere Messfenster"), [("akd_estimate_windows", "{{name}}")], 12, 30, 12, 7)

dash = {"uid": "battery-doctor", "title": T("Battery Doctor", "Akku-Doktor"), "tags": ["homelab", "energy"], "timezone": "browser",
        "schemaVersion": 39, "version": 1, "refresh": "5m", "time": {"from": "now-30d", "to": "now"},
        "templating": {"list": [{"name": "datasource", "type": "datasource", "query": "prometheus",
                                 "label": T("Data source", "Datenquelle"), "current": {}, "hide": 0}]},
        "panels": panels}
Path(__file__).with_name("battery-doctor.de.json" if DE else "battery-doctor.json").write_text(json.dumps(dash, ensure_ascii=False, indent=1), encoding="utf-8")
print("written:", "battery-doctor.de.json" if DE else "battery-doctor.json", len(panels), "panels")
