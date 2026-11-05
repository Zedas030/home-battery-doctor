"""SQLite store: raw minute values, compacted 15-minute values, hourly statistics, measurement windows,
estimates, daily values (incl. master/slave), alerts and the notification outbox."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw (k TEXT NOT NULL, ts INTEGER NOT NULL, v REAL NOT NULL, PRIMARY KEY (k, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS agg15 (k TEXT NOT NULL, ts INTEGER NOT NULL, mean REAL, mn REAL, mx REAL, first REAL,
    last REAL, n INTEGER, PRIMARY KEY (k, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hourly (k TEXT NOT NULL, ts INTEGER NOT NULL, mean REAL, mn REAL, mx REAL,
    PRIMARY KEY (k, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL, t0 INTEGER NOT NULL, t1 INTEGER NOT NULL,
    source TEXT NOT NULL, soc0 REAL, soc1 REAL, e_wh REAL, cap_kwh REAL, sigma_rel REAL, temp REAL,
    deltas TEXT, used INTEGER NOT NULL, reason TEXT, created INTEGER, UNIQUE (scope, kind, t0, source));
CREATE TABLE IF NOT EXISTS estimates (
    scope TEXT NOT NULL, period TEXT NOT NULL, computed INTEGER, status TEXT, cap_kwh REAL, lo REAL, hi REAL,
    soh REAL, n INTEGER, eff REAL, detail TEXT, PRIMARY KEY (scope, period)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS daily (day TEXT PRIMARY KEY, data TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY, first INTEGER, last INTEGER, state TEXT, level TEXT, title TEXT, message TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY, ts INTEGER, title TEXT, message TEXT, urgent INTEGER, data TEXT, sent INTEGER,
    attempts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, text TEXT);
"""


REASONS = {"hub_zu_klein": "swing_too_small", "zu_kurz": "too_short", "leistung_zu_klein": "power_too_low",
           "einheit_ohne_werte": "unit_missing", "temperatur": "temperature", "soc_ruecklaeufig": "soc_reversal",
           "unplausibel": "implausible", "soc_sprung": "soc_jump", "soc_gegenlaeufig": "soc_wrong_direction",
           "ausreisser": "outlier", "luecke": "gap", "soc_zu_grob": "soc_too_coarse", "akkuanzahl": "battery_count"}
DELTAS = {"e_wh_einheiten": "e_wh_units", "sys_hub_einheiten": "sys_swing_units", "teilfenster": "partial",
          "luecken_min": "gap_min", "p_mittel_w": "p_mean_w", "soc_stufe": "soc_step"}
DAILY = {"quelle": "source", "e_laden_wh": "e_charge_wh", "e_entladen_wh": "e_discharge_wh",
         "minuten_mit_daten": "minutes_with_data", "stunden_mit_daten": "hours_with_data",
         "ms_minuten_beide": "ms_minutes_both", "ms_sonnen_minuten": "ms_sun_minutes", "ms_abdeckung": "ms_coverage",
         "ms_diff_mittel": "ms_diff_mean", "ms_diff_abs_sonne": "ms_diff_abs_sun", "ms_drossel_min": "ms_throttle_min",
         "ms_master_wach": "ms_master_awake", "ms_slave_wach": "ms_slave_awake",
         "ms_aufwachverzug_min": "ms_wake_delay_min", "ms_slave_nicht_wach": "ms_slave_not_awake",
         "ms_laderate_master_pph": "ms_charge_rate_master_pph", "ms_laderate_slave_pph": "ms_charge_rate_slave_pph"}
DAILY_SUFFIX = {"_abwaerts_prozent": "_down_percent", "_minuten_mit_werten": "_minutes_with_values",
                "_temp_verletzung_min": "_temp_violation_min", "_temp_verletzung_h": "_temp_violation_h"}
META = {"vorgelernt": "prelearned", "entitaeten": "entity_baseline", "umbenannt": "renamed", "berichte": "reports",
        "letzte_auswertung": "last_analysis", "wartung": "maintenance", "ha_update_zeit": "ha_update_time"}
