"""Fake Home Assistant (REST + WebSocket) and Prometheus for the end-to-end test."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from aiohttp import WSMsgType, web

TOKEN = "test-token"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class FakeHA:
    def __init__(self):
        self.states: dict[str, dict] = {}
        self.registry: list[dict] = []
        self.service_calls: list[tuple[str, str, dict]] = []
        self.posted: dict[str, dict] = {}
        self.ws_commands: list[str] = []
        self.version = "2026.10.4"
        self.runner = None

    def set(self, eid, state, unit=None, uid=None, platform="mqtt"):
        now = datetime.now(timezone.utc).timestamp()
        self.states[eid] = {"entity_id": eid, "state": str(state),
                            "attributes": {"unit_of_measurement": unit} if unit else {},
                            "last_changed": iso(now), "last_updated": iso(now), "last_reported": iso(now)}
        if not any(e["entity_id"] == eid for e in self.registry):
            self.registry.append({"entity_id": eid, "unique_id": uid or f"uid-{eid}", "platform": platform,
                                  "config_entry_id": "ce1"})

    def rename(self, old, new):
        st = self.states.pop(old)
        st["entity_id"] = new
        self.states[new] = st
        for e in self.registry:
            if e["entity_id"] == old:
                e["entity_id"] = new

    def notifications(self):
        return [d for dom, _s, d in self.service_calls if dom == "notify"]

    def _auth(self, req):
        if req.headers.get("Authorization") != f"Bearer {TOKEN}":
            raise web.HTTPUnauthorized()

    async def h_states(self, req):
        self._auth(req)
        now = iso(datetime.now(timezone.utc).timestamp())
        for s in self.states.values():
            s["last_reported"] = now
        return web.json_response(list(self.states.values()))

    async def h_post_state(self, req):
        self._auth(req)
        self.posted[req.match_info["eid"]] = await req.json()
        return web.json_response({}, status=200)

    async def h_service(self, req):
        self._auth(req)
        self.service_calls.append((req.match_info["dom"], req.match_info["svc"], await req.json()))
        return web.json_response([])

    async def h_services(self, req):
        self._auth(req)
        return web.json_response([{"domain": "notify", "services": {"test": {}}}])

    async def h_config(self, req):
        self._auth(req)
        return web.json_response({"version": self.version})

    async def h_ws(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        await ws.send_json({"type": "auth_required"})
        msg = await ws.receive_json()
        if msg.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": self.version})
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                break
            d = json.loads(m.data)
            self.ws_commands.append(d.get("type"))
            res = {"config/entity_registry/list": self.registry, "recorder/statistics_during_period": {},
                   "history/history_during_period": {}}.get(d.get("type"), None)
            await ws.send_json({"id": d["id"], "type": "result", "success": True, "result": res})
        return ws

    async def start(self):
        app = web.Application()
        app.router.add_get("/api/states", self.h_states)
        app.router.add_post("/api/states/{eid}", self.h_post_state)
        app.router.add_post("/api/services/{dom}/{svc}", self.h_service)
        app.router.add_get("/api/services", self.h_services)
        app.router.add_get("/api/config", self.h_config)
        app.router.add_get("/api/websocket", self.h_ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def stop(self):
        await self.runner.cleanup()


class FakeProm:
    """Serves the simulation data for {entity="..."} (role via a mapping entity -> role)."""

    def __init__(self, data: dict, roles: dict):
        self.data = data
        self.by_entity = {ent: role for role, ent in roles.items()}
        self.runner = None

    async def h_query(self, _req):
        return web.json_response({"status": "success", "data": {"resultType": "scalar", "result": [0, "0"]}})

    async def h_range(self, req):
        q = req.query["query"]
        m = re.search(r'entity="([^"]+)"', q)
        role = self.by_entity.get(m.group(1)) if m else None
        start, end = float(req.query["start"]), float(req.query["end"])
        vals = [[ts, str(v)] for ts, v in self.data.get(role, []) if start <= ts <= end]
        res = [{"metric": {"entity": m.group(1)}, "values": vals}] if vals else []
        return web.json_response({"status": "success", "data": {"resultType": "matrix", "result": res}})

    async def start(self):
        app = web.Application()
        app.router.add_get("/api/v1/query", self.h_query)
        app.router.add_get("/api/v1/query_range", self.h_range)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def stop(self):
        await self.runner.cleanup()
