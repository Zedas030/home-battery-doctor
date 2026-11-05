"""German installations (version 1.0): German config keys, German texts, unchanged sensor IDs, database migration."""
import json
import sqlite3
from datetime import datetime, timedelta

from akd.alerts import Alerts
from akd.analysis import Analyzer
from akd.config import load_config
from akd.i18n import set_lang
from akd.publish import sensors
from akd.store import Store
from akd.synth import Sim, SimUnit, load_into

GERMAN_YAML = """
sprache: de
home_assistant: {url: "http://127.0.0.1:1", notify: notify.test}
zeitzone: Europe/Berlin
speicher_pfad: "{data}"
system:
  name: Testsystem
  soc: sensor.sys_soc
  ladeleistung: sensor.ch
  entladeleistung: sensor.dis
  solarleistung: sensor.sol
  akku_anzahl: sensor.anzahl
einheiten:
  master: {name: Master, soc: sensor.m_soc, temperatur: sensor.m_temp, nennkapazitaet_kwh: 2.048, inbetriebnahme: 2026-04-17,
           inbetriebnahme_geschaetzt: true}
  slave: {name: Slave, soc: sensor.s_soc, nennkapazitaet_kwh: 2.048, inbetriebnahme: 2026-04-17, schlaeft_mit_null: true}
garantie: {jahre: 10, min_restkapazitaet_prozent: 50}
schaetzung: {min_hub_prozent: 25, zeitraum_aktuell: 30d}
master_slave: {drossel_status: input_boolean.noah_diff_warnung, umstecken: 2026-10-24}
ueberwachung: {keine_daten_nach: 2h, ruhezeit: ["22:00", "08:00"]}
aufbewahrung: {rohdaten_tage: 90}
"""


def _cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(GERMAN_YAML.replace("{data}", str(tmp_path).replace("\\", "/")), encoding="utf-8")
    return load_config(p, require_token=False)


def test_german_config_keys_still_work(tmp_path):
    try:
        cfg = _cfg(tmp_path)
        assert cfg.language == "de" and cfg.app_name == "Akku-Doktor" and cfg.sensor_prefix == "akku_doktor"
        assert cfg.est.min_swing_percent == 25 and cfg.est.current_period == 30 * 86400
        assert cfg.units["master"].commissioned_estimated and cfg.units["slave"].sleeps_with_zero
        assert cfg.ms.replug_date.isoformat() == "2026-10-24"
        assert cfg.entity_roles()["count"] == "sensor.anzahl"
        assert cfg.entity_roles()["throttle"] == "input_boolean.noah_diff_warnung"
        assert cfg.raw_days == 90 and cfg.mon.no_data_after == 7200
    finally:
        set_lang("en")


def test_german_texts_and_unchanged_sensor_ids(tmp_path):
    try:
        cfg = _cfg(tmp_path)
        store = Store(":memory:")
        an = Analyzer(cfg, store, Alerts(cfg, store))
        sim = Sim([SimUnit("master", 2048), SimUnit("slave", 2048)], days=3, seed=2)
        load_into(store, sim.run())
        s = an.run(datetime.combine(sim.start + timedelta(days=3), datetime.min.time(), cfg.tz).timestamp(), full=True)

        class Mon:
            ha_connected = True
        ids = {e: (st, a) for e, st, a in sensors(cfg, s, Mon(), [])}
        assert "sensor.akku_doktor_system_kapazitaet" in ids
        assert "sensor.akku_doktor_noah_soc_differenz" in ids
        st, attrs = ids["sensor.akku_doktor_system_kapazitaet"]
        assert attrs["status"] in ("keine verlässliche Aussage", "keine Daten")
        assert "grund" in attrs
    finally:
        set_lang("en")


def test_migration_of_a_german_database(tmp_path):
    p = tmp_path / "old.sqlite"
    st = Store(str(p))
    db = st.db
    db.execute("DELETE FROM meta")   # pretend: database of version 1.0 (no schema marker)
    db.execute("INSERT INTO windows (scope, kind, t0, t1, source, used, reason, deltas) VALUES "
               "('system','entladen',1,2,'minuten',0,'hub_zu_klein',?)", (json.dumps({"e_wh_einheiten": 5}),))
    db.execute("INSERT INTO estimates VALUES ('system','aktuell',1,'unsicher',NULL,NULL,NULL,NULL,0,NULL,'{}')")
    db.execute("INSERT INTO daily VALUES ('2026-10-01', ?)", (json.dumps(
        {"quelle": "minuten", "e_entladen_wh": 100, "slave_abwaerts_prozent": 3, "ms_aufwachverzug_min": 40}),))
    db.execute("INSERT INTO meta VALUES ('vorgelernt', '{}'), ('berichte', '{\"2026-10\": \"x\"}')")
    db.execute("INSERT INTO alerts VALUES ('keine_daten', 1, 2, 'offen', 'kritisch', 't', 'm')")
    db.execute("INSERT INTO raw VALUES ('anzahl', 60, 2)")
    db.close()
    st2 = Store(str(p))
    w = st2.windows()[0]
    assert (w["kind"], w["source"], w["reason"]) == ("discharge", "minute", "swing_too_small")
    assert w["deltas"] == {"e_wh_units": 5}
    assert st2.estimates("system", "current")[0]["status"] == "uncertain"
    d = st2.daily()["2026-10-01"]
    assert d == {"source": "minute", "e_discharge_wh": 100, "slave_down_percent": 3, "ms_wake_delay_min": 40}
    assert st2.get_meta("prelearned") == {} and st2.get_meta("reports") == {"2026-10": "x"}
    a = st2.alert("no_data")
    assert a["state"] == "open" and a["level"] == "critical"
    assert st2.last("count") == (60, 2.0)
    st2.db.close()
    # a second start does not migrate again
    st3 = Store(str(p))
    assert st3.get_meta("schema") == 2
    assert sqlite3.connect(str(p)).execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 1
