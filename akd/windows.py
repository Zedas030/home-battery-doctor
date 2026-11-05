"""Find clean charge and discharge windows and compute the capacity of each window.

A window is a contiguous period with charging only OR discharging only. It starts and ends exactly at a step change
of the total SOC (edge alignment): the SOC is then known at both edges and the rounding error of whole percent
largely cancels out.
"""
from __future__ import annotations

import math

from .config import Config
from .grid import MIN, Grid, edge_points, interp

SIGMA_EDGE = 1 / math.sqrt(12)   # uncertainty of the step position per edge (percentage points)
CHARGE, DISCHARGE = "charge", "discharge"


def _segments(cfg: Config, g: Grid, kind: str):
    """Maximal stretches with a single flow direction; short gaps (<= max_gap) are bridged."""
    p = g.get("ch" if kind == CHARGE else "dis")
    q = g.get("dis" if kind == CHARGE else "ch")
    soc = g.get("sys_soc")
    max_gap = int(cfg.est.max_gap // MIN)
    segs, cur, gap = [], [], 0
    for i in range(g.n):
        if p[i] is None or q[i] is None or soc[i] is None:
            if cur:
                gap += 1
                if gap > max_gap:
                    segs.append(cur)
                    cur, gap = [], 0
            continue
        if q[i] > 5:  # opposite direction -> end
            if cur:
                segs.append(cur)
            cur, gap = [], 0
            continue
        cur.append(i)
        gap = 0
    if cur:
        segs.append(cur)
    return segs


def find_windows(cfg: Config, g: Grid, scope: str = "system") -> list[dict]:
    out = []
    est = cfg.est
    soc = g.get("sys_soc")
    temps = [g.get(f"{u}_temp") for u in cfg.units]
    usoc = {u: g.get(f"{u}_soc") for u in cfg.units}
    for kind in (CHARGE, DISCHARGE):
        p = g.get("ch" if kind == CHARGE else "dis")
        sign = 1 if kind == CHARGE else -1
        for idx in _segments(cfg, g, kind):
            if len(idx) < 2:
                continue
            # Jumps (calibration, a unit waking up) split the stretch
            parts, cur = [], [idx[0]]
            diffs = [abs(soc[b] - soc[a]) for a, b in zip(idx, idx[1:]) if soc[b] != soc[a]]
            jump = max(est.max_soc_jump_percent, min(diffs) if diffs else 0)
            for a, b in zip(idx, idx[1:]):
                if abs(soc[b] - soc[a]) > jump:
                    parts.append((cur, True))
                    cur = [b]
                else:
                    cur.append(b)
            parts.append((cur, False))
            for part, jumped in parts:
                w = _evaluate(cfg, g, part, kind, sign, p, soc, temps, usoc, scope)
                if w:
                    if jumped and w["used"] is False and not w["reason"]:
                        w["reason"] = "soc_jump"
                    out.append(w)
    return out


def _evaluate(cfg, g, part, kind, sign, p, soc, temps, usoc, scope):
    est = cfg.est
    lo_band, hi_band = est.soc_band
    times = [g.ts(i) for i in part]
    vals = [soc[i] for i in part]
    # step edges inside the band
    edges = [(k, i) for k, i in enumerate(part[1:], start=1) if soc[i] != soc[part[k - 1]]
             and lo_band <= soc[i] <= hi_band]
    if len(edges) < 2:
        return None
    ka, ia = edges[0]
    kb, ib = edges[-1]
    s0, s1 = soc[ia], soc[ib]
    swing = (s1 - s0) * sign
    if kb <= ka:
        return None
    t0, t1 = g.ts(ia), g.ts(ib)
    # Energy: power held per minute; bridged gaps use the last value
    e_wh, gaps = 0.0, 0
    for a, b in zip(part[ka:kb], part[ka + 1:kb + 1]):
        e_wh += p[a] * (b - a) * MIN / 3600
        gaps += (b - a) - 1
    dur = t1 - t0
    p_mean = e_wh / (dur / 3600) if dur > 0 else 0
    tvals = [t[i] for t in temps for i in part[ka:kb + 1] if t[i] is not None]
    temp = sum(tvals) / len(tvals) if tvals else None
    # Units: a sleeping unit only stops reporting its own values; the total SOC stays valid. Splitting the energy
    # between units needs values of all units, so use the longest stretch in which all units report, again
    # aligned to edges of the total SOC, with its own energy.
    deltas = {}
    allv = [all(usoc[u][part[k]] is not None for u in usoc) for k in range(ka, kb + 1)]
    best, run_start = (0, -1, -1), None
    for j, ok in enumerate(allv + [False]):
        if ok and run_start is None:
            run_start = j
        elif not ok and run_start is not None:
            if j - run_start > best[0]:
                best = (j - run_start, run_start + ka, j - 1 + ka)
            run_start = None
    sub_edges = [k for k, _ in edges if best[1] <= k <= best[2]]
    if usoc and len(sub_edges) >= 2 and \
            abs(soc[part[sub_edges[-1]]] - soc[part[sub_edges[0]]]) >= est.min_swing_percent / 2:
        sa, sb = sub_edges[0], sub_edges[-1]
        ta, tb = g.ts(part[sa]), g.ts(part[sb])
        e_sub = sum(p[a] * (b - a) * MIN / 3600 for a, b in zip(part[sa:sb], part[sa + 1:sb + 1]))
        for u, arr in usoc.items():
            pts = edge_points(times, [arr[i] for i in part])
            a, b = interp(pts, ta), interp(pts, tb)
            if a is None:
                a = arr[part[sa]]   # no edge before: use the step value (slightly less precise)
            if b is None:
                b = arr[part[sb]]
            deltas[u] = round(b - a, 3) if a is not None and b is not None else None
        deltas["e_wh_units"] = round(e_sub, 2)
        deltas["sys_swing_units"] = abs(soc[part[sb]] - soc[part[sa]])
        deltas["partial"] = (sa, sb) != (ka, kb)
    else:
        for u in usoc:
            deltas[u] = None
    reason = ""
    cnt = g.data.get("count")
    if cnt is not None:
        vals_c = [cnt[i] for i in part[ka:kb + 1]]
        bad = sum(1 for v in vals_c if v is not None and v != len(usoc))
        missing_c = sum(1 for v in vals_c if v is None)
        if bad or missing_c > 0.1 * len(vals_c):
            reason = "battery_count"
    if not reason:
        if swing <= 0:
            reason = "soc_wrong_direction"
        elif swing < est.min_swing_percent:
            reason = "swing_too_small"
        elif dur < est.min_duration:
            reason = "too_short"
        elif p_mean < est.min_power_w:
            reason = "power_too_low"
        elif temp is not None and not (est.temp_range[0] <= temp <= est.temp_range[1]):
            reason = "temperature"
        elif _non_monotonic(vals[ka:kb + 1], sign):
            reason = "soc_reversal"
    cap = e_wh / abs(s1 - s0) * 100 / 1000 if s1 != s0 else None
    nominal = cfg.nominal("system")
    if not reason and cap is not None and not (0.3 * nominal <= cap <= 1.6 * nominal):
        reason = "implausible"
    # Step height of the reported SOC (normally 1 %; coarser steps = larger uncertainty)
    steps = [abs(soc[part[k]] - soc[part[k - 1]]) for k, _ in edges]
    step = max(1.0, min(steps)) if steps else 1.0
    if not reason and step > est.max_soc_step_percent:
        reason = "soc_too_coarse"
    sigma_rel = math.sqrt((math.sqrt(2) * SIGMA_EDGE * step / max(abs(s1 - s0), 1)) ** 2
                          + est.power_error_relative ** 2 + (gaps / max(1, kb - ka)) ** 2)
    deltas.update({"gap_min": gaps, "p_mean_w": round(p_mean, 1), "soc_step": step})
    return {"scope": scope, "kind": kind, "t0": t0, "t1": t1, "source": "minute", "soc0": s0, "soc1": s1,
            "e_wh": round(e_wh, 2), "cap_kwh": round(cap, 4) if cap else None, "sigma_rel": round(sigma_rel, 5),
            "temp": round(temp, 2) if temp is not None else None, "deltas": deltas, "used": not reason,
            "reason": reason}


def _non_monotonic(vals, sign):
    """More than 1 % backwards against the flow direction = not clean."""
    best = vals[0]
    for v in vals:
        if (v - best) * sign > 0:
            best = v
        elif (best - v) * sign > 1:
            return True
    return False


def hourly_windows(cfg: Config, hours: dict[str, dict[int, tuple]], before_ts: float | None) -> list[dict]:
    """Coarse windows from HA long-term statistics (mean/min/max per hour), only for times without minute data.

    ΔSOC = hourly mean (last) − hourly mean (first); energy = half of the edge hours + full hours in between.
    The uncertainty is much larger (σ 1.5 percentage points per edge, 4 % energy) and marked as such.
    """
    est = cfg.est
    soc = hours.get("sys_soc", {})
    out = []
    ts_all = sorted(t for t in soc if before_ts is None or t + 3600 <= before_ts)
    for kind in (CHARGE, DISCHARGE):
        pk, qk = ("ch", "dis") if kind == CHARGE else ("dis", "ch")
        sign = 1 if kind == CHARGE else -1
        run = []

        def flush(run=run, kind=kind, sign=sign, pk=pk):
            if len(run) >= 3:
                s0, s1 = soc[run[0]][0], soc[run[-1]][0]
                e = 0.5 * hours[pk][run[0]][0] + 0.5 * hours[pk][run[-1]][0] + sum(hours[pk][t][0] for t in run[1:-1])
                swing = (s1 - s0) * sign
                awake = all((hours.get(f"{u}_soc", {}).get(t, (None, 1, None))[1] or 0) > 0 or
                            not cfg.units[u].sleeps_with_zero or f"{u}_soc" not in hours
                            for u in cfg.units for t in run)
                temps = [hours[f"{u}_temp"][t][0] for u in cfg.units for t in run
                         if t in hours.get(f"{u}_temp", {}) and hours[f"{u}_temp"][t][0] is not None]
                temp = sum(temps) / len(temps) if temps else None
                reason = ""
                n_units = len(cfg.units)
                cnt = hours.get("count")
                if cnt and any(not (cnt.get(t) and cnt[t][1] == n_units and cnt[t][2] == n_units) for t in run):
                    reason = "battery_count"
                elif swing < est.min_swing_percent:
                    reason = "swing_too_small"
                elif not awake:
                    reason = "unit_missing"
                elif not (est.soc_band[0] <= min(s0, s1) and max(s0, s1) <= est.soc_band[1]):
                    reason = "soc_band"
                elif temp is not None and not (est.temp_range[0] <= temp <= est.temp_range[1]):
                    reason = "temperature"
                cap = e / abs(s1 - s0) * 100 / 1000 if s1 != s0 else None
                nominal = cfg.nominal("system")
                if not reason and cap is not None and not (0.3 * nominal <= cap <= 1.6 * nominal):
                    reason = "implausible"
                sig = math.sqrt((math.sqrt(2) * 1.5 / max(abs(s1 - s0), 1)) ** 2 + 0.04 ** 2)
                if swing > 0:
                    out.append({"scope": "system", "kind": kind, "t0": run[0] + 1800, "t1": run[-1] + 1800,
                                "source": "hourly", "soc0": round(s0, 2), "soc1": round(s1, 2), "e_wh": round(e, 1),
                                "cap_kwh": round(cap, 4) if cap else None, "sigma_rel": round(sig, 5),
                                "temp": round(temp, 2) if temp is not None else None, "deltas": {},
                                "used": not reason, "reason": reason})
            run.clear()

        prev = None
        for t in ts_all:
            p = hours.get(pk, {}).get(t)
            q = hours.get(qk, {}).get(t)
            ok = (p and q and p[0] is not None and p[0] >= est.min_power_w and q[2] is not None and q[2] < 5
                  and soc[t][0] is not None)
            if ok and (prev is None or t - prev == 3600):
                run.append(t)
            else:
                flush()
                if ok:
                    run.append(t)
            prev = t if ok else None
        flush()
    return out
