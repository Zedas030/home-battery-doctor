"""Daily values: energy, full-cycle shares, temperatures and the master/slave metrics."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .config import Config
from .grid import Grid, build
from .store import Store


def day_bounds(cfg: Config, d: date) -> tuple[float, float]:
    a = datetime.combine(d, time(0), cfg.tz).timestamp()
    b = datetime.combine(d + timedelta(days=1), time(0), cfg.tz).timestamp()
    return a, b


def compute_day(cfg: Config, store: Store, d: date) -> dict | None:
    t0, t1 = day_bounds(cfg, d)
    g = build(store, cfg, t0, t1)
    have = sum(1 for v in g.get("ch") if v is not None)
    if have >= 60:
        prev = {}
        for u in cfg.units:
            rows = [v for _, v in store.series(f"{u}_soc", t0 - 86400, t0) if v > 0 or not cfg.units[u].sleeps_with_zero]
            prev[u] = rows[-1] if rows else None
        return _from_minutes(cfg, g, prev)
    return _from_hours(cfg, store, t0, t1)


def _from_minutes(cfg: Config, g: Grid, prev_soc: dict | None = None) -> dict:
    ch, dis, sol = g.get("ch"), g.get("dis"), g.get("sol")
    out = {"source": "minute", "minutes_with_data": sum(1 for v in ch if v is not None)}
    out["e_charge_wh"] = round(sum(v for v in ch if v is not None) / 60, 1)
    out["e_discharge_wh"] = round(sum(v for v in dis if v is not None) / 60, 1)
    out["e_solar_wh"] = round(sum(v for v in sol if v is not None) / 60, 1) if "sol" in g.data else None
    soc = g.get("sys_soc")
    vals = [v for v in soc if v is not None]
    out["soc_min"], out["soc_max"] = (min(vals), max(vals)) if vals else (None, None)
    w = cfg.warranty
    for u in cfg.units:
        arr = g.get(f"{u}_soc")
        # Full-cycle share per unit = sum of downward SOC steps / 100. Across sleep/data gaps the distance between
        # valid values counts (a sleeping unit still discharges); consecutive jumps > max_soc_jump are calibration.
        drops, prev, gap = 0.0, (prev_soc or {}).get(u), True
        for v in arr:
            if v is None:
                gap = True
                continue
            if prev is not None and v < prev and (gap or prev - v <= cfg.est.max_soc_jump_percent):
                drops += prev - v
            prev, gap = v, False
        out[f"{u}_down_percent"] = round(drops, 1)
        valid = [v for v in arr if v is not None]
        out[f"{u}_soc_min"] = min(valid) if valid else None
        out[f"{u}_soc_max"] = max(valid) if valid else None
        out[f"{u}_minutes_with_values"] = len(valid)
        temps = [v for v in g.get(f"{u}_temp") if v is not None]
        out[f"{u}_temp_min"] = min(temps) if temps else None
        out[f"{u}_temp_max"] = max(temps) if temps else None
        # warranty conditions: charge 0..45 °C, discharge -20..45 °C (configurable)
        viol = 0
        for i, tv in enumerate(g.get(f"{u}_temp")):
            if tv is None:
                continue
            charging = (ch[i] or 0) > cfg.est.min_power_w
            discharging = (dis[i] or 0) > cfg.est.min_power_w
            if charging and not (w.charge_temp_min <= tv <= w.charge_temp_max):
                viol += 1
            elif discharging and not (w.discharge_temp_min <= tv <= w.discharge_temp_max):
                viol += 1
            elif tv > w.charge_temp_max:
                viol += 1
        out[f"{u}_temp_violation_min"] = viol
    if cfg.ms:
        out.update(_master_slave(cfg, g))
    return out


def _master_slave(cfg: Config, g: Grid) -> dict:
    ms = cfg.ms
    m, s = g.get(f"{ms.master}_soc"), g.get(f"{ms.slave}_soc")
    sol, ch, dis = g.get("sol"), g.get("ch"), g.get("dis")
    sun = [(v or 0) > 50 for v in sol] if "sol" in g.data else [7 * 60 <= i <= 19 * 60 for i in range(g.n)]
    both = [(i, m[i] - s[i]) for i in range(g.n) if m[i] is not None and s[i] is not None]
    both_sun = [x for i, x in both if sun[i]]
    sun_minutes = sum(sun)
    out = {"ms_minutes_both": len(both), "ms_sun_minutes": sun_minutes,
           "ms_coverage": round(len(both_sun) / sun_minutes, 3) if sun_minutes else 0.0}
    if both:
        out["ms_diff_mean"] = round(sum(x for _, x in both) / len(both), 2)   # master − slave
        out["ms_diff_abs"] = round(sum(abs(x) for _, x in both) / len(both), 2)
        out["ms_diff_max"] = max((x for _, x in both), key=abs)
    if both_sun:
        out["ms_diff_abs_sun"] = round(sum(abs(x) for x in both_sun) / len(both_sun), 2)
    th = g.get("throttle")
    out["ms_throttle_min"] = sum(1 for v in th if v == 1) if "throttle" in g.data else None
    # Waking up: master = first minute after 03:00 with solar power; slave = first stable valid minute after
    # that, if it reported no values since 03:00 (0 % while asleep)
    start = 3 * 60
    mw = next((i for i in range(start, g.n) if (sol[i] or 0) > 20), None) if "sol" in g.data else None
    if mw is not None:
        out["ms_master_awake"] = _hhmm(g, mw)
        slept = any(s[i] is None for i in range(start, mw + 1))
        if not slept:
            out["ms_slave_awake"] = out["ms_master_awake"]
            out["ms_wake_delay_min"] = 0
        else:
            sw = next((i for i in range(mw, g.n) if s[i] is not None and all(s[j] is not None for j in
                                                                               range(i, min(i + 10, g.n)))), None)
            if sw is not None:
                out["ms_slave_awake"] = _hhmm(g, sw)
                out["ms_wake_delay_min"] = sw - mw
            else:
                out["ms_slave_awake"] = None
                out["ms_wake_delay_min"] = None
                out["ms_slave_not_awake"] = True
    # charge rate per unit (%/h) in pure charging minutes
    for key, arr in (("master", m), ("slave", s)):
        up, mins, prev = 0.0, 0, None
        for i in range(g.n):
            charging = (ch[i] or 0) > cfg.est.min_power_w and (dis[i] or 0) < 5
            if arr[i] is None:
                prev = None
                continue
            if charging:
                mins += 1
                if prev is not None and arr[i] > prev and arr[i] - prev <= cfg.est.max_soc_jump_percent:
                    up += arr[i] - prev
            prev = arr[i]
        out[f"ms_charge_rate_{key}_pph"] = round(up / (mins / 60), 2) if mins >= 30 else None
    return out


def _hhmm(g: Grid, i: int) -> str:
    return datetime.fromtimestamp(g.ts(i), g.tz).strftime("%H:%M")


def _from_hours(cfg: Config, store: Store, t0: float, t1: float) -> dict | None:
    """Older days with hourly statistics only: energy, downward steps (coarse), daily mean of the difference."""
    def h(k):
        return {ts: (mean, mn, mx) for ts, mean, mn, mx in store.hourly(k, t0, t1)}
    ch, dis = h("ch"), h("dis")
    if len(ch) < 12:
        return None
    out = {"source": "hourly", "hours_with_data": len(ch),
           "e_charge_wh": round(sum(v[0] or 0 for v in ch.values()), 1),
           "e_discharge_wh": round(sum(v[0] or 0 for v in dis.values()), 1)}
    sol = h("sol")
    out["e_solar_wh"] = round(sum(v[0] or 0 for v in sol.values()), 1) if sol else None
    w = cfg.warranty
    for u in cfg.units:
        hs = h(f"{u}_soc")
        means = [hs[t][0] for t in sorted(hs) if hs[t][0] is not None and (hs[t][1] or 0) > 0]
        out[f"{u}_down_percent"] = round(sum(max(0.0, a - b) for a, b in zip(means, means[1:])), 1)
        # hours in which the unit reported 0 % (offline/asleep) deliver 0 °C as a placeholder
        ht = {t: (v if not ((hs.get(t, (None, None, None))[1] == 0) and v[1] == 0) else (v[0], None, v[2]))
              for t, v in h(f"{u}_temp").items()}
        ok = lambda v: v is not None and -40 <= v <= 90  # noqa: E731  (ignore error values such as -273 °C)
        mx = [v[2] for v in ht.values() if ok(v[2])]
        mn = [v[1] for v in ht.values() if ok(v[1])]
        out[f"{u}_temp_min"] = min(mn) if mn else None
        out[f"{u}_temp_max"] = max(mx) if mx else None
        out[f"{u}_temp_violation_h"] = sum(1 for v in ht.values() if (ok(v[2]) and v[2] > w.charge_temp_max)
                                           or (ok(v[1]) and v[1] < w.discharge_temp_min))
    if cfg.ms:
        hm, hs = h(f"{cfg.ms.master}_soc"), h(f"{cfg.ms.slave}_soc")
        diffs = [hm[t][0] - hs[t][0] for t in hm if t in hs and hm[t][0] is not None and hs[t][0] is not None
                 and (hs[t][1] or 0) > 0]
        if diffs:
            out["ms_diff_mean"] = round(sum(diffs) / len(diffs), 2)
            out["ms_diff_abs"] = round(sum(abs(x) for x in diffs) / len(diffs), 2)
        out["ms_coverage"] = round(len(diffs) / 24, 3)
    return out
