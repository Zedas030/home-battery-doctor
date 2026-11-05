"""Estimation logic on synthetic charge curves with known capacity."""
from datetime import date, datetime, timedelta

import pytest

from akd.estimate import OK, UNSURE
from akd.synth import Sim, SimUnit, load_into
from akd.trend import growatt_reference
from conftest import make_cfg
from akd.alerts import Alerts
from akd.analysis import Analyzer
from akd.store import Store


def run_sim(env, sim: Sim, end_day: date | None = None):
    cfg, store, alerts, an = env
    load_into(store, sim.run())
    end_day = end_day or sim.start + timedelta(days=sim.days)
    now = datetime.combine(end_day, datetime.min.time(), cfg.tz).timestamp()
    return an.run(now, full=True), now


def within(est, truth, rel):
    return abs(est - truth) / truth <= rel


def test_known_capacity_system(env):
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048, sleeps=True)], days=40, seed=3)
    s, _ = run_sim(env, sim)
    cur = s["scopes"]["system"]["current"]
    truth = sim.true_usable_kwh(sim.start + timedelta(days=20))
    assert cur["status"] == OK
    assert within(cur["cap_kwh"], truth, 0.02), (cur, truth)
    assert cur["lo"] <= truth <= cur["hi"]
    assert cur["n"] >= 20
    # Wirkungsgrad Entlade-/Ladeseite = eta_c * eta_d
    assert cur["eff"] == pytest.approx(sim.eta_c * sim.eta_d, abs=0.02)


def test_per_unit_capacity_and_unequal_aging_alert(env):
    cfg, store, alerts, an = env
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 1740, sleeps=True, share_bias=-0.2)], days=45, seed=5)
    s, _ = run_sim(env, sim)
    d = sim.start + timedelta(days=22)
    for u in ("master", "slave"):
        cur = s["scopes"][u]["current"]
        truth = sim.true_usable_kwh(d, u)
        assert cur["status"] == OK, cur
        assert within(cur["cap_kwh"], truth, 0.04), (u, cur, truth)
        assert cur["lo"] <= truth <= cur["hi"], (u, cur, truth)
    assert alerts.is_open("unequal_aging")


def test_artificial_aging_trend_and_forecast(env):
    # 8 % loss per year over 8 months -> trend about -8 percentage points/year, 80 % in ~1.5 years
    sim = Sim([SimUnit("master", 2048, aging_per_year=0.08), SimUnit("slave", 2048, aging_per_year=0.08)],
              start=date(2026, 4, 1), days=245, seed=11)
    s, _ = run_sim(env, sim)
    tr = s["scopes"]["system"]["trend"]
    assert tr["status"] == "ok", tr
    expected = -0.08 * 100 * sim.eta_d   # SOH relative to the nominal capacity
    assert abs(tr["slope_pp_year"] - expected) < 2.5, tr
    lo, hi = tr["slope_range"]
    assert lo <= expected <= hi
    # true curve: SOH(t) = 97 % * (1 - 0.08 t) -> 80 % at t = (1 - 80/97)/0.08 years
    t80 = (1 - 80 / 97) / 0.08
    true_date = sim.start + timedelta(days=t80 * 365.25)
    p = date.fromisoformat(tr["forecast_80"]["date"])
    assert abs((p - true_date).days) < 120, (p, true_date)
    assert "expected in" in tr["forecast_80"]["text"]


def test_too_little_data_says_no_reliable_statement(env):
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048)], days=3, seed=2)
    s, _ = run_sim(env, sim)
    cur = s["scopes"]["system"]["current"]
    assert cur["status"] == UNSURE
    assert "clean measurement windows" in cur["detail"]["reason"]
    assert s["scopes"]["system"]["trend"]["status"] == "too_early"


def test_coarse_soc_steps_rejected(env):
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048)], days=20, seed=4, soc_step=10)
    s, _ = run_sim(env, sim)
    cur = s["scopes"]["system"]["current"]
    assert cur["status"] == UNSURE
    assert "10 % steps" in cur["detail"]["reason"]


def test_gap_and_calibration_jump_not_used(env):
    cfg = env[0]
    start = date(2026, 4, 1)
    gap_at = datetime.combine(start + timedelta(days=5), datetime.min.time(), cfg.tz) + timedelta(hours=21)
    jump_at = datetime.combine(start + timedelta(days=8), datetime.min.time(), cfg.tz) + timedelta(hours=22)
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048)], days=12, seed=6, gaps=[(gap_at, 40)],
              jumps=[(jump_at, 90)])
    run_sim(env, sim)
    store = env[1]
    for w in store.windows(used=True):
        assert not (w["t0"] < gap_at.timestamp() < w["t1"]), "window across a data gap used"
        assert not (w["t0"] < jump_at.timestamp() < w["t1"]), "window across a SOC jump used"


def test_sleeping_slave_zero_is_not_empty(env):
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048, sleeps=True)], days=10, seed=8)
    s, _ = run_sim(env, sim)
    days = env[1].daily()
    assert all((v.get("slave_soc_min") or 1) > 0 for v in days.values() if v.get("source") == "minute")
    # slave full cycles not inflated by sleep zeros: similar to the master
    assert s["cycles"]["slave"] == pytest.approx(s["cycles"]["master"], rel=0.25)


