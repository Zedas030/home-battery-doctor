import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from akd.alerts import Alerts  # noqa: E402
from akd.analysis import Analyzer  # noqa: E402
from akd.config import load_config  # noqa: E402
from akd.store import Store  # noqa: E402

BASE_YAML = """
language: en
home_assistant: {url: "http://127.0.0.1:1", notify: notify.test}
prometheus: {url: ""}
timezone: Europe/Berlin
data_path: "{data}"
system:
  name: Test system
  soc: sensor.sys_soc
  charge_power: sensor.ch
  discharge_power: sensor.dis
  solar_power: sensor.sol
  output_power: sensor.out
units:
  master: {name: Master, soc: sensor.m_soc, temperature: sensor.m_temp, nominal_kwh: 2.048, commissioned: 2026-04-01}
  slave: {name: Slave, soc: sensor.s_soc, temperature: sensor.s_temp, nominal_kwh: 2.048, commissioned: 2026-04-01, sleeps_with_zero: true}
master_slave:
  throttle_flag: input_boolean.throttle
  replug_date: auto
  replug_search_range: [2026-04-01, 2027-12-31]
estimation:
  trend_min_months: 6
"""


def make_cfg(tmp_path, extra: str = ""):
    p = tmp_path / "config.yaml"
    p.write_text(BASE_YAML.replace("{data}", str(tmp_path / "data").replace("\\", "/")) + extra, encoding="utf-8")
    return load_config(p, require_token=False)


@pytest.fixture
def env(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(":memory:")
    alerts = Alerts(cfg, store)
    an = Analyzer(cfg, store, alerts)
    return cfg, store, alerts, an
