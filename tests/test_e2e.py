"""End to end: fake HA + Prometheus -> import history, estimate, sensors, notifications, web, PDF, report,
self-monitoring (rename, missing entity, unit), read-only guarantee."""
import asyncio
import io
import socket
import zipfile
from datetime import date, datetime, timedelta

import aiohttp
import pytest

from akd.app import Service
from akd.ha import ReadOnlyViolation
from akd.synth import Sim, SimUnit
from akd.web import start_web
from conftest import make_cfg
from fakes import TOKEN, FakeHA, FakeProm


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


ROLE_ENT = {"sys_soc": "sensor.sys_soc", "ch": "sensor.ch", "dis": "sensor.dis", "sol": "sensor.sol",
            "out": "sensor.out", "master_soc": "sensor.m_soc", "master_temp": "sensor.m_temp",
            "slave_soc": "sensor.s_soc", "slave_temp": "sensor.s_temp", "throttle": "input_boolean.throttle"}
UNITS = {"sys_soc": "%", "ch": "W", "dis": "W", "sol": "W", "out": "W", "master_soc": "%", "slave_soc": "%",
         "master_temp": "°C", "slave_temp": "°C", "throttle": None}


def test_end_to_end(tmp_path):
    asyncio.run(_e2e(tmp_path))


async def _e2e(tmp_path):
    tz_today = date.today()
    sim = Sim([SimUnit("master", 2048), SimUnit("slave", 1900, sleeps=True, share_bias=-0.15)],
              start=tz_today - timedelta(days=30), days=30, seed=31)
    data = sim.run()
    ha, prom = FakeHA(), FakeProm(data, ROLE_ENT)
    ha_url, prom_url = await ha.start(), await prom.start()
    for role, ent in ROLE_ENT.items():
        last = data.get(role, [(0, 0)])[-1][1] if role != "throttle" else "off"
        ha.set(ent, last, UNITS[role])
    port = free_port()
    cfg = make_cfg(tmp_path, f"\nweb: {{port: {port}}}\n")
    cfg.ha_url, cfg.prometheus_url, cfg.token = ha_url, prom_url, TOKEN
    cfg.backup_dir = str(tmp_path / "backup")
    svc = Service(cfg)
    runner = await start_web(svc, host="127.0.0.1")
    try:
        await svc.startup()
        s = svc.summary
        # 1) history imported and estimated
        cur = s["scopes"]["system"]["current"]
        truth = sim.true_usable_kwh(sim.start + timedelta(days=15))
        assert cur["status"] == "ok", cur
        assert abs(cur["cap_kwh"] - truth) / truth < 0.03
        # 2) sensors in HA
        p = cfg.sensor_prefix
        assert float(ha.posted[f"sensor.{p}_system_capacity"]["state"]) == pytest.approx(cur["cap_kwh"], abs=0.001)
        assert ha.posted[f"sensor.{p}_status"]["state"] in ("ok", "warning")
        assert f"sensor.{p}_ms_soc_difference" in ha.posted
        # no flood of pushes at the first import
        await svc.notifier.deliver(svc.clock.now())
        assert ha.notifications() == []
        # months before the installation are not sent automatically as a report
        assert svc.monthly_due(svc.clock.now() + 40 * 86400) in (None, datetime.now(cfg.tz).strftime("%Y-%m"))
        # 3) web UI, export, metrics
        base = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as cs:
            for path in ("/", "/master-slave", "/windows", "/monitoring", "/reports", "/method", "/api/status", "/fenster", "/berichte"):
                async with cs.get(base + path) as r:
                    assert r.status == 200, path
                    txt = await r.text()
                    assert "Traceback" not in txt
            async with cs.get(base + "/metrics") as r:
                m = await r.text()
                assert 'akd_capacity_kwh{storage="system"' in m and "akd_ha_connected 1" in m
            async with cs.post(base + "/dossier", data={"scope": "system"}, allow_redirects=True) as r:
                assert r.status == 200 and r.content_type == "application/pdf"
                pdf = await r.read()
                assert pdf[:4] == b"%PDF" and len(pdf) > 5000
            zips = list(cfg.dossier_dir.glob("*.zip"))
            assert zips
            with zipfile.ZipFile(zips[0]) as z:
                names = set(z.namelist())
                assert {"windows.csv", "estimates.csv", "raw_minutes.csv", "SHA256SUMS.txt"} <= names
            async with cs.post(base + "/report", data={"month": datetime.now(cfg.tz).strftime("%Y-%m")}) as r:
                assert r.status == 200 and "Monthly report" in await r.text()
            async with cs.get(base + "/export/all.zip") as r:
                assert r.status == 200
                zipfile.ZipFile(io.BytesIO(await r.read())).testzip()
        # 4) immediate temperature alert -> push (urgent, also during quiet hours)
        ha.set("sensor.m_temp", 47, "°C")
        await svc.poll_once()
        assert svc.alerts.is_open("temp_critical_master")
        await svc.notifier.deliver(svc.clock.now())
        assert any("47" in n["title"] for n in ha.notifications())
        # 5) a rename is detected and followed
        ha.rename("sensor.m_soc", "sensor.m_soc_neu")
        svc._last_registry = 0
        await svc.poll_once()
        await svc.poll_once()
        assert svc.monitor.mapping()["master_soc"] == "sensor.m_soc_neu"
        assert svc.alerts.is_open("renamed_master_soc")
        # 6) entity disappears completely -> alert instead of a silent gap
        del ha.states["sensor.s_temp"]
        ha.registry = [e for e in ha.registry if e["entity_id"] != "sensor.s_temp"]
        await svc.poll_once()
        assert svc.alerts.is_open("missing_slave_temp")
        # 7) unit changes (e.g. kW instead of W) -> alert, values are not taken over
        before = svc.store.last("ch")
        ha.set("sensor.ch", 0.4, "kW")
        await svc.poll_once()
        assert svc.alerts.is_open("unit_ch")
        assert svc.store.last("ch") == before or svc.store.last("ch")[1] != 0.4
        # 8) an HA update is recorded as an event
        ha.version = "2026.11.0"
        svc._last_registry = 0
        await svc.poll_once()
        assert any("2026.11.0" in t for _ts, _k, t in svc.store.events(20))
        # 9) maintenance: compaction + backup
        svc.maintenance(svc.clock.now())
        assert list((tmp_path / "backup").glob("battery-doctor-*.sqlite"))
        # 10) read-only: no other services, only own sensors
        assert {d for d, _s, _x in ha.service_calls} <= {"notify"}
        assert all(e.startswith(f"sensor.{p}") for e in ha.posted)
        with pytest.raises(ReadOnlyViolation):
            await svc.ha.call_service("number", "set_value", {"entity_id": "number.x", "value": 0})
        with pytest.raises(ReadOnlyViolation):
            await svc.ha.set_state("number.noah_2000_system_output_power", 0, {})
        with pytest.raises(ReadOnlyViolation):
            await svc.ha.ws({"type": "call_service", "domain": "number", "service": "set_value"})
        assert "call_service" not in ha.ws_commands
    finally:
        await runner.cleanup()
        await svc.ha.close()
        await ha.stop()
        await prom.stop()
