"""Synthetic batteries with known capacity – for tests, acceptance and demo.

Two (or more) units, sunny days with clouds, evening load, charge and discharge efficiencies, whole percent,
battery details only every 5 min (like many cloud integrations), a sleeping slave reporting 0 %, artificial ageing.
True usable capacity (discharge side) = capacity × discharge efficiency.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass
class SimUnit:
    key: str
    cap_wh: float
    aging_per_year: float = 0.0   # relative loss per year, e.g. 0.04
    sleeps: bool = False
    share_bias: float = 0.0       # gets more (+) or less (−) charging power


@dataclass
class Sim:
    units: list[SimUnit]
    start: date = date(2026, 4, 1)
    days: int = 30
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Berlin"))
    eta_c: float = 0.97
    eta_d: float = 0.97
    seed: int = 1
    soc_step: float = 1.0          # resolution of the reported SOC
    unit_refresh_min: int = 5
    noise: float = 0.03
    temp: float = 22.0
    discharge_limit: float = 10.0
    gaps: list[tuple[datetime, int]] = field(default_factory=list)  # (start, minutes) without data
    jumps: list[tuple[datetime, float]] = field(default_factory=list)  # (time, new SOC of all units)
    imbalance_days: dict = field(default_factory=dict)  # day -> additional deviation of the slave share

    def cap_at(self, u: SimUnit, d: date) -> float:
        years = (d - self.start).days / 365.25
        return u.cap_wh * (1 - u.aging_per_year * years)

    def true_usable_kwh(self, d: date, key: str | None = None) -> float:
        us = [u for u in self.units if key in (None, u.key)]
        return sum(self.cap_at(u, d) for u in us) * self.eta_d / 1000

    def run(self) -> dict[str, list[tuple[float, float]]]:
        rnd = random.Random(self.seed)
        out: dict[str, list] = {k: [] for k in ("sys_soc", "ch", "dis", "sol", "out")}
        for u in self.units:
            out[f"{u.key}_soc"] = []
            out[f"{u.key}_temp"] = []
        energy = {u.key: self.cap_at(u, self.start) * 0.5 for u in self.units}
        reported = {u.key: None for u in self.units}
        gap_set = set()
        for g0, mins in self.gaps:
            t = int(g0.timestamp()) // 60 * 60
            gap_set.update(range(t, t + mins * 60, 60))
        jumps = {int(t.timestamp()) // 60 * 60: v for t, v in self.jumps}
        for day in range(self.days):
            d = self.start + timedelta(days=day)
            caps = {u.key: self.cap_at(u, d) for u in self.units}
            peak = rnd.uniform(250, 950)
            clouds = rnd.uniform(0.0, 0.5)
            load_night = rnd.uniform(150, 260)
            base_share = {u.key: 1.0 + u.share_bias + rnd.uniform(-0.25, 0.25) + self.imbalance_days.get(d, 0) *
                          (1 if u.sleeps else 0) for u in self.units}
            wake = {u.key: 8 * 60 + rnd.randint(20, 100) for u in self.units if u.sleeps}
            t_day = datetime.combine(d, time(0), self.tz)
            for minute in range(1440):
                ts = int((t_day + timedelta(minutes=minute)).timestamp())
                hour = minute / 60
                sol = 0.0
                if 7 <= hour <= 17:
                    sol = peak * math.sin(math.pi * (hour - 7) / 10) * (1 - clouds * rnd.random())
                load = load_night if (hour >= 17 or hour < 7) else 90
                net = sol - load
                ch = dis = 0.0
                if net > 0:
                    room = {k: caps[k] - energy[k] for k in energy}
                    tot = sum(base_share[k] for k in energy if room[k] > 1)
                    for k in energy:
                        if room[k] > 1 and tot > 0:
                            give = min(net * base_share[k] / tot / 60 * self.eta_c, room[k])
                            energy[k] += give
                            ch += give * 60 / self.eta_c
                else:
                    need = -net
                    avail = {k: energy[k] - caps[k] * self.discharge_limit / 100 for k in energy}
                    tot = sum(base_share[k] for k in energy if avail[k] > 1)
                    for k in energy:
                        if avail[k] > 1 and tot > 0:
                            take = min(need * base_share[k] / tot / 60 / self.eta_d, avail[k])
                            energy[k] -= take
                            dis += take * 60 * self.eta_d
                if ts in jumps:
                    for k in energy:
                        energy[k] = caps[k] * jumps[ts] / 100
                if ts in gap_set:
                    continue
                nz = lambda v: max(0.0, round(v * (1 + rnd.gauss(0, self.noise)))) if v > 0 else 0.0  # noqa: E731
                out["ch"].append((ts, nz(ch)))
                out["dis"].append((ts, nz(dis)))
                out["sol"].append((ts, nz(sol)))
                out["out"].append((ts, nz(max(0.0, min(load, sol + dis)))))
                tot_soc = sum(energy.values()) / sum(caps.values()) * 100
                out["sys_soc"].append((ts, self._q(tot_soc)))
                for u in self.units:
                    k = u.key
                    if minute % self.unit_refresh_min == 0 or reported[k] is None:
                        reported[k] = self._q(energy[k] / caps[k] * 100)
                    asleep = u.sleeps and (minute < wake.get(k, 0) or minute >= 22 * 60)
                    out[f"{k}_soc"].append((ts, 0.0 if asleep else reported[k]))
                    out[f"{k}_temp"].append((ts, round(self.temp + 4 * math.sin(math.pi * hour / 24), 0)))
        return out

    def _q(self, v: float) -> float:
        return float(min(100, max(0, round(v / self.soc_step) * self.soc_step)))


def load_into(store, data: dict, drossel=None):
    for k, rows in data.items():
        store.add(k, rows)
    if drossel:
        store.add("drossel", drossel)
