"""Web UI (overview, master/slave, measurement windows, self-monitoring, reports, dossier),
/metrics for Prometheus, /health for Docker, CSV export."""
from __future__ import annotations

import asyncio
import html
import io
import json
import zipfile
from datetime import datetime

from aiohttp import web

from . import __version__
from .charts import line_chart
from .dossier import export_tables, make_dossier, method_text
from .i18n import KIND, REASON, SOURCE, STATUS, WARRANTY, L, fmt_date, fmt_short, num, tr
from .report import capacity_chart, page

E = html.escape
CSS = """
.kpi{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}.sub{color:var(--muted);font-size:13px}
.st-ok{color:var(--ok)}.st-uncertain{color:var(--warn)}.st-no_data{color:var(--muted)}.st-crit{color:var(--bad)}
nav.sub a{margin-right:14px}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;font-size:13.5px}
button{font:inherit;padding:6px 12px;border-radius:9px;border:1px solid var(--line-2);
background:var(--surface-2);color:var(--text);cursor:pointer}
"""
REPORT_PREFIXES = ("report-", "bericht-")
DOSSIER_PREFIXES = ("warranty-dossier-", "garantie-dossier-")


def _nav() -> str:
    return (f"<nav class='tabs sub'><a href='/'>{L('Overview', 'Übersicht')}</a><a href='/master-slave'>Master/Slave</a>"
            f"<a href='/windows'>{L('Measurement windows', 'Messfenster')}</a>"
            f"<a href='/monitoring'>{L('Self-monitoring', 'Selbstüberwachung')}</a>"
            f"<a href='/reports'>{L('Reports &amp; dossiers', 'Berichte &amp; Dossiers')}</a>"
            f"<a href='/method'>{L('Method', 'Methode')}</a></nav>")