ALERTS = {"einbruch_": "drop_", "garantie_": "warranty_", "altern_ungleich": "unequal_aging",
          "ms_verschlechterung": "ms_worsening", "slave_ohne_werte": "slave_no_values", "umbenannt_": "renamed_",
          "einheit_": "unit_", "fehlt_": "missing_", "keine_daten": "no_data", "temp_kritisch_": "temp_critical_",
          "temp_hinweis_": "temp_notice_", "soc_eingefroren": "soc_frozen", "sicherung": "backup", "ha_weg": "ha_down"}


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.migrate()

    # ----------------------------------------------------------- migration of German internal codes (<= 1.0.0)
    def migrate(self):
        if self.get_meta("schema") == 2:
            return
        with self.lock:
            x = self.db.execute
            x("UPDATE windows SET kind='charge' WHERE kind='laden'")
            x("UPDATE windows SET kind='discharge' WHERE kind='entladen'")
            x("UPDATE windows SET source='minute' WHERE source='minuten'")
            x("UPDATE windows SET source='hourly' WHERE source='stunden'")
            for a, b in REASONS.items():
                x("UPDATE windows SET reason=? WHERE reason=?", (b, a))
            for wid, deltas in x("SELECT id, deltas FROM windows").fetchall():
                d = json.loads(deltas or "{}")
                x("UPDATE windows SET deltas=? WHERE id=?", (json.dumps({DELTAS.get(k, k): v for k, v in d.items()}), wid))
            x("UPDATE estimates SET status='uncertain' WHERE status='unsicher'")
            x("UPDATE estimates SET status='no_data' WHERE status='keine_daten'")
            for a, b in (("aktuell", "current"), ("kurz", "short"), ("vorher", "previous")):
                x("DELETE FROM estimates WHERE period=?", (b,))
                x("UPDATE estimates SET period=? WHERE period=?", (b, a))
            for a, b in (("anzahl", "count"), ("drossel", "throttle")):
                for t in ("raw", "agg15", "hourly"):
                    x(f"UPDATE OR REPLACE {t} SET k=? WHERE k=?", (b, a))
            for day, data in x("SELECT day, data FROM daily").fetchall():
                d = json.loads(data)
                nd = {}
                for k, v in d.items():
                    nk = DAILY.get(k, k)
                    for de_suffix, en_suffix in DAILY_SUFFIX.items():
                        if nk.endswith(de_suffix):
                            nk = nk[: -len(de_suffix)] + en_suffix
                    nd[nk] = {"minuten": "minute", "stunden": "hourly"}.get(v, v) if nk == "source" else v
                x("UPDATE daily SET data=? WHERE day=?", (json.dumps(nd), day))
            for k, v in x("SELECT k, v FROM meta").fetchall():
                if k in META:
                    x("DELETE FROM meta WHERE k=?", (META[k],))
                    x("UPDATE meta SET k=? WHERE k=?", (META[k], k))
            for key, *_ in x("SELECT key FROM alerts").fetchall():
                for a, b in ALERTS.items():
                    if key == a or (a.endswith("_") and key.startswith(a)):
                        x("UPDATE OR REPLACE alerts SET key=? WHERE key=?", (b + key[len(a):] if a.endswith("_") else b, key))
                        break
            x("UPDATE alerts SET state='open' WHERE state='offen'")
            x("UPDATE alerts SET state='done' WHERE state='erledigt'")
            x("UPDATE alerts SET level='critical' WHERE level='kritisch'")
            x("UPDATE alerts SET level='warning' WHERE level='warnung'")
        self.set_meta("schema", 2)

    # ----------------------------------------------------------- helpers
    def x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def many(self, sql, rows):
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.executemany(sql, rows)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    # ----------------------------------------------------------- meta
    def get_meta(self, k, default=None):
        r = self.x("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, k, v):
        self.x("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, json.dumps(v, ensure_ascii=False, default=str)))

    def event(self, ts, kind, text):
        self.x("INSERT INTO events (ts, kind, text) VALUES (?,?,?)", (int(ts), kind, text))

    def events(self, limit=50):
        return self.x("SELECT ts, kind, text FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ----------------------------------------------------------- samples
    def add(self, k: str, rows):
        """rows: iterable (ts, value). Timestamps are floored to the minute (one value per minute)."""
        self.many("INSERT OR REPLACE INTO raw VALUES (?,?,?)",
                  [(k, int(ts) // 60 * 60, float(v)) for ts, v in rows])

    def add_if_missing(self, k: str, rows):
        self.many("INSERT OR IGNORE INTO raw VALUES (?,?,?)",
                  [(k, int(ts) // 60 * 60, float(v)) for ts, v in rows])

    def series(self, k: str, t0: float, t1: float) -> list[tuple[int, float]]:
        return self.x("SELECT ts, v FROM raw WHERE k=? AND ts>=? AND ts<? ORDER BY ts",
                      (k, int(t0), int(t1))).fetchall()

    def last(self, k: str):
        return self.x("SELECT ts, v FROM raw WHERE k=? ORDER BY ts DESC LIMIT 1", (k,)).fetchone()

    def raw_range(self, k: str | None = None):
        if k:
            return self.x("SELECT MIN(ts), MAX(ts), COUNT(*) FROM raw WHERE k=?", (k,)).fetchone()
        return self.x("SELECT MIN(ts), MAX(ts), COUNT(*) FROM raw").fetchone()

    def agg_series(self, k: str, t0: float, t1: float):
        return self.x("SELECT ts, mean, mn, mx, first, last, n FROM agg15 WHERE k=? AND ts>=? AND ts<? ORDER BY ts",
                      (k, int(t0), int(t1))).fetchall()

    def put_hourly(self, k: str, rows):
        self.many("INSERT OR REPLACE INTO hourly VALUES (?,?,?,?,?)",
                  [(k, int(ts), mean, mn, mx) for ts, mean, mn, mx in rows])

    def hourly(self, k: str, t0: float, t1: float):
        return self.x("SELECT ts, mean, mn, mx FROM hourly WHERE k=? AND ts>=? AND ts<? ORDER BY ts",
                      (k, int(t0), int(t1))).fetchall()

    def hourly_range(self):
        return self.x("SELECT MIN(ts), MAX(ts), COUNT(*) FROM hourly").fetchone()

    def compact(self, now: float, raw_days: int) -> int:
        """Minute values older than raw_days -> 15-minute aggregates, then delete them."""
        cut = int(now - raw_days * 86400) // 900 * 900
        n = 0
        with self.lock:
            keys = [r[0] for r in self.db.execute("SELECT DISTINCT k FROM raw WHERE ts < ?", (cut,)).fetchall()]
            self.db.execute("BEGIN")
            try:
                for k in keys:
                    buckets: dict[int, list[float]] = {}
                    for ts, v in self.db.execute("SELECT ts, v FROM raw WHERE k=? AND ts<? ORDER BY ts", (k, cut)):
                        buckets.setdefault(ts // 900 * 900, []).append(v)
                    self.db.executemany("INSERT OR REPLACE INTO agg15 VALUES (?,?,?,?,?,?,?,?)",
                                        [(k, b, sum(vs) / len(vs), min(vs), max(vs), vs[0], vs[-1], len(vs))
                                         for b, vs in buckets.items()])
                    n += self.db.execute("DELETE FROM raw WHERE k=? AND ts < ?", (k, cut)).rowcount
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return n

    # ----------------------------------------------------------- windows
    def put_window(self, w: dict, now: float):
        self.x("""INSERT INTO windows (scope, kind, t0, t1, source, soc0, soc1, e_wh, cap_kwh, sigma_rel, temp, deltas,
                  used, reason, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT (scope, kind, t0, source) DO UPDATE SET t1=excluded.t1, soc0=excluded.soc0,
                  soc1=excluded.soc1, e_wh=excluded.e_wh, cap_kwh=excluded.cap_kwh, sigma_rel=excluded.sigma_rel,
                  temp=excluded.temp, deltas=excluded.deltas, used=excluded.used, reason=excluded.reason""",
               (w["scope"], w["kind"], int(w["t0"]), int(w["t1"]), w["source"], w.get("soc0"), w.get("soc1"),
                w.get("e_wh"), w.get("cap_kwh"), w.get("sigma_rel"), w.get("temp"),
                json.dumps(w.get("deltas") or {}), 1 if w["used"] else 0, w.get("reason") or "", int(now)))

    def windows(self, scope=None, kind=None, t0=None, t1=None, used=None, source=None):
        q, a = "SELECT * FROM windows WHERE 1=1", []
        for col, val, op in (("scope", scope, "="), ("kind", kind, "="), ("t0", t0, ">="), ("t0", t1, "<"),
                             ("used", None if used is None else int(used), "="), ("source", source, "=")):
            if val is not None:
                q += f" AND {col}{op}?"
                a.append(val)
        cur = self.x(q + " ORDER BY t0", a)
        cols = [c[0] for c in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["deltas"] = json.loads(d["deltas"] or "{}")
            out.append(d)
        return out

    def delete_windows(self, source: str, t0: float, t1: float):
        self.x("DELETE FROM windows WHERE source=? AND t0>=? AND t0<?", (source, int(t0), int(t1)))

    # ----------------------------------------------------------- estimates
    def put_estimate(self, scope, period, computed, e: dict):
        self.x("INSERT OR REPLACE INTO estimates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (scope, period, int(computed), e["status"], e.get("cap_kwh"), e.get("lo"), e.get("hi"), e.get("soh"),
                e.get("n", 0), e.get("eff"), json.dumps(e.get("detail") or {}, ensure_ascii=False, default=str)))

    def estimates(self, scope=None, period=None):
        q, a = "SELECT * FROM estimates WHERE 1=1", []
        if scope:
            q += " AND scope=?"
            a.append(scope)
        if period:
            q += " AND period=?"
            a.append(period)
        cur = self.x(q + " ORDER BY scope, period", a)
        cols = [c[0] for c in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["detail"] = json.loads(d["detail"] or "{}")
            out.append(d)
        return out

    # ----------------------------------------------------------- daily master/slave
    def put_daily(self, day: str, data: dict):
        self.x("INSERT OR REPLACE INTO daily VALUES (?,?)", (day, json.dumps(data)))

    def daily(self, d0: str | None = None, d1: str | None = None) -> dict[str, dict]:
        q, a = "SELECT day, data FROM daily WHERE 1=1", []
        if d0:
            q += " AND day>=?"
            a.append(d0)
        if d1:
            q += " AND day<=?"
            a.append(d1)
        return {d: json.loads(j) for d, j in self.x(q + " ORDER BY day", a).fetchall()}

    # ----------------------------------------------------------- alerts + outbox
    def alert(self, key):
        r = self.x("SELECT key, first, last, state, level, title, message FROM alerts WHERE key=?", (key,)).fetchone()
        return dict(zip(("key", "first", "last", "state", "level", "title", "message"), r)) if r else None

    def put_alert(self, key, first, last, state, level, title, message):  # state: open | done
        self.x("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,?,?)",
               (key, int(first), int(last), state, level, title, message))

    def alerts(self, state=None):
        q = "SELECT key, first, last, state, level, title, message FROM alerts"
        rows = self.x(q + (" WHERE state=?" if state else "") + " ORDER BY last DESC", (state,) if state else ()).fetchall()
        return [dict(zip(("key", "first", "last", "state", "level", "title", "message"), r)) for r in rows]

    def enqueue(self, ts, title, message, urgent, data=None):
        self.x("INSERT INTO outbox (ts, title, message, urgent, data, sent) VALUES (?,?,?,?,?,NULL)",
               (int(ts), title, message, 1 if urgent else 0, json.dumps(data or {})))

    def pending(self):
        return self.x("SELECT id, ts, title, message, urgent, data FROM outbox WHERE sent IS NULL ORDER BY id").fetchall()

    def mark_sent(self, oid, ts):
        self.x("UPDATE outbox SET sent=? WHERE id=?", (int(ts), oid))

    def mark_attempt(self, oid):
        self.x("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (oid,))

    def messages(self, limit=20):
        return self.x("SELECT id, ts, title, message, sent FROM outbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ----------------------------------------------------------- maintenance
    def size_bytes(self) -> int:
        if self.path == ":memory:":
            return 0
        p = Path(self.path)
        return sum(f.stat().st_size for f in (p, p.with_name(p.name + "-wal")) if f.exists())

    def backup_to(self, target: str):
        dst = sqlite3.connect(target)
        with self.lock:
            self.db.backup(dst)
        dst.close()
