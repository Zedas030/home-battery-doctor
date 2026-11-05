"""Master/slave: detect the re-plug date, before/after comparison with confidence, worsening over weeks."""
from __future__ import annotations

import random
from datetime import date, timedelta

from .config import Config
from .i18n import L, fmt_date, num
from .stats import bootstrap, permutation_p


def metrics() -> dict:
    return {
        "ms_diff_abs": (L("SOC difference (absolute, daily mean)", "SOC-Differenz (Betrag, Tagesmittel)"),
                        L("percentage points", "Prozentpunkte")),
        "ms_wake_delay_min": (L("Wake-up delay of the slave", "Aufwachverzug des Slaves"), L("minutes", "Minuten")),
        "ms_throttle_min": (L("Throttling by the SOC automation", "Drosselung durch die SOC-Automation"),
                            L("minutes per day", "Minuten pro Tag")),
    }


def usable_days(daily: dict[str, dict], min_cover=0.5) -> dict[date, dict]:
    out = {}
    for d, v in daily.items():
        if v.get("source") == "minute" and v.get("ms_coverage", 0) >= min_cover and v.get("ms_diff_abs") is not None:
            out[date.fromisoformat(d)] = v
    return out


def _best_split(ys, k_min):
    """Index k and reduction of the sum of squared errors by the best split into two means."""
    n = len(ys)
    tot = sum(ys)
    m = tot / n
    sse0 = sum((y - m) ** 2 for y in ys)
    best_k, best_sse = None, None
    pre = pre2 = 0.0
    tot2 = sum(y * y for y in ys)
    for i, y in enumerate(ys, start=1):
        pre += y
        pre2 += y * y
        if k_min <= i <= n - k_min:
            sse = (pre2 - pre * pre / i) + ((tot2 - pre2) - (tot - pre) ** 2 / (n - i))
            if best_sse is None or sse < best_sse:
                best_k, best_sse = i, sse
    return best_k, sse0 - best_sse


def detect_changepoint(cfg: Config, daily: dict[str, dict]) -> dict:
    """Day from which the mean SOC difference changes most clearly (two means, least squares) within the configured
    search range; with a permutation test against chance."""
    ms = cfg.ms
    days = {d: v for d, v in usable_days(daily).items() if ms.search_range[0] <= d <= ms.search_range[1]}
    ds = sorted(days)
    k_min = 3
    if len(ds) < 2 * k_min:
        return {"status": "too_few_days", "days": len(ds),
                "text": L("not enough days with data yet", "noch zu wenige Tage mit Daten")}
    ys = [days[d]["ms_diff_abs"] for d in ds]
    k, gain = _best_split(ys, k_min)
    # permutation test WITH a new search for the best split in each permutation (otherwise p would be optimistic)
    rnd = random.Random(5)
    perm, hits, n_perm = list(ys), 0, 999
    for _ in range(n_perm):
        rnd.shuffle(perm)
        if _best_split(perm, k_min)[1] >= gain - 1e-12:
            hits += 1
    p = (hits + 1) / (n_perm + 1)
    last_before, first_after = ds[k - 1], ds[k]
    out = {"status": "ok" if p < 0.05 else "no_clear_break", "date": first_after.isoformat(),
           "last_day_before": last_before.isoformat(), "p": round(p, 4), "days": len(ds)}
    gap = (first_after - last_before).days - 1
    if gap > 0:
        out["gap_days"] = gap
        out["text"] = L(f"break between {fmt_date(last_before)} and {fmt_date(first_after)} "
                        f"({gap} days without usable data in between)",
                        f"Bruch zwischen {fmt_date(last_before)} und {fmt_date(first_after)} "
                        f"({gap} Tage ohne brauchbare Daten dazwischen)")
    else:
        out["text"] = L(f"break from {fmt_date(first_after)}", f"Bruch ab {fmt_date(first_after)}")
    return out


def _diff_stats(a: list[float], b: list[float], seed: int):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    items = [(0, x) for x in a] + [(1, x) for x in b]

    def stat(s):
        xa = [x for g, x in s if g == 0]
        xb = [x for g, x in s if g == 1]
        if not xa or not xb:
            raise ValueError
        return sum(xb) / len(xb) - sum(xa) / len(xa)
    lo, hi = bootstrap(items, stat, b=1000, seed=seed)
    return {"before": round(ma, 2), "after": round(mb, 2), "difference": round(mb - ma, 2),
            "range": [round(lo, 2), round(hi, 2)], "p": round(permutation_p(a, b, n=3000, seed=seed), 4),
            "n_before": len(a), "n_after": len(b)}


