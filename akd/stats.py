"""Small statistics without numpy: weighted mean, bootstrap, Theil-Sen, permutation test, small linear systems."""
from __future__ import annotations

import math
import random
from statistics import median


def wmean(xs, ws):
    s = sum(ws)
    return sum(x * w for x, w in zip(xs, ws)) / s if s > 0 else float("nan")


def mad(xs):
    m = median(xs)
    return median([abs(x - m) for x in xs]) * 1.4826


def reject_outliers(xs, k=3.5):
    """Indices of outliers by the MAD rule (robust). With fewer than 4 values nothing is rejected."""
    if len(xs) < 4:
        return set()
    m, s = median(xs), mad(xs)
    if s <= 0:
        s = max(abs(m) * 0.01, 1e-9)
    return {i for i, x in enumerate(xs) if abs(x - m) > k * s}


def percentile(sorted_xs, q):
    if not sorted_xs:
        return float("nan")
    pos = (len(sorted_xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_xs[lo]
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def bootstrap(items, stat, b=1000, seed=1, alpha=0.05):
    """Percentile bootstrap: (lo, hi) of the statistic by resampling with replacement."""
    if len(items) < 2:
        return float("nan"), float("nan")
    rnd = random.Random(seed)
    n = len(items)
    vals = []
    for _ in range(b):
        sample = [items[rnd.randrange(n)] for _ in range(n)]
        try:
            v = stat(sample)
        except (ZeroDivisionError, ValueError, ArithmeticError):
            continue
        if v is not None and math.isfinite(v):
            vals.append(v)
    if len(vals) < b * 0.5:
        return float("nan"), float("nan")
    vals.sort()
    return percentile(vals, alpha / 2), percentile(vals, 1 - alpha / 2)


def theil_sen(xs, ys):
    """Robust line: median of the pairwise slopes, intercept as median of the residuals."""
    slopes = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs))
              if xs[j] != xs[i]]
    if not slopes:
        raise ValueError("zu wenige Punkte")
    m = median(slopes)
    b = median([y - m * x for x, y in zip(xs, ys)])
    return m, b


def permutation_p(a, b, n=4000, seed=7):
    """Two-sided permutation test on the difference of the means."""
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    obs = abs(sum(a) / len(a) - sum(b) / len(b))
    pool = list(a) + list(b)
    rnd = random.Random(seed)
    hit = 0
    for _ in range(n):
        rnd.shuffle(pool)
        x, y = pool[:len(a)], pool[len(a):]
        if abs(sum(x) / len(x) - sum(y) / len(y)) >= obs - 1e-12:
            hit += 1
    return (hit + 1) / (n + 1)


def solve(a, b):
    """Gaussian elimination for small systems a·x = b. Raises ValueError for (nearly) singular matrices."""
    n = len(b)
    m = [list(map(float, row)) + [float(v)] for row, v in zip(a, b)]
    scale = max(abs(x) for row in a for x in row) or 1.0
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-10 * scale:
            raise ValueError("singular")
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def wls_origin(rows, ys, ws):
    """Weighted least squares without intercept: y ≈ Σ c_j·x_j."""
    k = len(rows[0])
    ata = [[sum(w * r[i] * r[j] for r, w in zip(rows, ws)) for j in range(k)] for i in range(k)]
    aty = [sum(w * r[i] * y for r, y, w in zip(rows, ys, ws)) for i in range(k)]
    return solve(ata, aty)


def condition_ok(rows, ws, min_ratio=1e-4):
    """Checks whether the columns are independent enough (smallest/largest eigenvalue of the 2x2 normal matrix)."""
    if len(rows[0]) != 2:
        return True
    a = sum(w * r[0] * r[0] for r, w in zip(rows, ws))
    b = sum(w * r[0] * r[1] for r, w in zip(rows, ws))
    d = sum(w * r[1] * r[1] for r, w in zip(rows, ws))
    tr, det = a + d, a * d - b * b
    disc = math.sqrt(max(tr * tr / 4 - det, 0))
    lmax, lmin = tr / 2 + disc, tr / 2 - disc
    return lmax > 0 and lmin / lmax >= min_ratio


def huber_wls_origin(rows, ys, ws, iters=20, k=1.5):
    """Robust variant (Huber IRLS) on relative residuals."""
    c = wls_origin(rows, ys, ws)
    for _ in range(iters):
        res = [(y - sum(ci * x for ci, x in zip(c, r))) / y for r, y in zip(rows, ys)]
        s = mad(res) or 1e-6
        hw = [1.0 if abs(e) <= k * s else k * s / abs(e) for e in res]
        c_new = wls_origin(rows, ys, [w * h for w, h in zip(ws, hw)])
        if max(abs(a - b) for a, b in zip(c, c_new)) < 1e-9 * max(abs(x) for x in c_new):
            c = c_new
            break
        c = c_new
    return c
