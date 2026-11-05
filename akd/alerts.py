"""Alerts: notify once, keep open, all-clear; quiet hours for non-urgent ones; delivery via a HA notify service."""
from __future__ import annotations

import logging
from datetime import datetime

from .config import Config
from .store import Store

log = logging.getLogger("akd.alerts")


class Alerts:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.silent = False  # do not send anything while importing history

    def raise_(self, now, key, title, message, level="warning", urgent=False, renotify_after=None) -> bool:
        """Open an alert. Notifies only when opening (or again after renotify_after seconds)."""
        a = self.store.alert(key)
        if a and a["state"] == "open":
            notify = renotify_after is not None and now - a["last"] >= renotify_after
            self.store.put_alert(key, a["first"], now if notify else a["last"], "open", level, title, message)
            if notify and not self.silent:
                self.store.enqueue(now, title, message, urgent, {"tag": key})
            return notify
        self.store.put_alert(key, now, now, "open", level, title, message)
        if not self.silent:
            self.store.enqueue(now, title, message, urgent, {"tag": key})
        self.store.event(now, "alert", f"{title}: {message}")
        return True

    def clear(self, now, key, message=None):
        a = self.store.alert(key)
        if a and a["state"] == "open":
            self.store.put_alert(key, a["first"], now, "done", a["level"], a["title"], a["message"])
            if message and not self.silent:
                self.store.enqueue(now, "✅ " + a["title"], message, False, {"tag": key})
            self.store.event(now, "all_clear", f"{a['title']}: {message or 'done'}")

    def is_open(self, key) -> bool:
        a = self.store.alert(key)
        return bool(a and a["state"] == "open")

    def open_alerts(self):
        return self.store.alerts("open")


def in_quiet_hours(cfg: Config, ts: float) -> bool:
    (h1, m1), (h2, m2) = cfg.mon.quiet_hours
    t = datetime.fromtimestamp(ts, cfg.tz)
    cur = t.hour * 60 + t.minute
    a, b = h1 * 60 + m1, h2 * 60 + m2
    return (a <= cur or cur < b) if a > b else (a <= cur < b)


class Notifier:
    def __init__(self, cfg: Config, store: Store, ha):
        self.cfg, self.store, self.ha = cfg, store, ha
        self.sent = 0
        self.failed = 0

    async def deliver(self, now: float) -> int:
        if self.ha is None or not self.cfg.notify:
            return 0
        domain, service = self.cfg.notify.split(".", 1)
        quiet = in_quiet_hours(self.cfg, now)
        n = 0
        for oid, _ts, title, message, urgent, _data in self.store.pending():
            if quiet and not urgent:
                continue
            payload = {"title": title, "message": message}
            if service.startswith("mobile_app"):
                payload["data"] = {"group": self.cfg.sensor_prefix, "tag": self.cfg.sensor_prefix + str(oid)}
                if urgent:
                    payload["data"]["push"] = {"interruption-level": "time-sensitive"}
            try:
                await self.ha.call_service(domain, service, payload)
                if self.cfg.persistent_too:
                    await self.ha.call_service("persistent_notification", "create",
                                               {"title": title, "message": message,
                                                "notification_id": f"{self.cfg.sensor_prefix}_{oid}"})
            except Exception as e:  # noqa: BLE001
                self.store.mark_attempt(oid)
                self.failed += 1
                log.warning("notification %s not delivered (%s) – retrying later", oid, e)
                break
            self.store.mark_sent(oid, now)
            self.sent += 1
            n += 1
        return n
