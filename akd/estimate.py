"""From measurement windows to a capacity with confidence range – or honestly 'no reliable statement'."""
from __future__ import annotations

import math

from .config import Config
from .i18n import L
from .stats import bootstrap, condition_ok, huber_wls_origin, reject_outliers, wmean

OK = "ok"
UNSURE = "uncertain"     # no reliable statement
NODATA = "no_data"


def _combine_ci(est, lo, hi, sys_rel):
    """Combine the statistical range with a possible systematic deviation of the power sensor."""
    s = sys_rel * est
    return est - math.hypot(est - lo, s), est + math.hypot(hi - est, s)


def system_estimate(cfg: Config, wins: list[dict], kind: str = "discharge") -> dict:
    """Weighted mean of the window capacities (weight 1/σ²), bootstrap range, outliers by MAD."""
    est = cfg.est
    cand = [w for w in wins if w["used"] and w["kind"] == kind and w["cap_kwh"]]
    if not cand:
        coarse = [w for w in wins if w["kind"] == kind and w["reason"] == "soc_too_coarse"]
        if coarse:
            st = max(w["deltas"].get("soc_step", 0) for w in coarse)
            return {"status": UNSURE, "n": 0, "detail": {"reason": L(
                f"SOC is only reported in {st:.0f} % steps – too coarse for a reliable capacity",
                f"SOC wird nur in {st:.0f}-%-Stufen gemeldet – zu grob für eine verlässliche Kapazität")}}
        return {"status": NODATA, "n": 0, "detail": {"reason": L("no clean measurement windows",
                                                                 "keine sauberen Messfenster")}}
    caps = [w["cap_kwh"] for w in cand]
    out_idx = reject_outliers(caps)
    keep = [w for i, w in enumerate(cand) if i not in out_idx]
    items = [(w["cap_kwh"], 1 / max(w["sigma_rel"], 1e-3) ** 2) for w in keep]
    n = len(items)
    detail = {"kind": kind, "outliers": [w["id"] for i, w in enumerate(cand) if i in out_idx and "id" in w],
              "sources": sorted({w["source"] for w in keep}), "n_candidates": len(cand)}
    if n == 0:
        return {"status": NODATA, "n": 0, "detail": detail}
    c = wmean([x for x, _ in items], [w for _, w in items])
    # use at least the mean window uncertainty / sqrt(n) as statistical range
    sig_floor = c * math.sqrt(1 / sum(w for _, w in items)) * 1.96 if items else 0
    lo, hi = bootstrap(items, lambda s: wmean([x for x, _ in s], [w for _, w in s]), seed=n * 31 + 7)
    if not math.isfinite(lo):
        lo, hi = c - sig_floor, c + sig_floor
    lo, hi = min(lo, c - sig_floor), max(hi, c + sig_floor)
    lo, hi = _combine_ci(c, lo, hi, est.systematic_relative)
    res = {"cap_kwh": round(c, 4), "lo": round(lo, 4), "hi": round(hi, 4), "n": n, "detail": detail}
    rel = (hi - lo) / 2 / c
    detail["uncertainty_relative"] = round(rel, 4)
    if n < est.min_windows:
        res["status"] = UNSURE
        detail["reason"] = L(f"only {n} clean measurement windows (at least {est.min_windows} needed)",
                             f"nur {n} saubere Messfenster (mindestens {est.min_windows} nötig)")
    elif rel > est.max_uncertainty_relative:
        res["status"] = UNSURE
        detail["reason"] = L(f"range too wide (±{rel * 100:.1f} %, allowed ±{est.max_uncertainty_relative * 100:.0f} %)",
                             f"Bereich zu breit (±{rel * 100:.1f} %, erlaubt ±{est.max_uncertainty_relative * 100:.0f} %)")
    else:
        res["status"] = OK
    return res


