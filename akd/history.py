"""Import existing history: Prometheus (per minute), HA recorder (single states), HA long-term statistics (hourly)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiohttp

from .config import Config
from .store import Store

log = logging.getLogger("akd.history")
STATE_TO_NUM = {"on": 1.0, "off": 0.0}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def to_num(state):
    if state is None:
        return None
    s = str(state).strip().lower()
    if s in STATE_TO_NUM:
        return STATE_TO_NUM[s]
    try:
        return float(s)
    except ValueError:
        return None


async def prometheus_range(session, url, selector, start, end, step=60):
    out = []
    t = start
    while t < end:
        t2 = min(end, t + step * 10000)
        params = {"query": selector, "start": f"{t:.0f}", "end": f"{t2:.0f}", "step": f"{step:.0f}s"}
        async with session.get(f"{url}/api/v1/query_range", params=params) as r:
            js = await r.json(content_type=None)
        if js.get("status") != "success":
            raise RuntimeError(f"Prometheus: {js.get('error')}")
        for series in js["data"]["result"]:
            out += [(float(a), float(b)) for a, b in series["values"]]
        t = t2 + step
    out.sort()
    return out


async def backfill(cfg: Config, store: Store, ha, now: float, progress=print) -> dict:
    roles = cfg.entity_roles()
    aliases = cfg.history_aliases()
    rep = {"prometheus": {}, "recorder": {}, "hourly": {}}
    start = now - 400 * 86400
    # 1) Prometheus (HA exporter): per minute, as far back as its retention allows
    if cfg.prometheus_url:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
                async with s.get(f"{cfg.prometheus_url}/api/v1/query", params={"query": "time()"}) as r:
                    await r.json(content_type=None)
                pstart = now - 60 * 86400
                for key, ent in roles.items():
                    total = []
                    for e in [ent] + aliases.get(key, []):
                        sel = ('{entity="%s",__name__=~"homeassistant_(sensor|input_boolean|number)_.*",'
                               '__name__!~".*_(created|total)"}' % e)
                        if key == "throttle":
                            sel = 'homeassistant_input_boolean_state{entity="%s"}' % e
                        total += await prometheus_range(s, cfg.prometheus_url, sel, pstart, now, 60)
                    store.add_if_missing(key, total)
                    rep["prometheus"][key] = len(total)
                    progress(f"  Prometheus {key} ({ent}): {len(total)} minute values")
        except Exception as e:  # noqa: BLE001
            rep["prometheus_error"] = str(e)
            progress(f"  Prometheus not usable: {e}")
    if ha is not None:
        # 2) HA recorder: single states of the last days, fills gaps
        try:
            hist = await ha.history(list(roles.values()), iso(now - 12 * 86400), iso(now))
            for key, ent in roles.items():
                rows = []
                for st in (hist or {}).get(ent, []):
                    v = to_num(st.get("s"))
                    ts = st.get("lu") or st.get("lc")
                    if v is not None and ts is not None:
                        rows.append((float(ts), v))
                rows = _hold_minutes(sorted(rows), now, 11 * 60 if key.endswith("soc") else 4 * 60)
                store.add_if_missing(key, rows)
                rep["recorder"][key] = len(rows)
            progress(f"  HA recorder: {sum(rep['recorder'].values())} minute values added")
        except Exception as e:  # noqa: BLE001
            rep["recorder_error"] = str(e)
            progress(f"  HA recorder not readable: {e}")
        # 3) long-term statistics (hourly) since the beginning
        try:
            inv = {}
            for key, ent in roles.items():
                inv.setdefault(ent, []).append(key)
                for a in aliases.get(key, []):
                    inv.setdefault(a, []).append(key)
            res = await ha.statistics(list(inv), iso(start), iso(now), "hour")
            for ent, rows in (res or {}).items():
                vals = []
                for r in rows:
                    t = r["start"] / 1000 if isinstance(r["start"], (int, float)) else \
                        datetime.fromisoformat(r["start"]).timestamp()
                    vals.append((t, r.get("mean"), r.get("min"), r.get("max")))
                for key in inv.get(ent, []):
                    store.put_hourly(key, vals)
                    rep["hourly"][key] = rep["hourly"].get(key, 0) + len(vals)
            progress(f"  HA long-term statistics: {sum(rep['hourly'].values())} hourly values")
        except Exception as e:  # noqa: BLE001
            rep["statistics_error"] = str(e)
            progress(f"  HA long-term statistics not readable: {e}")
    store.set_meta("prelearned", {"at": now, "report": rep})
    return rep


def _hold_minutes(rows, end, hold):
    """The recorder stores changes only: repeat each value per minute for at most `hold` seconds
    (after that it counts as a gap – the recorder does not store equal values again)."""
    out = []
    for i, (ts, v) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else end
        t = ts
        while t < min(nxt, ts + hold):
            out.append((t, v))
            t += 60
    return out
