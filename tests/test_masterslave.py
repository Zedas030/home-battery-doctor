"""Master/slave: detect the re-plug date, before/after with confidence, no false finding without a change."""
from datetime import date, datetime, timedelta

from akd.synth import Sim, SimUnit, load_into


def _run(env, sim):
    cfg, store, alerts, an = env
    load_into(store, sim.run())
    now = datetime.combine(sim.start + timedelta(days=sim.days), datetime.min.time(), cfg.tz).timestamp()
    return an.run(now, full=True)


def test_replug_improvement_detected(env):
    start, switch = date(2026, 9, 20), date(2026, 10, 20)
    imb = {start + timedelta(days=i): -0.6 for i in range((switch - start).days)}
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048, sleeps=True)], start=start, days=50, seed=21,
              imbalance_days=imb)
    s = _run(env, sim)
    ms = s["master_slave"]
    cp = ms["replug"]
    assert cp["status"] == "ok", cp
    assert abs((date.fromisoformat(cp["date"]) - switch).days) <= 2, cp
    k = ms["comparison"]["metrics"]["ms_diff_abs"]
    assert k["status"] == "significant", k
    assert k["difference"] < 0 and k["range"][1] < 0
    assert "improved" in ms["comparison"]["summary"]


def test_no_change_no_false_finding(env):
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048, sleeps=True)], start=date(2026, 9, 20), days=50, seed=22)
    s = _run(env, sim)
    cp = s["master_slave"]["replug"]
    assert cp["status"] == "no_clear_break", cp
    assert "comparison" not in s["master_slave"]


def test_configured_date_with_too_few_days_after(tmp_path):
    from akd.alerts import Alerts
    from akd.analysis import Analyzer
    from akd.store import Store
    from conftest import make_cfg
    cfg = make_cfg(tmp_path)
    cfg.ms.replug_date = date(2026, 10, 17)
    store = Store(":memory:")
    an = Analyzer(cfg, store, Alerts(cfg, store))
    start = date(2026, 9, 20)
    imb = {start + timedelta(days=i): -0.6 for i in range(27)}
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048, sleeps=True)], start=start, days=30, seed=23,
              imbalance_days=imb)
    load_into(store, sim.run())
    s = an.run(datetime.combine(start + timedelta(days=30), datetime.min.time(), cfg.tz).timestamp(), full=True)
    v = s["master_slave"]["comparison"]
    assert v["metrics"]["ms_diff_abs"]["status"] in ("preliminary", "too_few_days")
    assert "preliminary" in v["summary"]


def test_wake_delay_measured(env):
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048, sleeps=True)], start=date(2026, 6, 1), days=6, seed=24)
    _run(env, sim)
    days = env[1].daily()
    delays = [v["ms_wake_delay_min"] for v in days.values() if v.get("ms_wake_delay_min") is not None]
    assert delays and all(d > 0 for d in delays)
