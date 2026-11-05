"""Ageing trend, forecast ("80 % expected in …"), manufacturer reference curve and warranty watchdog."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .config import Config
from .i18n import L, month_name
from .stats import bootstrap, theil_sen

YEAR = 365.25


def month_mid(period: str) -> date:
    y, m = map(int, period.split("-"))
    return date(y, m, 15)


def years_between(a: date, b: date) -> float:
    return (b - a).days / YEAR


def _month(d: date) -> str:
    return month_name(d.month, d.year)


def trend(cfg: Config, scope: str, monthly: list[dict], today: date) -> dict:
    """Theil-Sen over the monthly estimates (status ok only). Slope in SOH percentage points per year."""
    pts = sorted((month_mid(e["period"]), e["soh"]) for e in monthly
                 if e["status"] == "ok" and e.get("soh") is not None)
    out = {"points": len(pts), "status": "too_early"}
    if len(pts) < 2:
        out["text"] = L("not enough monthly values for a trend yet", "noch zu wenige Monatswerte für einen Trend")
        return out
    span_months = (pts[-1][0] - pts[0][0]).days / 30.44 + 1
    x0 = pts[0][0]
    xs = [years_between(x0, d) for d, _ in pts]
    ys = [y for _, y in pts]
    m, b = theil_sen(xs, ys)
    out.update({"slope_pp_year": round(m, 3), "months": round(span_months, 1)})
    if len(pts) >= 3:
        lo, hi = bootstrap(list(zip(xs, ys)), lambda s: theil_sen([a for a, _ in s], [c for _, c in s])[0], b=500)
        out["slope_range"] = [round(lo, 3), round(hi, 3)]
    else:
        lo = hi = None
    if span_months < cfg.est.trend_min_months or len(pts) < 3:
        out["text"] = L(f"trend only after {cfg.est.trend_min_months} months with reliable estimates "
                        f"(so far {span_months:.0f})",
                        f"Trend erst ab {cfg.est.trend_min_months} Monaten mit verlässlicher Schätzung "
                        f"(bisher {span_months:.0f})")
        return out
    if m >= 0 or (hi is not None and hi >= 0):
        out["status"] = "no_trend"
        out["text"] = L("no measurable capacity loss – forecast not foreseeable",
                        "kein messbarer Kapazitätsverlust – Prognose nicht absehbar")
        return out
    out["status"] = "ok"
    now_x = years_between(x0, today)
    current = m * now_x + b
    out["current_trend_soh"] = round(current, 2)
    for thr in (80, cfg.warranty.min_remaining_percent):
        key = f"forecast_{int(thr)}"
        if current <= thr:
            out[key] = {"date": today.isoformat(), "text": L(f"{thr:.0f} % already reached", f"{thr:.0f} % bereits erreicht")}
            continue
        t_est = (thr - b) / m
        rng = []
        for s in (lo, hi):  # steeper / flatter slope, same current value
            if s is not None and s < 0:
                rng.append(now_x + (thr - current) / s)
        d = x0 + timedelta(days=t_est * YEAR)
        item = {"date": d.isoformat(), "text": L(f"{thr:.0f} % expected in {_month(d)}",
                                                 f"{thr:.0f} % voraussichtlich im {_month(d)}")}
        if len(rng) == 2:
            a, c = sorted(x0 + timedelta(days=r * YEAR) for r in rng)
            item["range"] = [a.isoformat(), c.isoformat()]
            item["text"] += L(f" (range {_month(a)} to {_month(c)})", f" (Bereich {_month(a)} bis {_month(c)})")
        out[key] = item
    return out


def growatt_reference(cfg: Config, years: float, cycles: float) -> float:
    """Warranted energy per the manufacturer terms in % of the nominal capacity (Growatt NOAH formula):
    (nominal × 95 %) × (1 − 1.5 %·years − 0.011667 %·cycles) × (1 − reduction for > 300 cycles/year)."""
    w = cfg.warranty
    att = w.reference_fade_per_year / 100 * years + w.reference_fade_per_cycle / 100 * cycles
    red = 0.0
    if years > 0:
        red = max(0.0, (cycles / years - w.cycles_per_year) / w.cycles_per_year)
    return round(w.reference_base_percent * max(0.0, 1 - att) * max(0.0, 1 - red), 2)


def warranty_status(cfg: Config, scope: str, current: dict | None, tr: dict, cycles: float, today: date) -> dict:
    w = cfg.warranty
    start = cfg.start_date(scope)
    end = date(start.year + int(w.years), start.month, min(start.day, 28)) + timedelta(days=(w.years % 1) * YEAR)
    years = max(years_between(start, today), 0.0)
    cpy = cycles / years if years > 0.08 else None
    out = {"start": start.isoformat(), "end": end.isoformat(), "years_so_far": round(years, 2),
           "full_cycles": round(cycles, 1), "cycles_per_year": round(cpy, 1) if cpy else None,
           "limit_percent": w.min_remaining_percent, "status": "ok", "notes": [],
           "reference_soh": growatt_reference(cfg, years, cycles),
           "start_estimated": (scope == "system" and any(u.commissioned_estimated for u in cfg.units.values()))
           or (scope != "system" and cfg.units[scope].commissioned_estimated)}
    if cpy and cpy > w.cycles_per_year:
        out["notes"].append(L(f"{cpy:.0f} full cycles per year – above {w.cycles_per_year:.0f} the warranty shrinks",
                              f"{cpy:.0f} Vollzyklen pro Jahr – über {w.cycles_per_year:.0f} sinkt die Garantie anteilig"))
    end_eff = end
    if cpy:
        cyc_end = today + timedelta(days=(w.max_full_cycles - cycles) / cpy * YEAR)
        out["cycle_limit_reached"] = cyc_end.isoformat()
        if cyc_end < end:
            out["notes"].append(L(
                f"{w.max_full_cycles:.0f} full cycles expected before the end of the years ({_month(cyc_end)}) – "
                f"the warranty then ends earlier",
                f"{w.max_full_cycles:.0f} Vollzyklen voraussichtlich vor Ablauf der Jahre ({_month(cyc_end)}) – "
                f"das Garantieende kommt dann früher"))
            end_eff = cyc_end
    out["end_effective"] = end_eff.isoformat()
    if today > end_eff:
        out["status"] = "expired"
        return out
    limit = w.min_remaining_percent
    if current and current.get("status") == "ok" and current.get("soh") is not None:
        lo = current["detail"].get("soh_lo") or current["soh"]
        hi = current["detail"].get("soh_hi") or current["soh"]
        if hi < limit:
            out["status"] = "claim"
            out["notes"].append(L(f"capacity certainly below {limit:.0f} % – warranty case, create the dossier",
                                  f"Kapazität sicher unter {limit:.0f} % – Garantiefall, Dossier erstellen"))
        elif lo < limit + w.warn_margin_points:
            out["status"] = "warning"
            out["notes"].append(L(f"capacity approaching the warranty limit ({lo:.0f} % lower bound, limit {limit:.0f} %)",
                                  f"Kapazität nähert sich der Garantiegrenze ({lo:.0f} % Untergrenze, Grenze {limit:.0f} %)"))
    p = tr.get(f"forecast_{int(limit)}") if tr else None
    warn_at = end_eff - timedelta(days=w.warn_months_before_end * 30.44)
    if p and out["status"] == "ok":
        pd = date.fromisoformat(p["date"])
        if pd <= end_eff:
            out["status"] = "warning"
            out["notes"].append(L(f"forecast reaches {limit:.0f} % before the warranty ends ({_month(pd)})",
                                  f"Prognose erreicht {limit:.0f} % vor Garantieende ({_month(pd)})"))
    if today >= warn_at:
        out["notes"].append(L(f"warranty ends {_month(end_eff)} – create and check the warranty dossier now",
                              f"Garantie endet {_month(end_eff)} – jetzt Garantie-Dossier erstellen und prüfen"))
        if out["status"] == "ok":
            out["status"] = "ending"
    return out


def today_local(cfg: Config, ts: float) -> date:
    return datetime.fromtimestamp(ts, cfg.tz).date()
