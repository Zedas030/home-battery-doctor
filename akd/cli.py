"""Command line: run | status | prelearn | analyze | report [YYYY-MM] | dossier [scope] | selftest | synthetic |
reset-baseline. The German command names of version 1.0 still work as aliases."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

from .config import ConfigError, load_config
from .i18n import L

ALIASES = {"vorlernen": "prelearn", "analyse": "analyze", "bericht": "report", "selbsttest": "selftest",
           "synthetik": "synthetic", "basis-neu": "reset-baseline"}


def _cfg(require_token=True):
    return load_config(os.environ.get("AKD_CONFIG", "/config/config.yaml"), require_token=require_token)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ALIASES:
        argv[0] = ALIASES[argv[0]]
    ap = argparse.ArgumentParser(prog="battery-doctor", description="Battery Doctor – health of home batteries (read-only)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="start the service (default)")
    sub.add_parser("status", help="short status from the database")
    sub.add_parser("prelearn", help="import the history again and re-analyse everything")
    sub.add_parser("analyze", help="re-analyse all data (e.g. after changing estimation parameters)")
    b = sub.add_parser("report", help="create a monthly report")
    b.add_argument("month", nargs="?")
    d = sub.add_parser("dossier", help="create a warranty dossier (PDF + ZIP)")
    d.add_argument("scope", nargs="?", default="system")
    sub.add_parser("selftest", help="check connection, entities and notify service (changes nothing)")
    sub.add_parser("synthetic", help="acceptance proof: estimation on synthetic batteries with known capacity")
    sub.add_parser("reset-baseline", help="re-learn the remembered units/integrations of the entities")
    a = ap.parse_args(argv)
    if a.cmd == "synthetic":
        from .synthetic import main as synth_main
        return synth_main()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = _cfg(require_token=a.cmd not in ("status",))
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    from .app import Service
    svc = Service(cfg)
    cmd = a.cmd or "run"
    if cmd == "run":
        asyncio.run(svc.run_forever())
    elif cmd == "status":
        from .report import cap_text
        svc.analyzer.run(time.time(), full=False)
        s = svc.summary
        for sc, v in (s.get("scopes") or {}).items():
            trd = v.get("trend") or {}
            g = v.get("warranty") or {}
            print(f"{cfg.scope_name(sc)}: {cap_text(v.get('current'))}")
            print(f"   Trend: {(trd.get('forecast_80') or {}).get('text') or trd.get('text')}")
            print(f"   {L('Warranty', 'Garantie')}: {g.get('status')} {'; '.join(g.get('notes') or [])}")
        ms = s.get("master_slave") or {}
        if ms:
            print(f"{L('Re-plug', 'Umstecken')}: {(ms.get('replug') or {}).get('text')}")
            print(f"{L('Comparison', 'Vergleich')}: {(ms.get('comparison') or {}).get('summary')}")
        for al in svc.store.alerts("open"):
            print(f"{L('OPEN', 'OFFEN')}: {al['title']}: {al['message']}")
    elif cmd in ("prelearn", "analyze"):
        async def go():
            if cmd == "prelearn":
                from .history import backfill
                print(json.dumps(await backfill(cfg, svc.store, svc.ha, time.time()), indent=1, ensure_ascii=False))
            svc.alerts.silent = True
            svc.analyzer.run(time.time(), full=True)
            await svc.ha.close()
        asyncio.run(go())
        print(L("Re-analysis done.", "Neuauswertung fertig."))
    elif cmd == "report":
        async def go():
            from datetime import datetime
            svc.analyzer.run(time.time(), full=False)
            p, text = await svc.monthly(a.month or datetime.now(cfg.tz).strftime("%Y-%m"), notify=False)
            print(p)
            print(text)
        asyncio.run(go())
    elif cmd == "dossier":
        from .dossier import make_dossier
        if a.scope not in cfg.scopes():
            print(f"unknown scope {a.scope!r}, choose from {cfg.scopes()}", file=sys.stderr)
            return 2
        svc.analyzer.run(time.time(), full=False)
        pdf, z = make_dossier(cfg, svc.store, svc.summary, a.scope, time.time())
        print(pdf)
        print(z)
    elif cmd == "reset-baseline":
        svc.store.set_meta("entity_baseline", {})
        svc.store.set_meta("renamed", {})
        print(L("Baseline cleared – it is learned again at the next poll.",
                "Basis gelöscht – wird bei der nächsten Abfrage neu erfasst."))
    elif cmd == "selftest":
        return asyncio.run(selftest(svc))
    return 0


async def selftest(svc) -> int:
    cfg, ok = svc.cfg, True
    try:
        conf = await svc.ha.config()
        print(f"OK  Home Assistant {conf.get('version')} {L('reachable', 'erreichbar')}")
        states = {s["entity_id"]: s for s in await svc.ha.states()}
        for role, ent in cfg.entity_roles().items():
            st = states.get(ent)
            if not st:
                ok = False
                print(f"{L('MISSING', 'FEHLT')}  {role}: {ent}")
            else:
                print(f"OK  {role}: {ent} = {st['state']} {st['attributes'].get('unit_of_measurement') or ''}")
        if cfg.notify:
            dom, srv = cfg.notify.split(".", 1)
            svcs = {d["domain"]: set(d["services"]) for d in await svc.ha.services()}
            if srv in svcs.get(dom, set()):
                print(f"OK  {L('notify service', 'Meldedienst')} {cfg.notify}")
            else:
                ok = False
                print(f"{L('MISSING', 'FEHLT')}  {L('notify service', 'Meldedienst')} {cfg.notify}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"{L('ERROR', 'FEHLER')}  {e}")
    finally:
        await svc.ha.close()
    print(L("self-test", "Selbsttest"), L("passed", "bestanden") if ok else L("FAILED", "NICHT bestanden"))
    return 0 if ok else 1
