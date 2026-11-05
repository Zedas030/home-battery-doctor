"""The service: poll every minute, analyse every 30 min, sensors, notifications, maintenance, monthly report."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__
from .alerts import Alerts, Notifier
from .analysis import Analyzer
from .config import Config
from .ha import HAClient
from .history import backfill
from .i18n import L
from .monitor import Monitor
from .publish import metrics, sensors
from .report import write_monthly
from .store import Store

log = logging.getLogger("akd.app")


class Clock:
    def now(self) -> float:
        return time.time()


class Service:
    def __init__(self, cfg: Config, clock: Clock | None = None, store: Store | None = None):
        self.cfg = cfg
        self.clock = clock or Clock()
        self.store = store or Store(cfg.db_path)
        self.alerts = Alerts(cfg, self.store)
        self.analyzer = Analyzer(cfg, self.store, self.alerts)
        self.monitor = Monitor(cfg, self.store, self.alerts)
        self.ha = HAClient(cfg.ha_url, cfg.token, cfg.sensor_prefix)
        self.notifier = Notifier(cfg, self.store, self.ha)
        self.started = self.clock.now()
        self.errors = 0
        self.lock = asyncio.Lock()
        self.ready = False
        self.backfill_report = None
        self._last_registry = 0.0
        self._last_publish = 0.0

    @property
    def summary(self) -> dict:
        return self.analyzer.summary

    # ------------------------------------------------------------------ start
    async def startup(self):
        now = self.clock.now()
        self.store.event(now, "start", f"{self.cfg.app_name} {__version__} started")
        await self.poll_once()
        if not self.store.get_meta("prelearned"):
            log.info("first start: importing existing history …")
            self.backfill_report = await backfill(self.cfg, self.store, self.ha, now, progress=log.info)
            self.alerts.silent = True   # do not report history as new events
            try:
                await self.analyse(full=True)
            finally:
                self.alerts.silent = False
            self.store.event(now, "prelearned", "history imported and analysed")
            # months before the installation are not sent automatically as a report (on demand only)
            month = datetime.fromtimestamp(now, self.cfg.tz).strftime("%Y-%m")
            done = self.store.get_meta("reports") or {}
            for d in self.store.daily():
                if d[:7] < month:
                    done.setdefault(d[:7], "before installation – on demand only")
            self.store.set_meta("reports", done)
        else:
            await self.analyse(full=False)
        self.ready = True
        self.monitor.checks_enabled = True
        await self.publish()

    async def analyse(self, full=False):
        async with self.lock:
            now = self.clock.now()
            await asyncio.to_thread(self.analyzer.run, now, full)
            self.store.set_meta("last_analysis", now)

    # ------------------------------------------------------------------ polling
    async def poll_once(self):
        now = self.clock.now()
        try:
            states = await self.ha.states()
            self.monitor.ha_connected = True
            self.monitor.poll(states, now)
            self.alerts.clear(now, "ha_down", L("Home Assistant reachable again.", "Home Assistant wieder erreichbar."))
            if now - self._last_registry > 3600:
                self._last_registry = now
                try:
                    self.monitor.check_registry(await self.ha.entity_registry(), now)
                    conf = await self.ha.config()
                    self.monitor.note_version(conf.get("version", "?"), now)
                except Exception as e:  # noqa: BLE001
                    log.warning("registry/version not readable: %s", e)
        except Exception as e:  # noqa: BLE001
            self.monitor.ha_connected = False
            self.monitor.poll_errors += 1
            self.errors += 1
            log.warning("Home Assistant not reachable: %s", e)

    async def publish(self):
        if not self.summary.get("scopes"):
            return
        self._last_publish = self.clock.now()
        for eid, state, attrs in sensors(self.cfg, self.summary, self.monitor, self.store.alerts("open")):
            try:
                await self.ha.set_state(eid, state, attrs)
            except Exception as e:  # noqa: BLE001
                log.warning("sensor %s not set: %s", eid, e)
                self.errors += 1
                break

    def metrics_text(self) -> str:
        return metrics(self.cfg, self.summary, self.monitor, self.store,
                       {"errors_total": self.errors, "notifications_sent_total": self.notifier.sent,
                        "outbox_pending": len(self.store.pending()), "uptime_seconds": self.clock.now() - self.started})

    # ------------------------------------------------------------------ maintenance + report
    def maintenance(self, now: float):
        n = self.store.compact(now, self.cfg.raw_days)
        if n:
            self.store.event(now, "compaction", f"{n} minute values compacted to 15-minute values")
        self.backup(now)
        self.store.set_meta("maintenance", now)

    def backup(self, now: float):
        if not self.cfg.backup_dir:
            return
        d = Path(self.cfg.backup_dir)
        stem = Path(self.cfg.db_path).stem
        try:
            d.mkdir(parents=True, exist_ok=True)
            target = d / f"{stem}-{datetime.fromtimestamp(now, self.cfg.tz):%Y-%m-%d}.sqlite"
            # Writing SQLite directly to a network share (CIFS) can hang on file locks:
            # back up locally first, then copy as a plain file.
            tmp = self.cfg.data_dir / ".backup.sqlite"
            tmp.unlink(missing_ok=True)
            self.store.backup_to(str(tmp))
            shutil.copyfile(tmp, str(target) + ".part")
            Path(str(target) + ".part").replace(target)
            tmp.unlink(missing_ok=True)
            for src in (self.cfg.report_dir, self.cfg.dossier_dir):
                if src.exists():
                    shutil.copytree(src, d / src.name, dirs_exist_ok=True)
            for p in sorted(d.glob(f"{stem}-*.sqlite"))[:-self.cfg.backup_days]:
                p.unlink()
            self.alerts.clear(now, "backup", L("Backup works again.", "Sicherung läuft wieder."))
        except OSError as e:
            self.alerts.raise_(now, "backup", L(f"🔧 {self.cfg.app_name}: backup failed",
                                                f"🔧 {self.cfg.app_name}: Sicherung fehlgeschlagen"),
                               L(f"{d}: {e}. Results stay in the database, but without a copy.",
                                 f"{d}: {e}. Ergebnisse liegen weiter in der Datenbank, aber ohne Kopie."))

    def monthly_due(self, now: float) -> str | None:
        t = datetime.fromtimestamp(now, self.cfg.tz)
        if (t.day, t.hour, t.minute) < (self.cfg.report_day, *self.cfg.report_time):
            return None
        prev = (date(t.year, t.month, 1) - timedelta(days=1)).strftime("%Y-%m")
        if (self.store.get_meta("reports") or {}).get(prev):
            return None
        days = self.store.daily()
        if not days or min(days) > prev + "-31":
            return None
        return prev

    async def monthly(self, period: str, notify=True):
        now = self.clock.now()
        path, text = await asyncio.to_thread(write_monthly, self.cfg, self.store, self.summary, period, now)
        done = self.store.get_meta("reports") or {}
        done[period] = path.name
        self.store.set_meta("reports", done)
        if notify:
            self.store.enqueue(now, L(f"🔋 {self.cfg.app_name} monthly report", f"🔋 {self.cfg.app_name} Monatsbericht"),
                               text + L(f" – full report in the web UI (port {self.cfg.web_port}, Reports).",
                                        f" – ganzer Bericht in der Weboberfläche (Port {self.cfg.web_port}, Berichte)."),
                               False)
        return path, text

    # ------------------------------------------------------------------ loop
    async def run_forever(self):
        from .web import start_web
        runner = await start_web(self)
        try:
            await self.startup()
        except Exception:  # noqa: BLE001
            log.exception("start analysis failed – the service keeps running")
            self.errors += 1
            self.ready = True
        last_analysis = self.clock.now()
        last_day = datetime.fromtimestamp(self.clock.now(), self.cfg.tz).date()
        try:
            while True:
                await asyncio.sleep(self.cfg.mon.poll_interval)
                now = self.clock.now()
                try:
                    await self.poll_once()
                    if now - last_analysis >= 1800:
                        last_analysis = now
                        await self.analyse()
                        await self.publish()
                    elif now - self._last_publish >= 300:
                        await self.publish()
                    t = datetime.fromtimestamp(now, self.cfg.tz)
                    if t.date() != last_day and (t.hour, t.minute) >= (3, 20):
                        last_day = t.date()
                        await asyncio.to_thread(self.maintenance, now)
                    per = self.monthly_due(now)
                    if per:
                        await self.analyse()
                        await self.monthly(per)
                    await self.notifier.deliver(now)
                except Exception:  # noqa: BLE001
                    self.errors += 1
                    log.exception("error in the main loop")
        finally:
            await runner.cleanup()
            await self.ha.close()
