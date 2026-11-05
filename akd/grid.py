"""Minute grid from the raw values: hold short gaps, hide sleep zeros, detect frozen cloud values."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .store import Store

MIN = 60
# How long a value is held without a new reading (s). noah-mqtt sends battery details only every 5 min.
HOLD = {"soc": 11 * MIN, "temp": 20 * MIN, "throttle": 30 * MIN, "default": 4 * MIN}
POWER_KEYS = ("ch", "dis", "sol", "out")
STALE_RUN = 20  # minutes in which ALL power values stay exactly the same (see _blank_stale)


def _hold(key: str) -> float:
    for k, v in HOLD.items():
        if key.endswith(k):
            return v
    return HOLD["default"]


@dataclass
class Grid:
    t0: int
    n: int
    data: dict[str, list]
    tz: object = None

    def ts(self, i: int) -> int:
        return self.t0 + i * MIN

    def get(self, key: str) -> list:
        return self.data.get(key) or [None] * self.n


def build(store: Store, cfg: Config, t0: float, t1: float, keys=None) -> Grid:
    t0 = int(t0) // MIN * MIN
    t1 = int(t1) // MIN * MIN
    n = max(0, (t1 - t0) // MIN)
    keys = keys or list(cfg.entity_roles())
    data = {}
    for k in keys:
        hold = _hold(k)
        rows = store.series(k, t0 - hold, t1)
        arr = [None] * n
        j = 0
        last_ts, last_v = None, None
        for i in range(n):
            t = t0 + i * MIN
            while j < len(rows) and rows[j][0] <= t:
                last_ts, last_v = rows[j]
                j += 1
            if last_ts is not None and t - last_ts <= hold:
                arr[i] = last_v
        data[k] = arr
    # Impossible temperatures (error values such as -273 °C) are not readings
    for k in data:
        if k.endswith("_temp"):
            data[k] = [v if v is None or -40 <= v <= 90 else None for v in data[k]]
    # Offline/asleep units report 0 % and 0 °C at the same time: that 0 °C is a placeholder, not a reading
    for uk in cfg.units:
        s_arr, t_arr = data.get(f"{uk}_soc"), data.get(f"{uk}_temp")
        if s_arr and t_arr:
            data[f"{uk}_temp"] = [None if (t == 0 and s_arr[i] == 0) else t for i, t in enumerate(t_arr)]
    # A sleeping unit reports 0 % -> no data
    for uk, u in cfg.units.items():
        if u.sleeps_with_zero and f"{uk}_soc" in data:
            data[f"{uk}_soc"] = [None if v == 0 else v for v in data[f"{uk}_soc"]]
    _blank_stale(data, n, cfg.nominal_total_kwh)
    return Grid(t0, n, data, cfg.tz)


def _blank_stale(data: dict, n: int, nominal_kwh: float):
    """The cloud delivers nothing new while HA/Prometheus keep showing the old value. Recognisable when all power
    values stay exactly the same for a long time AND the SOC does not move although the shown power should have
    changed it by at least 2 percentage points. (Constant power alone is not enough: at night with a fixed output
    setpoint the storage really reports identical values, but the SOC goes down.)"""
    pk = [k for k in POWER_KEYS if k in data]
    if len(pk) < 2:
        return
    soc = data.get("sys_soc")
    start = 0
    for i in range(1, n + 1):
        same = i < n and all(data[k][i] is not None and data[k][i] == data[k][i - 1] for k in pk) and             (soc is None or soc[i] == soc[i - 1])
        if not same:
            run = i - start
            if run >= STALE_RUN:
                flow = abs((data.get("ch") or [0] * n)[start] or 0) + abs((data.get("dis") or [0] * n)[start] or 0)
                expected = flow * run / 60 / (nominal_kwh * 1000) * 100
                if expected >= 2:
                    for k in list(data):
                        for j in range(start, i):
                            data[k][j] = None
            start = i


def edge_points(times: list[int], vals: list) -> list[tuple[int, float]]:
    """Times at which a stepped value (whole percent) changes: (t, new value)."""
    out = []
    prev = None
    for t, v in zip(times, vals):
        if v is None:
            prev = None
            continue
        if prev is not None and v != prev:
            out.append((t, float(v)))
        prev = v
    return out


def interp(points: list[tuple[int, float]], t: float):
    """SOC interpolated linearly between step edges; None outside the edges."""
    if not points or t < points[0][0] or t > points[-1][0]:
        return None
    lo, hi = 0, len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    (ta, va), (tb, vb) = points[lo], points[hi]
    if tb == ta:
        return va
    return va + (vb - va) * (t - ta) / (tb - ta)