def build_app(svc) -> web.Application:
    app = web.Application()
    cfg, store = svc.cfg, svc.store

    def render(title, body):
        return web.Response(text=page(f"{title} · {cfg.app_name}", _nav() + body, cfg, CSS), content_type="text/html")

    def ts(t, f=fmt_short):
        return f(datetime.fromtimestamp(t, cfg.tz))

    async def index(_req):
        s = svc.summary
        if not s.get("scopes"):
            return render(L("Overview", "Übersicht"), "<div class='card'><p>" + L(
                "The first analysis is still running (history is being imported) – please reload in a few minutes.",
                "Die erste Auswertung läuft noch (Historie wird eingelesen) – bitte in ein paar Minuten neu laden.")
                + "</p></div>")
        tiles = []
        for scope, sc in s["scopes"].items():
            cur = sc.get("current") or {}
            g = sc.get("warranty") or {}
            trd = sc.get("trend") or {}
            st = cur.get("status", "no_data")
            val = f"{num(cur['cap_kwh'])} kWh" if st == "ok" else "–"
            detail = (f" · {L('range', 'Bereich')} {num(cur.get('lo'))}–{num(cur.get('hi'))} · SOH {num(cur.get('soh'), 0)} %"
                      if st == "ok" else f" · {E((cur.get('detail') or {}).get('reason', ''))}")
            tiles.append(
                f"<div class='card tile'><div class='tile-top'>{E(cfg.scope_name(scope))}</div><div class='kpi'>{val}</div>"
                f"<div class='sub st-{st}'>{E(tr(STATUS, st))}{detail}</div>"
                f"<div class='sub'>{cur.get('n', 0)} {L('windows', 'Messfenster')} · {L('nominal', 'Nenn')} "
                f"{num(cfg.nominal(scope), 3)} kWh · {L('full cycles', 'Vollzyklen')} {num((s.get('cycles') or {}).get(scope), 1)}</div>"
                f"<div class='sub'>Trend: {E((trd.get('forecast_80') or {}).get('text') or trd.get('text', '–'))}</div>"
                f"<div class='sub'>{L('Warranty', 'Garantie')}: {E(tr(WARRANTY, g.get('status')) or '–')} "
                f"{L('until', 'bis')} {E(str(g.get('end_effective', '–')))} · {L('manufacturer curve today', 'Herstellerkurve heute')} "
                f"{num(g.get('reference_soh'), 0)} %</div></div>")
        al = store.alerts("open")
        alerts = "".join(f"<li class='{'st-crit' if a['level'] == 'critical' else ''}'><b>{E(a['title'])}</b>: "
                         f"{E(a['message'])}</li>" for a in al) or \
            f"<li class='muted'>{L('No open alerts.', 'Keine offenen Meldungen.')}</li>"
        charts = "".join(f"<section class='card sec'><h2>{E(cfg.scope_name(sc))}</h2>"
                         f"{capacity_chart(cfg, sc, v.get('months') or [], v.get('warranty'))}</section>"
                         for sc, v in s["scopes"].items())
        waiting = L("pending", "wartet")
        msg = "".join(f"<li><span class='muted'>{ts(t)}</span> {E(ti)}: {E(m)}"
                      f"{'' if sent else f' <span class=pill>{waiting}</span>'}</li>"
                      for _i, t, ti, m, sent in store.messages(10)) or f"<li class='muted'>{L('None yet.', 'Noch keine.')}</li>"
        ha = L("connected", "verbunden") if svc.monitor.ha_connected else L("not reachable", "nicht erreichbar")
        last = s.get("time")
        head = (f"<div class='page-head'><h1>{E(cfg.app_name)}</h1><p class='muted'>Home Assistant {ha} · "
                f"{L('last analysis', 'letzte Auswertung')} {ts(last)} · Version {__version__}</p></div>" if last else "")
        body = head + (f"<section class='sec'><div class='grid cols-auto'>{''.join(tiles)}</div></section>"
                       f"<section class='card sec'><h2>{L('Open alerts', 'Offene Meldungen')}</h2><ul>{alerts}</ul></section>"
                       f"{charts}<section class='card sec'><h2>{L('Recent notifications', 'Letzte Benachrichtigungen')}</h2>"
                       f"<ul>{msg}</ul></section>")
        return render(L("Overview", "Übersicht"), body)

    async def master_slave(_req):
        ms = svc.summary.get("master_slave") or {}
        if not cfg.ms:
            return render("Master/Slave", f"<div class='card'>{L('Not configured.', 'Nicht konfiguriert.')}</div>")
        days = store.daily()
        ds = [d for d in sorted(days) if days[d].get("ms_diff_abs") is not None][-120:]
        lab = [(i, fmt_date_short(d)) for i, d in enumerate(ds)][:: max(1, len(ds) // 12)]
        um = ms.get("replug") or {}
        minute = lambda d: days[d].get("source") == "minute"  # noqa: E731
        chart = line_chart([
            {"name": L("SOC difference absolute (daily mean, minute data)", "SOC-Differenz Betrag (Tagesmittel, Minutenwerte)"),
             "points": [(i, days[d]["ms_diff_abs"] if minute(d) else None) for i, d in enumerate(ds)]},
            {"name": L("from hourly values (coarse)", "aus Stundenwerten (grob)"),
             "points": [(i, days[d]["ms_diff_abs"] if not minute(d) else None) for i, d in enumerate(ds)], "cls": "s4",
             "line": False},
            {"name": L("master − slave (signed)", "Master − Slave (Vorzeichen)"),
             "points": [(i, days[d].get("ms_diff_mean")) for i, d in enumerate(ds)], "cls": "s2", "dots": False}],
            y_label=L("percentage points", "Prozentpunkte"), x_labels=lab)
        wake = line_chart([{"name": L("slave wake-up delay (min)", "Aufwachverzug Slave (min)"),
                            "points": [(i, days[d].get("ms_wake_delay_min")) for i, d in enumerate(ds)], "cls": "s3"}],
                          y_label=L("minutes", "Minuten"), y_min=0, x_labels=lab)
        rate = line_chart([{"name": L("charge rate master (%/h)", "Laderate Master (%/h)"),
                            "points": [(i, days[d].get("ms_charge_rate_master_pph")) for i, d in enumerate(ds)]},
                           {"name": L("charge rate slave (%/h)", "Laderate Slave (%/h)"),
                            "points": [(i, days[d].get("ms_charge_rate_slave_pph")) for i, d in enumerate(ds)], "cls": "s2"}],
                          y_label="%/h", y_min=0, x_labels=lab)
        v = ms.get("comparison") or {}
        rows = "".join(
            f"<tr><td>{E(k['name'])}</td><td>{num(k.get('before'), 1)}</td><td>{num(k.get('after'), 1)}</td>"
            f"<td>{num(k.get('difference'), 1)}</td>"
            f"<td>{(num(k['range'][0], 1) + ' … ' + num(k['range'][1], 1)) if k.get('range') else '–'}</td>"
            f"<td>{num(k.get('p'), 3)}</td><td>{k.get('n_before', 0)}/{k.get('n_after', 0)}</td>"
            f"<td>{E(k.get('status', ''))}</td></tr>" for k in (v.get("metrics") or {}).values())
        wz = ms.get("worsening")
        table = ""
        if v:
            table = (f"<div class='tbl-wrap'><table><thead><tr><th>{L('Metric', 'Kennzahl')}</th><th>{L('before', 'vorher')}</th>"
                     f"<th>{L('after', 'nachher')}</th><th>{L('change', 'Änderung')}</th><th>{L('95 % range', '95-%-Bereich')}</th>"
                     f"<th>p</th><th>{L('days', 'Tage')}</th><th>{L('rating', 'Bewertung')}</th></tr></thead><tbody>{rows}"
                     f"</tbody></table></div><p><b>{E(v.get('summary', ''))}</b></p>")
            if v.get("season_adjusted_difference") is not None:
                sad = num(v["season_adjusted_difference"], 1)
                table += "<p class='muted'>" + L(f"Season-adjusted (days with similar solar yield): change {sad} percentage points.",
                                                 f"Saisonbereinigt (Tage mit ähnlichem Solarertrag): Änderung {sad} Prozentpunkte.") + "</p>"
        wtxt = ""
        if wz:
            worse = L("worsened", "verschlechtert") if wz["worse"] else L("no worsening", "keine Verschlechterung")
            wtxt = "<p>" + L(f"Last {cfg.ms.worsening_days} days Ø {num(wz['now'], 1)}, before {num(wz['before'], 1)} percentage points – {worse}.",
                             f"Letzte {cfg.ms.worsening_days} Tage Ø {num(wz['now'], 1)}, davor {num(wz['before'], 1)} Prozentpunkte – {worse}.") + "</p>"
        pval = f" (p = {num(um.get('p'), 3)})" if um.get("p") is not None else ""
        body = (f"<div class='page-head'><h1>Master/Slave</h1></div><section class='card sec'><h2>{L('Re-plug', 'Umstecken')}</h2>"
                f"<p>{E(um.get('text', L('not determined yet', 'noch nicht bestimmt')))}{pval}</p>{table}</section>"
                f"<section class='card sec'><h2>{L('SOC difference', 'SOC-Differenz')}</h2>{chart}{wtxt}</section>"
                f"<section class='card sec'><h2>{L('Waking up in the morning', 'Aufwachen am Morgen')}</h2>{wake}</section>"
                f"<section class='card sec'><h2>{L('Charge rate per unit', 'Laderate je Einheit')}</h2>{rate}<p class='muted'>" + L(
                    "Charging power is only measured for the whole system; per unit the SOC increase per hour in pure charging minutes is shown.",
                    "Die Ladeleistung wird nur gesamt gemessen; je Einheit ist hier die SOC-Zunahme pro Stunde in reinen Lademinuten gezeigt.")
                + "</p></section>")
        return render("Master/Slave", body)

    def fmt_date_short(d: str) -> str:
        return L(d[5:], d[8:10] + "." + d[5:7] + ".")

    async def windows(req):
        kind = req.query.get("kind")
        wins = (store.windows(kind=kind) if kind else store.windows())[-300:][::-1]
        rows = "".join(
            f"<tr><td>{ts(w['t0'], lambda d: d.strftime(L('%y-%m-%d %H:%M', '%d.%m.%y %H:%M')))}</td>"
            f"<td>{ts(w['t1'], lambda d: d.strftime('%H:%M'))}</td><td>{tr(KIND, w['kind'])}</td><td>{tr(SOURCE, w['source'])}</td>"
            f"<td>{num(w['soc0'], 0)} → {num(w['soc1'], 0)}</td><td>{num(w['e_wh'], 0)}</td><td>{num(w['cap_kwh'])}</td>"
            f"<td>{num((w['sigma_rel'] or 0) * 100, 1)} %</td><td>{'✓' if w['used'] else E(tr(REASON, w['reason']))}</td>"
            f"<td>{E(', '.join(f'{k} {num(v, 1)}' for k, v in w['deltas'].items() if k in cfg.units and v is not None))}</td></tr>"
            for w in wins)
        body = (f"<div class='page-head'><h1>{L('Measurement windows', 'Messfenster')}</h1><p class='muted'>"
                f"{L('Newest first, at most 300.', 'Neueste zuerst, höchstens 300.')} "
                f"<a href='/windows?kind=discharge'>{L('discharge only', 'nur Entladen')}</a> · "
                f"<a href='/windows?kind=charge'>{L('charge only', 'nur Laden')}</a> · "
                f"<a href='/export/windows.csv'>{L('all as CSV', 'alle als CSV')}</a></p></div>"
                f"<div class='card tbl-wrap'><table><thead><tr><th>{L('Start', 'Beginn')}</th><th>{L('End', 'Ende')}</th>"
                f"<th>{L('Kind', 'Art')}</th><th>{L('Source', 'Quelle')}</th><th>SOC %</th><th>Wh</th><th>kWh</th><th>±</th>"
                f"<th>{L('used', 'verwendet')}</th><th>{L('ΔSOC units', 'ΔSOC Einheiten')}</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></div>")
        return render(L("Measurement windows", "Messfenster"), body)

    async def monitoring(_req):
        mon = svc.monitor
        base = store.get_meta("entity_baseline") or {}
        rows = []
        for role, ent in mon.mapping().items():
            lv = mon.live.get(role) or {}
            last = store.last(role)
            age = (svc.clock.now() - last[0]) if last else None
            b = base.get(role) or {}
            rows.append(f"<tr><td>{E(role)}</td><td>{E(ent)}</td><td>{E(str(lv.get('raw', '–')))}</td>"
                        f"<td>{E(str(b.get('unit') or ''))}</td><td>{E(str(b.get('platform') or ''))}</td>"
                        f"<td>{'–' if age is None else f'{age / 60:.0f} min'}</td><td>{E(lv.get('state', '–'))}</td></tr>")
        rr, hr = store.raw_range(), store.hourly_range()
        ev = "".join(f"<li><span class='muted'>{ts(t)}</span> {E(k)}: {E(x)}</li>" for t, k, x in store.events(30))
        d = lambda t: ts(t, lambda x: fmt_date(x.date())) if t else "–"  # noqa: E731
        body = (f"<div class='page-head'><h1>{L('Self-monitoring', 'Selbstüberwachung')}</h1><p class='muted'>Home Assistant "
                f"{E(str(mon.ha_version or '?'))} · {L('minute values', 'Minutenwerte')} {d(rr[0])}–{d(rr[1])} ({rr[2] or 0}) · "
                f"{L('hourly statistics', 'Stundenstatistik')} {d(hr[0])}–{d(hr[1])} · {L('database', 'Datenbank')} "
                f"{store.size_bytes() / 1e6:.1f} MB</p></div><div class='card tbl-wrap'><table><thead><tr>"
                f"<th>{L('Role', 'Rolle')}</th><th>{L('Entity', 'Entität')}</th><th>{L('Value', 'Wert')}</th>"
                f"<th>{L('Unit', 'Einheit')}</th><th>Integration</th><th>{L('last fresh value', 'letzter frischer Wert')}</th>"
                f"<th>{L('State', 'Zustand')}</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
                f"<section class='card sec'><h2>{L('Events', 'Ereignisse')}</h2><ul>{ev}</ul></section>")
        return render(L("Self-monitoring", "Selbstüberwachung"), body)

    async def reports(_req):
        rep = sorted((p for p in cfg.report_dir.glob("*.html") if p.name.startswith(REPORT_PREFIXES)), reverse=True) \
            if cfg.report_dir.exists() else []
        dos = sorted((p for p in cfg.dossier_dir.glob("*") if p.name.startswith(DOSSIER_PREFIXES)), reverse=True) \
            if cfg.dossier_dir.exists() else []
        opts = "".join(f"<option value='{s}'>{E(cfg.scope_name(s))}</option>" for s in cfg.scopes())
        when = f"{cfg.report_time[0]:02d}:{cfg.report_time[1]:02d}"
        body = (f"<div class='page-head'><h1>{L('Reports &amp; dossiers', 'Berichte &amp; Dossiers')}</h1></div>"
                f"<section class='card sec'><h2>{L('Warranty dossier', 'Garantie-Dossier')}</h2><p>" + L(
                    "PDF with capacity history, method, measurement windows and temperature proof, plus a ZIP with all raw data (CSV) and checksums.",
                    "PDF mit Kapazitätsverlauf, Methode, Messfenstern und Temperaturnachweis, dazu ein ZIP mit allen Rohdaten (CSV) und Prüfsummen.")
                + f"</p><form method='post' action='/dossier'><select name='scope'>{opts}</select> "
                f"<button>{L('Create dossier', 'Dossier erstellen')}</button></form><ul>"
                + "".join(f"<li><a href='/dossiers/{p.name}'>{p.name}</a></li>" for p in dos[:30])
                + f"</ul></section><section class='card sec'><h2>{L('Monthly reports', 'Monatsberichte')}</h2><ul>"
                + ("".join(f"<li><a href='/reports/{p.name}'>{p.stem.split('-', 1)[1]}</a></li>" for p in rep) or
                   f"<li class='muted'>{L(f'None yet (created on day {cfg.report_day} at {when}).', f'Noch keiner (erscheint am {cfg.report_day}. um {when}).')}</li>")
                + f"</ul><form method='post' action='/report'><input name='month' placeholder='YYYY-MM' size='8'> "
                f"<button>{L('Create report now', 'Bericht jetzt erstellen')}</button></form></section>"
                f"<section class='card sec'><h2>Export</h2><ul><li><a href='/export/all.zip'>{L('Everything as ZIP (CSV)', 'Alles als ZIP (CSV)')}</a></li>"
                f"<li><a href='/export/windows.csv'>{L('Measurement windows', 'Messfenster')}</a></li>"
                f"<li><a href='/export/estimates.csv'>{L('Estimates', 'Schätzungen')}</a></li>"
                f"<li><a href='/export/daily.csv'>{L('Daily values', 'Tageswerte')}</a></li></ul></section>")
        return render(L("Reports", "Berichte"), body)

    async def method(_req):
        txt = "".join(f"<p>{E(p)}</p>" for p in method_text(cfg))
        return render(L("Method", "Methode"), f"<div class='page-head'><h1>{L('How it is calculated', 'So wird gerechnet')}"
                                              f"</h1></div><div class='card'>{txt}</div>")

    async def dossier(req):
        data = await req.post()
        scope = data.get("scope", "system")
        if scope not in cfg.scopes():
            raise web.HTTPBadRequest(text="unknown scope")
        pdf, _z = await asyncio.to_thread(make_dossier, cfg, store, svc.summary, scope, svc.clock.now())
        raise web.HTTPSeeOther(f"/dossiers/{pdf.name}")

    async def report_post(req):
        data = await req.post()
        m = (data.get("month") or data.get("monat") or "").strip() or \
            datetime.fromtimestamp(svc.clock.now(), cfg.tz).strftime("%Y-%m")
        try:
            datetime.strptime(m, "%Y-%m")
        except ValueError:
            raise web.HTTPBadRequest(text="month as YYYY-MM")
        path, _t = await svc.monthly(m, notify=False)
        raise web.HTTPSeeOther(f"/reports/{path.name}")

    def _file(folder, name, ctype):
        if "/" in name or "\\" in name or name.startswith("."):
            raise web.HTTPNotFound()
        p = folder / name
        if not p.exists():
            raise web.HTTPNotFound()
        return web.Response(body=p.read_bytes(), content_type=ctype,
                            headers={"Content-Disposition": f"inline; filename={name}"} if ctype != "text/html" else None)

    async def report_file(req):
        name = req.match_info["name"]
        if not name.startswith(REPORT_PREFIXES):
            raise web.HTTPNotFound()
        return _file(cfg.report_dir, name, "text/html")

    async def dossier_file(req):
        name = req.match_info["name"]
        return _file(cfg.dossier_dir, name, "application/pdf" if name.endswith(".pdf") else "application/zip")

    async def export(req):
        name = {"alles.zip": "all.zip", "fenster.csv": "windows.csv", "schaetzungen.csv": "estimates.csv",
                "tageswerte.csv": "daily.csv"}.get(req.match_info["name"], req.match_info["name"])
        tables = await asyncio.to_thread(export_tables, cfg, store, None if name == "all.zip" else 90)
        if name == "all.zip":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for k, v in tables.items():
                    z.writestr(k, v)
            return web.Response(body=buf.getvalue(), content_type="application/zip",
                                headers={"Content-Disposition": "attachment; filename=battery-doctor-export.zip"})
        if name not in tables:
            raise web.HTTPNotFound()
        return web.Response(body=tables[name], content_type="text/csv",
                            headers={"Content-Disposition": f"attachment; filename={name}"})

    async def metrics_h(_req):
        return web.Response(text=svc.metrics_text(), content_type="text/plain")

    async def health(_req):
        return web.json_response({"ok": True, "ready": svc.ready, "ha_connected": svc.monitor.ha_connected,
                                  "version": __version__})

    async def status(_req):
        s = svc.summary
        out = {"version": __version__, "ha_connected": svc.monitor.ha_connected, "time": s.get("time"),
               "storages": {sc: {"capacity": v.get("current"), "trend": v.get("trend"), "warranty": v.get("warranty")}
                            for sc, v in (s.get("scopes") or {}).items()},
               "cycles": s.get("cycles"),
               "master_slave": {k: v for k, v in (s.get("master_slave") or {}).items() if k != "recent_days"},
               "alerts": store.alerts("open")}
        return web.Response(text=json.dumps(out, ensure_ascii=False, indent=1, default=str), content_type="application/json")

    r = app.router
    r.add_get("/", index)
    r.add_get("/master-slave", master_slave)
    for path, h in (("/windows", windows), ("/fenster", windows), ("/monitoring", monitoring),
                    ("/ueberwachung", monitoring), ("/reports", reports), ("/berichte", reports),
                    ("/method", method), ("/methode", method)):
        r.add_get(path, h)
    r.add_post("/dossier", dossier)
    r.add_post("/report", report_post)
    r.add_post("/bericht", report_post)
    r.add_get("/reports/{name}", report_file)
    r.add_get("/berichte/{name}", report_file)
    r.add_get("/dossiers/{name}", dossier_file)
    r.add_get("/export/{name}", export)
    r.add_get("/metrics", metrics_h)
    r.add_get("/health", health)
    r.add_get("/api/status", status)
    return app


async def start_web(svc, host="0.0.0.0"):
    runner = web.AppRunner(build_app(svc), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, svc.cfg.web_port)
    await site.start()
    return runner
