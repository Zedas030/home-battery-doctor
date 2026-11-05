"""Access to Home Assistant. Read-only on purpose – the only things allowed are notifications and setting the
tool's own display sensors (sensor.<prefix>_*). Everything else is refused before any network access."""
from __future__ import annotations

import itertools
import json

import aiohttp

ALLOWED_SERVICES = {"notify": None, "persistent_notification": {"create", "dismiss"}}


class ReadOnlyViolation(RuntimeError):
    pass


class HAError(RuntimeError):
    pass


class HAClient:
    def __init__(self, url: str, token: str, prefix: str, timeout: float = 20.0):
        self.url = url.rstrip("/")
        self.token = token
        self.prefix = prefix
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._s: aiohttp.ClientSession | None = None
        self._ids = itertools.count(1)

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def session(self):
        if self._s is None or self._s.closed:
            self._s = aiohttp.ClientSession(timeout=self.timeout)
        return self._s

    async def close(self):
        if self._s is not None and not self._s.closed:
            await self._s.close()

    async def config(self) -> dict:
        s = await self.session()
        async with s.get(f"{self.url}/api/config", headers=self.headers) as r:
            if r.status != 200:
                raise HAError(f"GET /api/config: HTTP {r.status}")
            return await r.json()

    async def services(self) -> list[dict]:
        s = await self.session()
        async with s.get(f"{self.url}/api/services", headers=self.headers) as r:
            if r.status != 200:
                raise HAError(f"GET /api/services: HTTP {r.status}")
            return await r.json()

    async def states(self) -> list[dict]:
        s = await self.session()
        async with s.get(f"{self.url}/api/states", headers=self.headers) as r:
            if r.status == 401:
                raise HAError("token rejected (401) – check HA_TOKEN in .env")
            if r.status != 200:
                raise HAError(f"GET /api/states: HTTP {r.status}")
            return await r.json()

    async def set_state(self, entity_id: str, state, attributes: dict):
        if not entity_id.startswith(f"sensor.{self.prefix}_") and entity_id != f"sensor.{self.prefix}":
            raise ReadOnlyViolation(f"only own sensors may be set, not {entity_id}")
        s = await self.session()
        async with s.post(f"{self.url}/api/states/{entity_id}", headers=self.headers,
                          data=json.dumps({"state": state, "attributes": attributes}, default=str)) as r:
            if r.status not in (200, 201):
                raise HAError(f"POST {entity_id}: HTTP {r.status}")

    async def call_service(self, domain: str, service: str, data: dict):
        allowed = ALLOWED_SERVICES.get(domain, False)
        if allowed is False or (allowed is not None and service not in allowed):
            raise ReadOnlyViolation(f"service {domain}.{service} is blocked: this tool never controls anything")
        s = await self.session()
        async with s.post(f"{self.url}/api/services/{domain}/{service}", headers=self.headers,
                          data=json.dumps(data)) as r:
            if r.status not in (200, 201):
                raise HAError(f"{domain}.{service}: HTTP {r.status} {await r.text()}")

    async def ws(self, payload: dict, timeout: float = 300):
        """A single WebSocket command (read-only commands only)."""
        t = payload.get("type", "")
        if t == "call_service" or t.startswith(("config/entity_registry/update", "config/entity_registry/remove")) \
                or "save" in t or "delete" in t or "update" in t or "create" in t:
            raise ReadOnlyViolation(f"WebSocket command {t} is blocked")
        s = await self.session()
        ws_url = self.url.replace("http", "ws", 1) + "/api/websocket"
        async with s.ws_connect(ws_url, max_msg_size=256 * 1024 * 1024,
                                timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
            msg = await ws.receive_json(timeout=30)
            if msg.get("type") != "auth_required":
                raise HAError(f"unexpected answer: {msg}")
            await ws.send_json({"type": "auth", "access_token": self.token})
            msg = await ws.receive_json(timeout=30)
            if msg.get("type") != "auth_ok":
                raise HAError("login to Home Assistant failed (check the token)")
            mid = next(self._ids)
            await ws.send_json({"id": mid, **payload})
            while True:
                msg = await ws.receive_json(timeout=timeout)
                if msg.get("id") == mid and msg.get("type") == "result":
                    if not msg.get("success"):
                        raise HAError(str(msg.get("error")))
                    return msg.get("result")

    async def entity_registry(self) -> list[dict]:
        return await self.ws({"type": "config/entity_registry/list"})

    async def statistics(self, ids, start_iso, end_iso, period="hour"):
        return await self.ws({"type": "recorder/statistics_during_period", "start_time": start_iso,
                              "end_time": end_iso, "statistic_ids": list(ids), "period": period,
                              "types": ["mean", "min", "max"]})

    async def history(self, ids, start_iso, end_iso):
        return await self.ws({"type": "history/history_during_period", "start_time": start_iso, "end_time": end_iso,
                              "entity_ids": list(ids), "minimal_response": True, "no_attributes": True,
                              "significant_changes_only": False})
