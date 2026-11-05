"""Acceptance proof: the estimation on synthetic batteries with known capacity (python -m akd synthetic)."""
from __future__ import annotations

import tempfile
from datetime import timedelta, datetime
from pathlib import Path

import yaml

from .alerts import Alerts
from .analysis import Analyzer
from .config import load_config
from .store import Store
from .synth import Sim, SimUnit, load_into

CFG = {
    "language": "en", "home_assistant": {"url": "http://127.0.0.1:1", "notify": "notify.test"}, "timezone": "Europe/Berlin",
    "system": {"name": "Synthetic", "soc": "sensor.a", "charge_power": "sensor.b", "discharge_power": "sensor.c",
               "solar_power": "sensor.d", "output_power": "sensor.e"},
    "units": {"master": {"name": "Master", "soc": "sensor.f", "temperature": "sensor.g", "nominal_kwh": 2.048,
                         "commissioned": "2026-04-01"},
              "slave": {"name": "Slave", "soc": "sensor.h", "temperature": "sensor.i", "nominal_kwh": 2.048,
                        "commissioned": "2026-04-01", "sleeps_with_zero": True}},
    "master_slave": {"replug_date": "auto", "replug_search_range": ["2026-01-01", "2027-12-31"]},
}


def _run(sim: Sim):
    d = Path(tempfile.mkdtemp())
    (d / "c.yaml").write_text(yaml.safe_dump(dict(CFG, data_path=str(d)), allow_unicode=True), encoding="utf-8")
    cfg = load_config(d / "c.yaml", require_token=False)
    st = Store(":memory:")
    an = Analyzer(cfg, st, Alerts(cfg, st))
    load_into(st, sim.run())
    now = datetime.combine(sim.start + timedelta(days=sim.days), datetime.min.time(), cfg.tz).timestamp()
    return an.run(now, full=True), an


def fmt(e):
    if not e or e.get("status") != "ok":
        return f"no reliable statement ({(e or {}).get('detail', {}).get('reason', 'no data')})"
    return f"{e['cap_kwh']:.3f} kWh [{e['lo']:.3f} – {e['hi']:.3f}]"


def main():
    print("Scenario | Scope | true (usable) | estimated [95 % range] | deviation | truth inside range")
    print("---|---|---|---|---|---")
    aging = "3 ageing 8 %/year, 8 months"
    cases = [
        ("1 new, 2 × 2.048 kWh, 40 days", Sim([SimUnit("master", 2048), SimUnit("slave", 2048, sleeps=True)], days=40, seed=3)),
        ("2 slave 15 % weaker, 45 days", Sim([SimUnit("master", 2048), SimUnit("slave", 1740, sleeps=True, share_bias=-0.2)],
                                             days=45, seed=5)),
        (aging, Sim([SimUnit("master", 2048, 0.08), SimUnit("slave", 2048, 0.08)], days=245, seed=11)),
    ]
    out = {}
    for name, sim in cases:
        s, _an = _run(sim)
        out[name] = (s, sim)
        mid = sim.start + timedelta(days=sim.days - 30 if sim.days >= 60 else sim.days // 2)  # middle of the period
        for scope in ("system", "master", "slave"):
            e = s["scopes"][scope]["current"]
            truth = sim.true_usable_kwh(mid, None if scope == "system" else scope)
            if e.get("status") == "ok":
                dev = (e["cap_kwh"] - truth) / truth * 100
                inside = "yes" if e["lo"] <= truth <= e["hi"] else "NO"
                print(f"{name} | {scope} | {truth:.3f} kWh | {fmt(e)} | {dev:+.1f} % | {inside}")
            else:
                print(f"{name} | {scope} | {truth:.3f} kWh | {fmt(e)} | – | –")
    s, sim = out[aging]
    trd = s["scopes"]["system"]["trend"]
    true80 = sim.start + timedelta(days=(1 - 80 / 97) / 0.08 * 365.25)
    print(f"\nTrend (scenario 3): {trd.get('slope_pp_year')} percentage points/year (true {-8 * 0.97:.2f}), "
          f"range {trd.get('slope_range')}")
    print(f"Forecast: {(trd.get('forecast_80') or {}).get('text')} – true 80 % threshold: {true80:%Y-%m}")
    print("\nToo little data:")
    for name, sim in (("4 only 3 days", Sim([SimUnit("master", 2048), SimUnit("slave", 2048)], days=3, seed=2)),
                      ("5 SOC in 10 % steps only, 20 days",
                       Sim([SimUnit("master", 2048), SimUnit("slave", 2048)], days=20, seed=4, soc_step=10))):
        s, _ = _run(sim)
        print(f"  {name}: {fmt(s['scopes']['system']['current'])}; trend: {s['scopes']['system']['trend'].get('text')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
