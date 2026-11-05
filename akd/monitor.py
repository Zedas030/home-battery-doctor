"""Live polling and self-monitoring: store values; missing/renamed entities, changed units or integrations, data
gaps, frozen SOC, temperatures. Better an alert than a silent gap."""
from __future__ import annotations

import logging
from datetime import datetime

from .alerts import Alerts
from .config import Config
from .grid import _hold
from .history import to_num
from .i18n import L
from .store import Store

log = logging.getLogger("akd.monitor")
CORE = ("sys_soc", "ch", "dis")
INTERNAL = ("input_boolean.", "input_number.")   # HA-internal helpers are always current


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Monitor:
    def __init__(self, cfg: Config, store: Store, alerts: Alerts):
        self.cfg, self.store, self.alerts = cfg, store, alerts
        self.live: dict[str, dict] = {}       # role -> {entity, value, age, state}
        self.ha_connected = False
        self.ha_version = None
        self.last_poll = None
        self.registry: dict[str, dict] = {}   # entity_id -> registry entry
        self.poll_errors = 0
        self.checks_enabled = False   # only after the history import (otherwise false alarms at start)

    @property
    def tag(self) -> str:
        return f"🔧 {self.cfg.app_name}"

    # ---------------------------------------------------------------- role -> entity mapping
    def mapping(self) -> dict[str, str]:
        m = dict(self.cfg.entity_roles())
        m.update({k: v for k, v in (self.store.get_meta("renamed") or {}).items() if k in m})
        return m

    def baseline(self) -> dict:
        return self.store.get_meta("entity_baseline") or {}

    # ---------------------------------------------------------------- registry (hourly)
    def check_registry(self, entries: list[dict], now: float):
        self.registry = {e["entity_id"]: e for e in entries}
        base = self.baseline()
        changed = False
        for role, ent in self.mapping().items():
            e = self.registry.get(ent)
            b = base.get(role)
            if e is None:
                # renamed? look for the same unique_id under a new ID
                if b and b.get("unique_id"):
                    new = next((x for x in entries if x.get("unique_id") == b["unique_id"]
                                and x.get("platform") == b.get("platform")), None)
                    if new and new["entity_id"] != ent:
                        ren = self.store.get_meta("renamed") or {}
                        ren[role] = new["entity_id"]
                        self.store.set_meta("renamed", ren)
                        self.alerts.raise_(now, f"renamed_{role}", L(f"{self.tag}: entity renamed",
                                                                     f"{self.tag}: Entität umbenannt"), L(
                            f"{ent} is now called {new['entity_id']}. I keep reading under the new name automatically – "
                            f"please update config.yaml.",
                            f"{ent} heißt jetzt {new['entity_id']}. Ich lese automatisch unter dem neuen Namen weiter – "
                            f"bitte in config.yaml nachtragen."))
                continue
            info = {"entity_id": ent, "unique_id": e.get("unique_id"), "platform": e.get("platform"),
                    "config_entry_id": e.get("config_entry_id")}
            if b and b.get("platform") and b.get("platform") != info["platform"]:
                self.alerts.raise_(now, f"integration_{role}", L(f"{self.tag}: integration changed",
                                                                 f"{self.tag}: Integration geändert"), L(
                    f"{ent} now comes from '{info['platform']}' instead of '{b['platform']}'. Please check the values "
                    f"(unit, resolution).",
                    f"{ent} kommt jetzt von '{info['platform']}' statt '{b['platform']}'. Werte bitte prüfen "
                    f"(Einheit, Auflösung)."))
            if not b or any(b.get(k) != v for k, v in info.items() if v is not None and k != "entity_id") or \
                    b.get("entity_id") != ent:
                base[role] = {**(b or {}), **info}
                changed = True
        if changed:
            self.store.set_meta("entity_baseline", base)

    def note_version(self, version: str, now: float):
        old = self.store.get_meta("ha_version")
        if old and old != version:
            self.store.event(now, "ha_update", f"Home Assistant {old} → {version}")
            self.store.set_meta("ha_update_time", now)
        self.store.set_meta("ha_version", version)
        self.ha_version = version

    # ---------------------------------------------------------------- poll (every minute)
    def poll(self, states: list[dict], now: float):
        self.last_poll = now
        by_id = {s["entity_id"]: s for s in states}
        base = self.baseline()
        base_changed = False
        missing = []
        # Freshness: HA does not refresh last_reported for unchanged MQTT values. The storage integration delivers
        # all values together, so the newest timestamp of all storage entities counts for all of them.
        reps = []
        for role, ent in self.mapping().items():
            st = by_id.get(ent)
            if st and not ent.startswith(INTERNAL):
                r = _ts(st.get("last_reported")) or _ts(st.get("last_updated"))
                if r:
                    reps.append(r)
        device_fresh = max(reps) if reps else None
        for role, ent in self.mapping().items():
            st = by_id.get(ent)
            lv = self.live.setdefault(role, {"entity": ent})
            lv["entity"] = ent
            if st is None:
                lv.update({"state": "missing", "value": None})
                missing.append((role, ent))
                continue
            self.alerts.clear(now, f"missing_{role}", L(f"{ent} is back.", f"{ent} ist wieder da."))
            attrs = st.get("attributes") or {}
            unit, dc = attrs.get("unit_of_measurement"), attrs.get("device_class")
            b = base.get(role) or {}
            if "unit" not in b:
                b.update({"unit": unit, "device_class": dc, "entity_id": ent})
                base[role] = b
                base_changed = True
            elif b.get("unit") != unit:
                lv.update({"state": "unit", "value": None})
                self.alerts.raise_(now, f"unit_{role}", L(f"{self.tag}: unit changed", f"{self.tag}: Einheit geändert"), L(
                    f"{ent} now reports '{unit}' instead of '{b.get('unit')}'. Values are not taken over until this is "
                    f"clarified (afterwards: command reset-baseline, see README).",
                    f"{ent} meldet jetzt '{unit}' statt '{b.get('unit')}'. Werte werden nicht übernommen, bis das "
                    f"geklärt ist (danach: Befehl reset-baseline, siehe README)."))
                continue
            v = to_num(st.get("state"))
            if ent.startswith(INTERNAL):
                age = 0.0
            else:
                rep = _ts(st.get("last_reported")) or _ts(st.get("last_updated"))
                fresh = max(x for x in (rep, device_fresh) if x) if (rep or device_fresh) else None
                age = now - fresh if fresh else None
            lv.update({"value": v, "age": age, "raw": st.get("state"),
                       "state": "ok" if v is not None else str(st.get("state"))})
            if v is not None and (age is None or age <= _hold(role)):
                self.store.add(role, [(now, v)])
        if base_changed:
            self.store.set_meta("entity_baseline", base)
        for role, ent in missing:
            known = [e for e in self.registry if e.endswith(ent.split(".", 1)[1][-12:])][:3]
            hint = L(f" Similar: {', '.join(known)}.", f" Ähnlich: {', '.join(known)}.") if known else ""
            self.alerts.raise_(now, f"missing_{role}", L(f"{self.tag}: entity missing", f"{self.tag}: Entität fehlt"), L(
                f"{ent} no longer exists in Home Assistant (renamed, or integration changed by an update?).{hint} "
                f"Without it there will be gaps – please check config.yaml.",
                f"{ent} gibt es in Home Assistant nicht mehr (umbenannt oder Integration nach einem Update geändert?)."
                f"{hint} Ohne sie entstehen Lücken – bitte config.yaml prüfen."))
        self._check_temperature(now)
        if self.checks_enabled:
            self._check_data_age(by_id, now)
            self._check_frozen(now)

    def _daylight(self, by_id) -> bool:
        sun = by_id.get("sun.sun")
        if sun:
            return sun.get("state") == "above_horizon"
        h = datetime.now(self.cfg.tz).hour
        return 8 <= h < 17

    def _check_data_age(self, by_id, now):
        cfg = self.cfg
        stale = []
        for role in list(CORE) + [f"{u}_soc" for u in cfg.units]:
            lv = self.live.get(role) or {}
            if not lv or lv.get("state") == "missing":
                continue
            age = lv.get("age")
            if lv.get("value") is None:   # unavailable/unknown: age since the last stored value
                last = self.store.last(role)
                age = now - last[0] if last else None
            if age is None or age > cfg.mon.no_data_after:
                stale.append((role, age))
        soc = (self.live.get("sys_soc") or {}).get("value")
        lim = to_num((by_id.get(cfg.system.discharge_limit) or {}).get("state")) if cfg.system.discharge_limit else None
        relevant = self._daylight(by_id) or (soc is not None and lim is not None and soc > lim + 3)
        key = "no_data"
        if stale and relevant:
            never = L("never", "nie")
            txt = ", ".join(f"{self.mapping()[r]} ({never if a is None else f'{a / 3600:.1f} h'})" for r, a in stale)
            hours = cfg.mon.no_data_after / 3600
            self.alerts.raise_(now, key, L(f"🔋 {cfg.app_name}: no data", f"🔋 {cfg.app_name}: keine Daten"), L(
                f"No fresh values for more than {hours:.0f} h: {txt}. Check the storage integration, its cloud and "
                f"Home Assistant.",
                f"Seit über {hours:.0f} h keine frischen Werte: {txt}. Prüfen: Speicher-Integration, deren Cloud, "
                f"Home Assistant."), renotify_after=24 * 3600)
        elif not stale:
            self.alerts.clear(now, key, L("Data is coming in again.", "Daten kommen wieder."))

    def _check_temperature(self, now):
        cfg, w = self.cfg, self.cfg.warranty
        ch = (self.live.get("ch") or {}).get("value") or 0
        for u, unit in cfg.units.items():
            t = (self.live.get(f"{u}_temp") or {}).get("value")
            key_c, key_w = f"temp_critical_{u}", f"temp_notice_{u}"
            if t is None or not -40 <= t <= 90:   # error values (e.g. -273 °C) are not readings
                continue
            if t == 0 and (self.live.get(f"{u}_soc") or {}).get("value") == 0:   # offline placeholder
                continue
            charging = ch > cfg.est.min_power_w
            if t > w.charge_temp_max or t < w.discharge_temp_min or (charging and t < w.charge_temp_min):
                why = (L(f"charging below {w.charge_temp_min:.0f} °C", f"Laden unter {w.charge_temp_min:.0f} °C")
                       if charging and t < w.charge_temp_min else
                       L(f"above {w.charge_temp_max:.0f} °C", f"über {w.charge_temp_max:.0f} °C") if t > w.charge_temp_max
                       else L(f"below {w.discharge_temp_min:.0f} °C", f"unter {w.discharge_temp_min:.0f} °C"))
                self.alerts.raise_(now, key_c, f"🌡️ {unit.name}: {t:.0f} °C", L(
                    f"Battery temperature {t:.0f} °C ({why}) – outside the warranty conditions. Check location/ventilation.",
                    f"Akkutemperatur {t:.0f} °C ({why}) – außerhalb der Garantiebedingungen. Standort/Belüftung prüfen."),
                    level="critical", urgent=True, renotify_after=6 * 3600)
            elif w.charge_temp_min + 1 <= t <= w.charge_temp_max - 2:
                self.alerts.clear(now, key_c, L(f"Temperature back at {t:.0f} °C.", f"Temperatur wieder {t:.0f} °C."))
            if cfg.mon.temp_warn_above <= t <= w.charge_temp_max:
                self.alerts.raise_(now, key_w, L(f"🌡️ {unit.name}: warm ({t:.0f} °C)", f"🌡️ {unit.name}: warm ({t:.0f} °C)"), L(
                    f"Battery at {t:.0f} °C – still within the warranty (up to {w.charge_temp_max:.0f} °C), but "
                    f"noticeably warm.",
                    f"Akku {t:.0f} °C – noch innerhalb der Garantie (bis {w.charge_temp_max:.0f} °C), aber auffällig warm."),
                    renotify_after=24 * 3600)
            elif t < cfg.mon.temp_warn_above - 3:
                self.alerts.clear(now, key_w)

    def _check_frozen(self, now):
        """The SOC does not move for hours although energy flows according to the power values."""
        cfg = self.cfg
        span = cfg.mon.soc_frozen_after
        rows = self.store.series("sys_soc", now - span, now + 1)
        key = "soc_frozen"
        if len(rows) < span / 60 * 0.8:
            return
        vals = {v for _, v in rows}
        ch = sum(v for _, v in self.store.series("ch", now - span, now + 1)) / 60
        dis = sum(v for _, v in self.store.series("dis", now - span, now + 1)) / 60
        if len(vals) == 1 and abs(ch - dis) >= cfg.mon.soc_frozen_min_wh and \
                not (next(iter(vals)) >= 99 and ch > dis) and not (next(iter(vals)) <= 1 and dis > ch):
            soc = next(iter(vals))
            self.alerts.raise_(now, key, L(f"🔋 {cfg.app_name}: SOC stands still", f"🔋 {cfg.app_name}: SOC steht still"), L(
                f"Total SOC at {soc:.0f} % for {span / 3600:.0f} h although {abs(ch - dis):.0f} Wh flowed. The cloud "
                f"probably delivers no battery details – this time is not used for the estimate.",
                f"Gesamt-SOC seit {span / 3600:.0f} h bei {soc:.0f} %, obwohl {abs(ch - dis):.0f} Wh geflossen sind. "
                f"Vermutlich liefert die Cloud keine Akkudetails – diese Zeit fließt nicht in die Schätzung ein."))
        elif len(vals) > 1:
            self.alerts.clear(now, key, L("SOC is moving again.", "SOC bewegt sich wieder."))
