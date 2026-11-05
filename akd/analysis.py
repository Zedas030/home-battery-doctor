"""Bring the analysis together: windows, daily values, estimates per period, trend, warranty, master/slave, alerts."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from . import masterslave as msmod
from .alerts import Alerts
from .config import Config
from .daily import compute_day
from .estimate import OK, estimate_all
from .grid import build
from .i18n import L, fmt_date, num
from .store import Store
from .trend import trend, warranty_status
from .windows import find_windows, hourly_windows

log = logging.getLogger("akd.analysis")
CHUNK_DAYS = 7


class Analyzer:
    def __init__(self, cfg: Config, store: Store, alerts: Alerts):
        self.cfg, self.store, self.alerts = cfg, store, alerts
        self.summary: dict = {}

    # ------------------------------------------------------------------ windows
    def refresh_windows(self, t0: float, t1: float, now: float) -> int:
        n = 0
        a, t1 = int(t0), int(t1)
        while a < t1:
            b = min(t1, a + CHUNK_DAYS * 86400)
            g = build(self.store, self.cfg, a, b + 86400 if b < t1 else b)
            wins = [w for w in find_windows(self.cfg, g) if a <= w["t0"] < b]
            self.store.delete_windows("minute", a, b)
            for w in wins:
                self.store.put_window(w, now)
            n += len(wins)
            a = b
        return n

    def refresh_hourly_windows(self, now: float) -> int:
        if not self.cfg.est.use_hourly_statistics:
            return 0
        rng = self.store.raw_range("ch")
        before = rng[0] if rng and rng[0] else None
        hours = {key: {ts: (mean, mn, mx) for ts, mean, mn, mx in self.store.hourly(key, 0, now)}
                 for key in self.cfg.entity_roles()}
        if not hours.get("sys_soc"):
            return 0
        wins = hourly_windows(self.cfg, hours, before)
        self.store.delete_windows("hourly", 0, now + 1)
        for w in wins:
            self.store.put_window(w, now)
        return len(wins)

    # ------------------------------------------------------------------ days
    def refresh_daily(self, d0: date, d1: date) -> int:
        n = 0
        d = d0
        while d <= d1:
            r = compute_day(self.cfg, self.store, d)
            if r:
                self.store.put_daily(d.isoformat(), r)
                n += 1
            d += timedelta(days=1)
        return n

    def first_day(self) -> date | None:
        cands = []
        r = self.store.raw_range()
        if r and r[0]:
            cands.append(r[0])
        h = self.store.hourly_range()
        if h and h[0]:
            cands.append(h[0])
        if not cands:
            return None
        return datetime.fromtimestamp(min(cands), self.cfg.tz).date()

    # ------------------------------------------------------------------ estimates
    def compute_estimates(self, now: float) -> dict:
        cfg = self.cfg
        allw = self.store.windows()
        periods: dict[str, list] = {}
        for w in allw:
            periods.setdefault(datetime.fromtimestamp(w["t0"], cfg.tz).strftime("%Y-%m"), []).append(w)
        cur_from = now - cfg.est.current_period
        short = now - cfg.est.drop_period
        periods["current"] = [w for w in allw if w["t0"] >= cur_from]
        periods["short"] = [w for w in allw if w["t0"] >= short]
        periods["previous"] = [w for w in allw if cur_from <= w["t0"] < short]
        res = {}
        for p, wins in periods.items():
            # minute windows win; hourly windows only if the period has no minute windows
            mins = [w for w in wins if w["source"] == "minute"]
            use = mins if any(w["used"] for w in mins) else wins
            est = estimate_all(cfg, use)
            for scope, e in est.items():
                self.store.put_estimate(scope, p, now, e)
            res[p] = est
        return res

    def cycles(self) -> dict:
        cfg = self.cfg
        days = self.store.daily()
        e_dis = sum(v.get("e_discharge_wh") or 0 for v in days.values())
        out = {"system": e_dis / 1000 / cfg.nominal_total_kwh, "discharged_kwh": e_dis / 1000,
               "since": min(days) if days else None}
        for u in cfg.units:
            out[u] = sum(v.get(f"{u}_down_percent") or 0 for v in days.values()) / 100
        return out

    # ------------------------------------------------------------------ everything
    def run(self, now: float, full: bool = False) -> dict:
        cfg = self.cfg
        today = datetime.fromtimestamp(now, cfg.tz).date()
        first = self.first_day()
        if first is None:
            self.summary = {"status": "no_data", "time": now}
            return self.summary
        if full:
            self.refresh_hourly_windows(now)
            self.refresh_windows(datetime.combine(first, datetime.min.time(), cfg.tz).timestamp(), now, now)
            self.refresh_daily(first, today)
        else:
            self.refresh_windows(now - 3 * 86400, now, now)
            self.refresh_daily(today - timedelta(days=2), today)
        res = self.compute_estimates(now)
        cyc = self.cycles()
        out = {"time": now, "scopes": {}, "cycles": cyc}
        for scope in cfg.scopes():
            monthly = [e for e in self.store.estimates(scope) if re.fullmatch(r"\d{4}-\d{2}", e["period"])]
            tr = trend(cfg, scope, monthly, today)
            cur = res.get("current", {}).get(scope)
            wz = warranty_status(cfg, scope, cur, tr, cyc.get(scope, 0), today)
            out["scopes"][scope] = {"current": cur, "trend": tr, "warranty": wz, "months": monthly,
                                    "short": res.get("short", {}).get(scope),
                                    "previous": res.get("previous", {}).get(scope)}
        if cfg.ms:
            out["master_slave"] = self.master_slave(today)
        self.summary = out
        self.evaluate(now, today)
        return out

    def master_slave(self, today: date) -> dict:
        cfg = self.cfg
        daily = self.store.daily()
        out = {}
        if cfg.ms.replug_date:
            switch = cfg.ms.replug_date
            out["replug"] = {"status": "configured", "date": switch.isoformat(),
                             "text": L(f"re-plug date from the configuration: {fmt_date(switch)}",
                                       f"Umstecken laut Konfiguration am {fmt_date(switch)}")}
        else:
            cp = msmod.detect_changepoint(cfg, daily)
            out["replug"] = cp
            switch = date.fromisoformat(cp["date"]) if cp.get("status") == "ok" else None
            if cp.get("status") == "no_clear_break":
                cp["text"] = L(f"No clear break in the SOC difference (best candidate {cp['date']}, p = {num(cp['p'], 2)}). "
                               f"Please set master_slave.replug_date in config.yaml.",
                               f"Kein klarer Bruch in der SOC-Differenz erkennbar (bester Kandidat {cp['date']}, "
                               f"p = {num(cp['p'], 2)}). Datum bitte in config.yaml eintragen (master_slave.replug_date).")
        if switch:
            out["comparison"] = msmod.before_after(cfg, daily, switch)
        out["worsening"] = msmod.worsening(cfg, daily, today, switch)
        out["recent_days"] = {d: v for d, v in daily.items() if d >= (today - timedelta(days=14)).isoformat()}
        return out

    # ------------------------------------------------------------------ alerts from the analysis
    def evaluate(self, now: float, today: date):
        cfg, al, s = self.cfg, self.alerts, self.summary
        for scope, sc in s["scopes"].items():
            name = cfg.scope_name(scope)
            # capacity drop: short period clearly below the period before, ranges separated
            k, v = sc.get("short"), sc.get("previous")
            key = f"drop_{scope}"
            if k and v and k.get("cap_kwh") and v.get("status") == OK and k.get("n", 0) >= 3:
                drop = 1 - k["cap_kwh"] / v["cap_kwh"]
                days = cfg.est.drop_period / 86400
                if drop >= cfg.est.drop_threshold_relative and k["hi"] < v["lo"]:
                    al.raise_(now, key, L(f"🔋 {name}: capacity drop", f"🔋 {name}: Kapazitätseinbruch"), L(
                        f"Last {days:.0f} days {num(k['cap_kwh'])} kWh (range {num(k['lo'])}–{num(k['hi'])}) instead of "
                        f"{num(v['cap_kwh'])} kWh before (−{drop * 100:.0f} %). Outside the normal course.",
                        f"Letzte {days:.0f} Tage {num(k['cap_kwh'])} kWh (Bereich {num(k['lo'])}–{num(k['hi'])}) statt "
                        f"{num(v['cap_kwh'])} kWh davor (−{drop * 100:.0f} %). Außerhalb des normalen Verlaufs."),
                        level="critical", urgent=True)
                elif drop < cfg.est.drop_threshold_relative / 2:
                    al.clear(now, key, L("Capacity back within the normal course.", "Kapazität wieder im normalen Verlauf."))
            # warranty watchdog
            g = sc["warranty"]
            key = f"warranty_{scope}"
            if g["status"] in ("warning", "claim", "ending"):
                al.raise_(now, key, L(f"🛡️ {name}: warranty", f"🛡️ {name}: Garantie"), "; ".join(g["notes"]),
                          level="critical" if g["status"] == "claim" else "warning", urgent=False,
                          renotify_after=30 * 86400)
            elif g["status"] == "ok":
                al.clear(now, key)
        if not cfg.ms:
            return
        ms = cfg.ms
        mname, sname = cfg.units[ms.master].name, cfg.units[ms.slave].name
        # one unit ages faster
        m, sl = s["scopes"].get(ms.master, {}).get("current"), s["scopes"].get(ms.slave, {}).get("current")
        key = "unequal_aging"
        if m and sl and m.get("status") == OK and sl.get("status") == OK:
            rel = abs(m["cap_kwh"] - sl["cap_kwh"]) / ((m["cap_kwh"] + sl["cap_kwh"]) / 2)
            apart = m["hi"] < sl["lo"] or sl["hi"] < m["lo"]
            if rel >= ms.unequal_aging_relative and apart:
                weak = mname if m["cap_kwh"] < sl["cap_kwh"] else sname
                al.raise_(now, key, L("🔋 Units are ageing unequally", "🔋 Einheiten altern ungleich"), L(
                    f"{mname}: {num(m['cap_kwh'])} kWh ({num(m['lo'])}–{num(m['hi'])}), {sname}: {num(sl['cap_kwh'])} kWh "
                    f"({num(sl['lo'])}–{num(sl['hi'])}). {weak} has {rel * 100:.0f} % less usable capacity.",
                    f"{mname}: {num(m['cap_kwh'])} kWh ({num(m['lo'])}–{num(m['hi'])}), {sname}: {num(sl['cap_kwh'])} kWh "
                    f"({num(sl['lo'])}–{num(sl['hi'])}). {weak} hat {rel * 100:.0f} % weniger nutzbare Kapazität."))
            elif rel < ms.unequal_aging_relative / 2:
                al.clear(now, key)
        w = s.get("master_slave", {}).get("worsening")
        key = "ms_worsening"
        if w and w["worse"]:
            al.raise_(now, key, L("🔋 SOC difference is growing", "🔋 SOC-Differenz wird größer"), L(
                f"Last {ms.worsening_days} days {num(w['now'], 1)} percentage points on average, before {num(w['before'], 1)} "
                f"(p = {num(w['p'], 3)}).",
                f"Letzte {ms.worsening_days} Tage im Mittel {num(w['now'], 1)} Prozentpunkte, davor {num(w['before'], 1)} "
                f"(p = {num(w['p'], 3)})."), renotify_after=14 * 86400)
        elif w and not w["worse"]:
            al.clear(now, key, L("SOC difference no longer growing.", "SOC-Differenz nicht mehr steigend."))
        # slave without any value during the whole sunny day yesterday
        y = (today - timedelta(days=1)).isoformat()
        yv = next(iter(self.store.daily(y, y).values()), None)
        key = "slave_no_values"
        if yv and yv.get("source") == "minute" and (yv.get("e_solar_wh") or 0) > 500 and \
                yv.get(f"{ms.slave}_minutes_with_values", 1) == 0:
            al.raise_(now, key, L(f"🔋 {sname}: no values yesterday", f"🔋 {sname}: gestern keine Werte"),
                      L("The slave reported no SOC during a whole sunny day (only 0 % = asleep).",
                        "Der Slave hat einen ganzen Sonnentag lang keinen SOC geliefert (nur 0 % = schläft)."))
        elif yv and yv.get(f"{ms.slave}_minutes_with_values", 0) > 0:
            al.clear(now, key, L("The slave reports values again.", "Slave liefert wieder Werte."))