def test_hourly_statistics_give_coarse_but_correct_estimate(tmp_path):
    cfg = make_cfg(tmp_path)
    minute = Store(":memory:")
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048)], days=40, seed=9)
    data = sim.run()
    # hourly statistics like HA: mean/min/max per hour, no minute values
    store = Store(":memory:")
    for k, rows in data.items():
        buckets = {}
        for ts, v in rows:
            buckets.setdefault(int(ts) // 3600 * 3600, []).append(v)
        store.put_hourly(k, [(t, sum(v) / len(v), min(v), max(v)) for t, v in buckets.items()])
    an = Analyzer(cfg, store, Alerts(cfg, store))
    now = datetime.combine(sim.start + timedelta(days=40), datetime.min.time(), cfg.tz).timestamp()
    s = an.run(now, full=True)
    cur = s["scopes"]["system"]["current"]
    truth = sim.true_usable_kwh(sim.start + timedelta(days=20))
    assert cur["detail"]["sources"] == ["hourly"]
    assert cur["cap_kwh"] is not None and within(cur["cap_kwh"], truth, 0.07), (cur, truth)
    assert cur["lo"] <= truth <= cur["hi"]
    del minute


def test_growatt_reference_formula(env):
    cfg = env[0]
    # Beispiel: 1 Jahr, 300 Zyklen -> 95 % * (1 - 1,5 % - 3,5 %) = 90,25 %
    assert growatt_reference(cfg, 1.0, 300) == pytest.approx(90.25, abs=0.05)
    # 600 Zyklen in einem Jahr -> Minderung (600-300)/300 = 100 % -> 0
    assert growatt_reference(cfg, 1.0, 600) == 0.0


def test_warranty_warning_near_limit(env):
    from akd.trend import warranty_status
    cfg = env[0]
    cur = {"status": "ok", "soh": 53.0, "detail": {"soh_lo": 51.0, "soh_hi": 55.0}}
    g = warranty_status(cfg, "master", cur, {}, 900, date(2029, 4, 1))
    assert g["status"] == "warning"
    cur = {"status": "ok", "soh": 45.0, "detail": {"soh_lo": 43.0, "soh_hi": 47.0}}
    g = warranty_status(cfg, "master", cur, {}, 900, date(2029, 4, 1))
    assert g["status"] == "claim"
    g = warranty_status(cfg, "master", {"status": "ok", "soh": 90, "detail": {"soh_lo": 88, "soh_hi": 92}}, {}, 2500,
                        date(2035, 6, 1))
    assert g["status"] == "ending"


def test_compaction_keeps_aggregates():
    st = Store(":memory:")
    st.add("ch", [(1_700_000_000 + i * 60, float(i)) for i in range(120)])
    n = st.compact(1_700_000_000 + 400 * 86400, 180)
    assert n == 120
    agg = st.agg_series("ch", 0, 4e9)
    assert sum(r[6] for r in agg) == 120
    assert st.series("ch", 0, 4e9) == []


def test_constant_night_power_is_real_but_frozen_cloud_is_gap(env):
    """At night the storage reports identical values with a fixed output setpoint – that is real as long as the SOC
    goes down. If the SOC stands still as well although power is shown, the cloud hangs: gap."""
    from akd.grid import build
    cfg, store = env[0], env[1]
    t0 = 1_790_000_000 // 60 * 60
    for k, v in (("ch", 0.0), ("sol", 0.0), ("dis", 230.0), ("out", 225.0)):
        store.add(k, [(t0 + i * 60, v) for i in range(240)])
    # first 2 h: SOC goes down; then 2 h frozen
    store.add("sys_soc", [(t0 + i * 60, 80 - i // 10 if i < 120 else 68) for i in range(240)])
    g = build(store, cfg, t0, t0 + 240 * 60)
    dis = g.get("dis")
    assert all(v is not None for v in dis[:115])
    assert all(v is None for v in dis[125:235])


def test_windows_only_when_all_batteries_reported(env):
    """Before the slave was connected (battery count 1) no window may enter the system estimate."""
    cfg, store, alerts, an = env
    cfg.system.battery_count = "sensor.anzahl"
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048)], days=20, seed=12)
    data = sim.run()
    load_into(store, data)
    half = data["ch"][len(data["ch"]) // 2][0]
    store.add("count", [(ts, 1.0 if ts < half else 2.0) for ts, _ in data["ch"]])
    now = datetime.combine(sim.start + timedelta(days=20), datetime.min.time(), cfg.tz).timestamp()
    an.run(now, full=True)
    for w in store.windows():
        if w["t0"] < half - 3600:
            assert not w["used"] and w["reason"] == "battery_count", w
    assert any(w["used"] for w in store.windows() if w["t0"] > half)


def test_placeholder_temperatures_ignored(env):
    from akd.grid import build
    cfg, store = env[0], env[1]
    t0 = 1_790_000_000 // 60 * 60
    store.add("master_soc", [(t0 + i * 60, 0.0 if i < 30 else 50.0) for i in range(60)])
    store.add("master_temp", [(t0 + i * 60, 0.0 if i < 30 else (-273.0 if i == 45 else 21.0)) for i in range(60)])
    g = build(store, cfg, t0, t0 + 3600)
    temps = g.get("master_temp")
    assert all(v is None for v in temps[:30])
    assert temps[45] is None and temps[40] == 21.0