def unit_estimates(cfg: Config, wins: list[dict], eff: float | None = None) -> dict[str, dict]:
    """Capacity per unit although power is only measured for the whole system:
    E_window ≈ Σ_i k_i · ΔSOC_i  (k_i = Wh per percentage point of unit i), solved robustly over many windows.
    Only identifiable if the units move by different amounts of percent in the windows.
    Discharge windows count directly (usable energy). Charge windows are converted to the discharge side with the
    measured system efficiency (discharge/charge side) – only if that is reliably known."""
    units = list(cfg.units)
    est = cfg.est
    if len(units) == 1:
        return {units[0]: system_estimate(cfg, wins, "discharge")}
    cand = [w for w in wins if w["used"] and w["source"] == "minute"
            and (w["kind"] == "discharge" or eff) and w["deltas"].get("e_wh_units")
            and all(w["deltas"].get(u) not in (None, 0) for u in units)]
    res = {}
    if len(cand) < max(est.min_windows, len(units) + 2):
        for u in units:
            res[u] = {"status": UNSURE if cand else NODATA, "n": len(cand), "detail": {"reason": L(
                f"only {len(cand)} suitable windows with values of all units",
                f"nur {len(cand)} geeignete Fenster mit Werten aller Einheiten")}}
        return res
    rows = [[w["deltas"][u] * (1 if w["kind"] == "charge" else -1) for u in units] for w in cand]
    ys = [w["deltas"]["e_wh_units"] * (eff if w["kind"] == "charge" else 1.0) for w in cand]
    ws = [1 / (y * max(w["sigma_rel"], 0.01)) ** 2 for y, w in zip(ys, cand)]
    if not condition_ok(rows, ws):
        for u in units:
            res[u] = {"status": UNSURE, "n": len(cand), "detail": {"reason": L(
                "the units move almost identically in all windows – split not identifiable",
                "Einheiten bewegen sich in allen Fenstern fast gleich – Aufteilung nicht bestimmbar")}}
        return res
    try:
        k = huber_wls_origin(rows, ys, ws)
    except ValueError:
        for u in units:
            res[u] = {"status": UNSURE, "n": len(cand), "detail": {"reason": L("equation system not solvable",
                                                                               "Gleichungssystem nicht lösbar")}}
        return res
    items = list(zip(rows, ys, ws))

    def stat_for(j):
        def f(sample):
            return huber_wls_origin([r for r, _, _ in sample], [y for _, y, _ in sample],
                                    [w for _, _, w in sample], iters=5)[j] * 100 / 1000
        return f

    for j, u in enumerate(units):
        c = k[j] * 100 / 1000
        lo, hi = bootstrap(items, stat_for(j), b=400, seed=11 + j)
        if not math.isfinite(lo) or c <= 0:
            res[u] = {"status": UNSURE, "n": len(cand), "cap_kwh": round(c, 4) if c > 0 else None,
                      "detail": {"reason": L("range not determinable", "Bereich nicht bestimmbar")}}
            continue
        lo, hi = _combine_ci(c, min(lo, c), max(hi, c), est.systematic_relative)
        rel = (hi - lo) / 2 / c
        r = {"cap_kwh": round(c, 4), "lo": round(lo, 4), "hi": round(hi, 4), "n": len(cand),
             "detail": {"kind": "discharge" + (" + charge×efficiency" if eff else ""),
                        "method": "regression over windows", "n_discharge": sum(w["kind"] == "discharge" for w in cand),
                        "uncertainty_relative": round(rel, 4)}}
        nominal = cfg.units[u].nominal_kwh
        if not 0.3 * nominal <= c <= 1.6 * nominal:
            r["status"] = UNSURE
            r["detail"]["reason"] = L("implausible result", "Ergebnis unplausibel")
        elif rel > est.max_uncertainty_relative * 1.5:
            r["status"] = UNSURE
            r["detail"]["reason"] = L(
                f"range too wide (±{rel * 100:.1f} %) – the units move too similarly in the windows to split the "
                f"jointly measured energy reliably",
                f"Bereich zu breit (±{rel * 100:.1f} %) – die Einheiten bewegen sich in den Fenstern zu ähnlich, um "
                f"die gemeinsam gemessene Energie sicher aufzuteilen")
        else:
            r["status"] = OK
        res[u] = r
    return res


def add_soh(cfg: Config, scope: str, r: dict) -> dict:
    if r.get("cap_kwh"):
        nom = cfg.nominal(scope)
        r["soh"] = round(r["cap_kwh"] / nom * 100, 2)
        r.setdefault("detail", {})["soh_lo"] = round(r["lo"] / nom * 100, 2) if r.get("lo") else None
        r["detail"]["soh_hi"] = round(r["hi"] / nom * 100, 2) if r.get("hi") else None
    return r


def estimate_all(cfg: Config, wins: list[dict]) -> dict[str, dict]:
    """For one period: system (discharge side = usable), charge side as cross-check, efficiency, units."""
    out = {}
    sys_d = system_estimate(cfg, wins, "discharge")
    sys_c = system_estimate(cfg, wins, "charge")
    if sys_d.get("cap_kwh") and sys_c.get("cap_kwh") and sys_d["status"] == OK and sys_c["status"] == OK:
        sys_d["eff"] = round(sys_d["cap_kwh"] / sys_c["cap_kwh"], 4)
    sys_d.setdefault("detail", {})["charge_side"] = {k: sys_c.get(k) for k in ("status", "cap_kwh", "lo", "hi", "n")}
    out["system"] = add_soh(cfg, "system", sys_d)
    eff = sys_d.get("eff")
    for u, r in unit_estimates(cfg, wins, eff).items():
        out[u] = add_soh(cfg, u, r)
    return out
