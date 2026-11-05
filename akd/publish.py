"""Own sensors in Home Assistant and Prometheus metrics."""
from __future__ import annotations

from datetime import datetime

from . import __version__
from .config import Config
from .i18n import L, STATUS, tr

# German installations keep their original entity IDs and state values
SUFFIX_DE = {"capacity": "kapazitaet", "soh": "soh", "full_cycles": "vollzyklen", "forecast_80": "prognose_80",
             "warranty": "garantie", "ms_soc_difference": "noah_soc_differenz", "ms_wake_delay": "noah_aufwachverzug",
             "ms_replug_effect": "noah_umstecken_effekt"}
WARRANTY_STATE_DE = {"ok": "ok", "warning": "warnung", "claim": "anspruch", "ending": "ablauf_naht", "expired": "abgelaufen"}
STATUS_STATE_DE = {"ok": "ok", "warning": "warnung", "critical": "kritisch", "error": "fehler"}


def _sfx(name: str) -> str:
    return L(name, SUFFIX_DE[name])


def _attrs(base: dict, **kw):
    d = dict(base)
    d.update({k: v for k, v in kw.items() if v is not None})
    return d


def sensors(cfg: Config, summary: dict, monitor, open_alerts: list) -> list[tuple[str, object, dict]]:
    p = cfg.sensor_prefix
    out = []
    state = "error" if not monitor.ha_connected else "warning" if open_alerts else "ok"
    if any(a["level"] == "critical" for a in open_alerts):
        state = "critical"
    out.append((f"sensor.{p}_status", L(state, STATUS_STATE_DE[state]), {
        "friendly_name": f"{cfg.app_name} Status", "icon": "mdi:battery-heart-variant", "version": __version__,
        L("open_alerts", "offene_meldungen"): [a["title"] + ": " + a["message"] for a in open_alerts][:10],
        L("last_analysis", "letzte_auswertung"): datetime.fromtimestamp(summary["time"], cfg.tz).isoformat()
        if summary.get("time") else None}))
    for scope, sc in (summary.get("scopes") or {}).items():
        cur = sc.get("current") or {}
        name = cfg.scope_name(scope)
        st = cur.get("status", "no_data")
        ok = st == "ok"
        base = {"status": tr(STATUS, st), L("windows", "messfenster"): cur.get("n", 0),
                L("reason", "grund"): (cur.get("detail") or {}).get("reason")}
        low, high = L("lower_bound", "untergrenze"), L("upper_bound", "obergrenze")
        out.append((f"sensor.{p}_{scope}_{_sfx('capacity')}", round(cur["cap_kwh"], 3) if ok else "unknown", _attrs(
            base, friendly_name=L(f"{name} usable capacity", f"{name} nutzbare Kapazität"), unit_of_measurement="kWh",
            device_class="energy_storage", state_class="measurement", icon="mdi:battery-high",
            **{low: cur.get("lo"), high: cur.get("hi"), L("nominal_kwh", "nennkapazitaet"): cfg.nominal(scope),
               L("efficiency", "wirkungsgrad"): cur.get("eff")})))
        out.append((f"sensor.{p}_{scope}_{_sfx('soh')}", round(cur["soh"], 1) if ok else "unknown", _attrs(
            base, friendly_name=L(f"{name} state of health", f"{name} Gesundheit (SOH)"), unit_of_measurement="%",
            state_class="measurement", icon="mdi:heart-pulse",
            **{low: (cur.get("detail") or {}).get("soh_lo"), high: (cur.get("detail") or {}).get("soh_hi")})))
        cyc = (summary.get("cycles") or {}).get(scope)
        out.append((f"sensor.{p}_{scope}_{_sfx('full_cycles')}", round(cyc, 1) if cyc is not None else "unknown", {
            "friendly_name": L(f"{name} full cycles", f"{name} Vollzyklen"), "state_class": "total_increasing",
            "icon": "mdi:battery-sync", L("since", "seit"): (summary.get("cycles") or {}).get("since")}))
        trd = sc.get("trend") or {}
        p80 = trd.get("forecast_80") or {}
        out.append((f"sensor.{p}_{scope}_{_sfx('forecast_80')}", p80.get("date") or "unknown", _attrs(
            {}, friendly_name=L(f"{name} 80 % expected", f"{name} 80 % voraussichtlich"), icon="mdi:calendar-clock",
            text=p80.get("text") or trd.get("text"), **{L("trend_pp_per_year", "trend_pp_pro_jahr"): trd.get("slope_pp_year")})))
        g = sc.get("warranty") or {}
        gs = g.get("status", "unknown")
        out.append((f"sensor.{p}_{scope}_{_sfx('warranty')}", L(gs, WARRANTY_STATE_DE.get(gs, gs)), _attrs(
            {}, friendly_name=L(f"{name} warranty", f"{name} Garantie"), icon="mdi:shield-check",
            **{L("end", "ende"): g.get("end_effective"), L("reference_soh", "referenz_soh"): g.get("reference_soh"),
               L("notes", "hinweise"): g.get("notes")})))
    ms = summary.get("master_slave")
    if ms:
        days = ms.get("recent_days") or {}
        last = days[max(days)] if days else {}
        out.append((f"sensor.{p}_{_sfx('ms_soc_difference')}", last.get("ms_diff_abs", "unknown"), {
            "friendly_name": L("Master/slave SOC difference (today, mean)", "NOAH SOC-Differenz (heute, Mittel)"),
            "unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:scale-unbalanced",
            L("day", "tag"): max(days) if days else None, L("largest", "groesste"): last.get("ms_diff_max")}))
        delay = next((days[d].get("ms_wake_delay_min") for d in sorted(days, reverse=True)
                      if days[d].get("ms_wake_delay_min") is not None), None)
        out.append((f"sensor.{p}_{_sfx('ms_wake_delay')}", delay if delay is not None else "unknown", {
            "friendly_name": L("Slave wake-up delay", "NOAH Slave Aufwachverzug"), "unit_of_measurement": "min",
            "icon": "mdi:weather-sunset-up"}))
        cmp_ = ms.get("comparison") or {}
        v = cmp_.get("metrics", {}).get("ms_diff_abs", {})
        out.append((f"sensor.{p}_{_sfx('ms_replug_effect')}", v.get("difference", "unknown"), {
            "friendly_name": L("Re-plug effect (SOC difference)", "NOAH Effekt Umstecken (SOC-Differenz)"),
            "unit_of_measurement": "%", "icon": "mdi:swap-horizontal", "status": v.get("status"),
            L("range", "bereich"): v.get("range"), L("date", "datum"): (ms.get("replug") or {}).get("date"),
            L("summary", "fazit"): cmp_.get("summary")}))
    return out


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def metrics(cfg: Config, summary: dict, monitor, store, extra: dict) -> str:
    lines = []

    def g(name, help_, rows):
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} gauge")
        for labels, v in rows:
            if v is None:
                continue
            lab = ",".join(f'{k}="{_esc(x)}"' for k, x in labels.items())
            lines.append(f"{name}{{{lab}}} {float(v):.6g}" if lab else f"{name} {float(v):.6g}")

    sc = summary.get("scopes") or {}
    cap, soh, rel, n = [], [], [], []
    for scope, s in sc.items():
        cur = s.get("current") or {}
        lab = {"storage": scope, "name": cfg.scope_name(scope)}
        n.append((lab, cur.get("n", 0)))
        rel.append((lab, 1 if cur.get("status") == "ok" else 0))
        if cur.get("cap_kwh") and cur.get("status") == "ok":   # uncertain values are not exported as numbers
            for b, k in (("est", "cap_kwh"), ("lo", "lo"), ("hi", "hi")):
                cap.append(({**lab, "bound": b}, cur.get(k)))
            soh.append((lab, cur.get("soh")))
    g("akd_capacity_kwh", "Estimated usable capacity (est/lo/hi)", cap)
    g("akd_soh_percent", "State of health in % of the nominal capacity", soh)
    g("akd_estimate_reliable", "1 = reliable estimate", rel)
    g("akd_estimate_windows", "Clean measurement windows in the current period", n)
    g("akd_nominal_kwh", "Nominal capacity", [({"storage": s, "name": cfg.scope_name(s)}, cfg.nominal(s)) for s in sc])
    g("akd_full_cycles", "Full-cycle equivalents", [({"storage": s}, (summary.get("cycles") or {}).get(s)) for s in sc])
    g("akd_efficiency_ratio", "Discharge side / charge side",
      [({"storage": "system"}, (sc.get("system", {}).get("current") or {}).get("eff"))])
    g("akd_trend_pp_per_year", "SOH trend in percentage points per year",
      [({"storage": s}, (v.get("trend") or {}).get("slope_pp_year")) for s, v in sc.items()])
    g("akd_warranty_reference_percent", "Manufacturer reference curve (warranted energy)",
      [({"storage": s}, (v.get("warranty") or {}).get("reference_soh")) for s, v in sc.items()])
    live = monitor.live
    g("akd_soc_percent", "Current SOC", [({"role": r}, (live.get(r) or {}).get("value"))
                                         for r in ["sys_soc"] + [f"{u}_soc" for u in cfg.units]])
    g("akd_temperature_celsius", "Battery temperature", [({"unit": u}, (live.get(f"{u}_temp") or {}).get("value"))
                                                          for u in cfg.units])
    g("akd_power_watts", "Charge/discharge/solar power", [({"role": r}, (live.get(r) or {}).get("value"))
                                                          for r in ("ch", "dis", "sol")])
    ages = []
    for r in cfg.entity_roles():
        last = store.last(r)
        ages.append(({"role": r}, (monitor.last_poll or 0) - last[0] if last and monitor.last_poll else None))
    g("akd_data_age_seconds", "Seconds since the last fresh value", ages)
    ms = summary.get("master_slave") or {}
    days = ms.get("recent_days") or {}
    if days:
        last = days[max(days)]
        g("akd_ms_soc_diff_percent", "SOC difference master−slave (absolute, daily mean)", [({}, last.get("ms_diff_abs"))])
        delay = next((days[d].get("ms_wake_delay_min") for d in sorted(days, reverse=True)
                      if days[d].get("ms_wake_delay_min") is not None), None)
        g("akd_ms_wake_delay_minutes", "Wake-up delay of the slave", [({}, delay)])
    v = (ms.get("comparison") or {}).get("metrics", {}).get("ms_diff_abs", {})
    g("akd_ms_replug_effect_pp", "Change of the SOC difference after the re-plug", [({}, v.get("difference"))])
    g("akd_alerts_open", "Open alerts", [({}, len(store.alerts("open")))])
    g("akd_ha_connected", "1 = Home Assistant reachable", [({}, 1 if monitor.ha_connected else 0)])
    g("akd_last_analysis_timestamp_seconds", "Time of the last analysis", [({}, summary.get("time"))])
    g("akd_db_size_bytes", "Size of the database", [({}, store.size_bytes())])
    for k, val in extra.items():
        g(f"akd_{k}", k, [({}, val)])
    g("akd_info", "Version", [({"version": __version__}, 1)])
    return "\n".join(lines) + "\n"