def before_after(cfg: Config, daily: dict[str, dict], switch: date) -> dict:
    ms = cfg.ms
    days = usable_days(daily)
    before = {d: v for d, v in days.items() if switch - timedelta(days=ms.compare_days) <= d < switch}
    after = {d: v for d, v in days.items() if switch <= d < switch + timedelta(days=ms.compare_days)}
    out = {"date": switch.isoformat(), "days_before": len(before), "days_after": len(after), "metrics": {}}
    for i, (key, (label, unit)) in enumerate(metrics().items()):
        a = [v[key] for v in before.values() if v.get(key) is not None]
        b = [v[key] for v in after.values() if v.get(key) is not None]
        if len(a) < 2 or len(b) < 2:
            out["metrics"][key] = {"name": label, "unit": unit, "status": "too_few_days",
                                   "n_before": len(a), "n_after": len(b)}
            continue
        r = _diff_stats(a, b, seed=100 + i)
        r.update({"name": label, "unit": unit})
        r["status"] = "preliminary" if min(len(a), len(b)) < ms.min_days_per_side else (
            "significant" if r["p"] < 0.05 and (r["range"][0] > 0 or r["range"][1] < 0) else "not_significant")
        out["metrics"][key] = r
    # season: split days by solar yield into classes and compare only within the same class
    sol = sorted(v["e_solar_wh"] for v in list(before.values()) + list(after.values()) if v.get("e_solar_wh") is not None)
    if len(sol) >= 6 and before and after:
        t1, t2 = sol[len(sol) // 3], sol[2 * len(sol) // 3]

        def cls(v):
            e = v.get("e_solar_wh") or 0
            return 0 if e < t1 else 1 if e < t2 else 2
        diffs, weights = [], []
        for c in range(3):
            a = [v["ms_diff_abs"] for v in before.values() if cls(v) == c]
            b = [v["ms_diff_abs"] for v in after.values() if cls(v) == c]
            if a and b:
                diffs.append(sum(b) / len(b) - sum(a) / len(a))
                weights.append(min(len(a), len(b)))
        if diffs:
            out["season_adjusted_difference"] = round(sum(d * w for d, w in zip(diffs, weights)) / sum(weights), 2)
            out["season_classes"] = len(diffs)
    k = out["metrics"].get("ms_diff_abs", {})
    if k.get("status") in ("too_few_days", "preliminary"):
        out["summary"] = L(f"Not enough days after the re-plug yet ({out['days_after']}, needed {ms.min_days_per_side}) "
                           f"– result preliminary.",
                           f"Noch zu wenig Tage nach dem Umstecken ({out['days_after']}, nötig {ms.min_days_per_side}) "
                           f"– Ergebnis vorläufig.")
    elif k.get("status") == "significant":
        better = k["difference"] < 0
        rng = f"{num(k['range'][0], 1)} … {num(k['range'][1], 1)}"
        out["summary"] = L(
            f"The SOC difference has measurably {'improved' if better else 'worsened'}: {num(k['before'], 1)} → "
            f"{num(k['after'], 1)} percentage points (95 % range of the change {rng}, p = {num(k['p'], 3)}).",
            f"Die SOC-Differenz hat sich messbar {'verbessert' if better else 'verschlechtert'}: {num(k['before'], 1)} → "
            f"{num(k['after'], 1)} Prozentpunkte (95-%-Bereich der Änderung {rng}, p = {num(k['p'], 3)}).")
    else:
        out["summary"] = L("No measurable change of the SOC difference (within the range of chance).",
                           "Keine messbare Änderung der SOC-Differenz (Unterschied im Rahmen des Zufalls).")
    return out


def worsening(cfg: Config, daily: dict[str, dict], today: date, since: date | None) -> dict | None:
    """The last N days against the 2N days before (only days after the re-plug)."""
    ms = cfg.ms
    days = usable_days(daily)
    n = ms.worsening_days
    recent = [v["ms_diff_abs"] for d, v in days.items() if today - timedelta(days=n) <= d < today]
    prev = [v["ms_diff_abs"] for d, v in days.items() if today - timedelta(days=3 * n) <= d < today - timedelta(days=n)
            and (since is None or d >= since)]
    if len(recent) < n // 2 or len(prev) < n // 2:
        return None
    mr, mp = sum(recent) / len(recent), sum(prev) / len(prev)
    p = permutation_p(prev, recent, n=2000)
    return {"now": round(mr, 2), "before": round(mp, 2), "p": round(p, 4),
            "worse": mr - mp >= ms.worsening_points and p < 0.05}
