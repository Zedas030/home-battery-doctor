"""Monthly report (HTML, kept permanently) and the short version for the push notification."""
from __future__ import annotations

import html
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__
from .charts import CHART_CSS, line_chart
from .config import Config
from .i18n import KIND, REASON, WARRANTY, L, fmt_dt, fmt_short, lang, month_name, num, tr
from .store import Store

THEME = (Path(__file__).parent / "static" / "homelab-theme.css")


def period_name(period: str) -> str:
    y, m = map(int, period.split("-"))
    return month_name(m, y)


def cap_text(e: dict | None) -> str:
    if not e or e.get("status") == "no_data":
        return L("no data", "keine Daten")
    if e.get("status") != "ok":
        return L("no reliable statement", "keine verlässliche Aussage") + f" ({(e.get('detail') or {}).get('reason', '')})"
    return (f"{num(e['cap_kwh'])} kWh ({L('range', 'Bereich')} {num(e['lo'])}–{num(e['hi'])}, "
            f"SOH {num(e.get('soh'), 0)} %)")


def capacity_chart(cfg: Config, scope: str, monthly: list[dict], warranty: dict | None) -> str:
    nom = cfg.nominal(scope)
    pts, unsure, labels = [], [], []
    for e in sorted(monthly, key=lambda e: e["period"]):
        y, m = map(int, e["period"].split("-"))
        x = y * 12 + m
        labels.append((x, f"{m:02d}/{str(y)[2:]}"))
        if e.get("cap_kwh") and e["status"] == "ok":
            pts.append((x, e["cap_kwh"] / nom * 100, e["lo"] / nom * 100, e["hi"] / nom * 100))
        elif e.get("cap_kwh") and (e.get("detail") or {}).get("uncertainty_relative", 1) <= 0.15:
            unsure.append((x, e["cap_kwh"] / nom * 100))   # very uncertain values are not shown at all
    series = [{"name": L("estimate (SOH %) with 95 % range", "Schätzung (SOH %) mit 95-%-Bereich"), "points": pts}]
    if unsure:
        series.append({"name": L("uncertain (too few/too imprecise windows)", "unsicher (zu wenige/zu ungenaue Fenster)"),
                       "points": unsure, "line": False, "cls": "s4"})
    if warranty and warranty.get("reference_soh") and labels:
        series.append({"name": L("manufacturer warranty curve (today)", "Garantiekurve des Herstellers (heute)"),
                       "points": [(labels[-1][0], warranty["reference_soh"])], "line": False, "cls": "s3"})
    return line_chart(series, y_label="SOH %", y_min=40, y_max=105,
                      hlines=[(80, "80 %"), (cfg.warranty.min_remaining_percent, L("warranty limit", "Garantiegrenze"))],
                      x_labels=labels[:: max(1, len(labels) // 12)])


def page(title: str, body: str, cfg: Config, extra_css: str = "") -> str:
    theme = THEME.read_text(encoding="utf-8") if THEME.exists() else ""
    home = (f"<a class='btn cross home-btn' href='{html.escape(cfg.home_url)}'><span class='lbl'>"
            f"{html.escape(cfg.home_label)}</span></a>") if cfg.home_url else ""
    return (f"<!doctype html><html lang='{lang()}' data-theme='dark'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><meta name='color-scheme' content='light dark'>"
            f"<title>{html.escape(title)}</title><style>{theme}{CHART_CSS}{extra_css}</style>"
            "<script>(function(){var t;try{t=localStorage.getItem('hl-theme')}catch(e){}if(t!=='light'&&t!=='dark')"
            "t=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';document.documentElement.dataset.theme=t})()"
            "</script></head><body><header class='top'><a class='brand' href='/'><span class='brand-mark'>🔋</span>"
            f"<span><span class='brand-name'>{html.escape(cfg.app_name)}</span><span class='brand-sub'>"
            f"{L('Health of home batteries', 'Gesundheit der Heimspeicher')}</span>"
            f"</span></a><div class='top-right'>{home}</div></header><main class='content'>{body}"
            f"<footer class='foot'>{html.escape(cfg.app_name)} {__version__} · "
            f"{L('read-only – controls nothing', 'nur lesend – steuert keine Speicher')}</footer></main></body></html>")


def build_monthly(cfg: Config, store: Store, summary: dict, period: str, now: float) -> tuple[str, str]:
    """(html, short text) for a month 'YYYY-MM'."""
    y, m = map(int, period.split("-"))
    d0 = date(y, m, 1)
    d1 = (d0 + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    t0 = datetime.combine(d0, datetime.min.time(), cfg.tz).timestamp()
    t1 = datetime.combine(d1 + timedelta(days=1), datetime.min.time(), cfg.tz).timestamp()
    days = store.daily(d0.isoformat(), d1.isoformat())
    wins = store.windows(t0=t0, t1=t1)
    e = html.escape
    title = L(f"Monthly report {period_name(period)}", f"Monatsbericht {period_name(period)}")
    parts = [f"<div class='page-head'><h1>{title}</h1>"
             f"<p class='muted'>{L('created', 'erstellt')} {fmt_dt(datetime.fromtimestamp(now, cfg.tz))}</p></div>"]
    short, tiles = [], []
    for scope in cfg.scopes():
        est = (store.estimates(scope, period) or [None])[0]
        sc = (summary.get("scopes") or {}).get(scope, {})
        g = sc.get("warranty") or {}
        cyc_month = (sum(v.get("e_discharge_wh") or 0 for v in days.values()) / 1000 / cfg.nominal_total_kwh
                     if scope == "system" else sum(v.get(f"{scope}_down_percent") or 0 for v in days.values()) / 100)
        tiles.append(f"<div class='tile card'><div class='tile-top'>{e(cfg.scope_name(scope))}</div>"
                     f"<div class='tile-val'>{num(est['cap_kwh']) + ' kWh' if est and est['status'] == 'ok' else '–'}</div>"
                     f"<div class='tile-sub'>{e(cap_text(est))}<br>{L('full cycles this month', 'Vollzyklen im Monat')} "
                     f"{num(cyc_month, 1)}, {L('total', 'gesamt')} {num((summary.get('cycles') or {}).get(scope), 1)} · "
                     f"{L('warranty', 'Garantie')}: {e(tr(WARRANTY, g.get('status')) or '–')}</div></div>")
        if scope == "system":
            short.append(f"{L('System', 'System')} {cap_text(est)}")
            if est and est.get("eff"):
                short.append(f"{L('efficiency', 'Wirkungsgrad')} {num(est['eff'] * 100, 0)} %")
        elif est and est["status"] == "ok":
            short.append(f"{cfg.units[scope].name.split(' (')[0]} {num(est['cap_kwh'])} kWh")
    parts.append(f"<section class='sec'><div class='grid cols-auto'>{''.join(tiles)}</div></section>")
    for scope in cfg.scopes():
        sc = (summary.get("scopes") or {}).get(scope, {})
        trd = sc.get("trend") or {}
        p80 = (trd.get("forecast_80") or {}).get("text") or trd.get("text", "")
        parts.append(f"<section class='sec card'><h2>{e(cfg.scope_name(scope))}: {L('capacity history', 'Kapazitätsverlauf')}</h2>"
                     f"{capacity_chart(cfg, scope, sc.get('months') or [], sc.get('warranty'))}"
                     f"<p>Trend: {e(p80)}</p>"
                     f"<p class='muted'>{e('; '.join((sc.get('warranty') or {}).get('notes') or []))}</p></section>")
    used = [w for w in wins if w["used"]]
    rej = Counter(tr(REASON, w["reason"]) for w in wins if not w["used"])
    parts.append(f"<section class='sec card'><h2>{L('Measurement windows', 'Messfenster')}</h2><p>"
                 f"{L(f'{len(used)} clean windows, {sum(rej.values())} rejected', f'{len(used)} saubere Fenster, {sum(rej.values())} verworfen')}"
                 f" ({e(', '.join(f'{k}: {v}' for k, v in rej.most_common()))}).</p>"
                 f"<div class='tbl-wrap'><table><thead><tr><th>{L('Start', 'Beginn')}</th><th>{L('Kind', 'Art')}</th>"
                 f"<th>SOC</th><th>{L('Energy', 'Energie')}</th><th>{L('Capacity', 'Kapazität')}</th><th>±</th>"
                 "</tr></thead><tbody>" +
                 "".join(f"<tr><td>{fmt_short(datetime.fromtimestamp(w['t0'], cfg.tz))}</td><td>{tr(KIND, w['kind'])}</td>"
                         f"<td>{num(w['soc0'], 0)} → {num(w['soc1'], 0)} %</td><td>{num(w['e_wh'], 0)} Wh</td>"
                         f"<td>{num(w['cap_kwh'])} kWh</td><td>{num(w['sigma_rel'] * 100, 1)} %</td></tr>" for w in used[:80]) +
                 "</tbody></table></div></section>")
    if cfg.ms and days:
        ds = sorted(days)
        pts = [(i, days[d].get("ms_diff_abs")) for i, d in enumerate(ds)]
        wake = [(i, days[d].get("ms_wake_delay_min")) for i, d in enumerate(ds)]
        labels = [(i, d[8:10] + ".") for i, d in enumerate(ds)][::3]
        ms = summary.get("master_slave") or {}
        vals = [v for _, v in pts if v is not None]
        wv = [v for _, v in wake if v is not None]
        mean_d = num(sum(vals) / len(vals) if vals else None, 1)
        mean_w = num(sum(wv) / len(wv) if wv else None, 0)
        parts.append("<section class='sec card'><h2>Master/Slave</h2>"
                     + line_chart([{"name": L("SOC difference (daily mean, percentage points)",
                                              "SOC-Differenz (Tagesmittel, Prozentpunkte)"), "points": pts}],
                                  y_label=L("percentage points", "Prozentpunkte"), y_min=0, x_labels=labels)
                     + line_chart([{"name": L("slave wake-up delay (minutes)", "Aufwachverzug Slave (Minuten)"),
                                    "points": wake, "cls": "s2"}], y_label=L("minutes", "Minuten"), y_min=0, x_labels=labels)
                     + "<p>" + L(f"Mean difference {mean_d} percentage points, wake-up delay {mean_w} min on average.",
                                 f"Mittlere Differenz {mean_d} Prozentpunkte, Aufwachverzug im Mittel {mean_w} min.") + "</p>"
                     f"<p>{e((ms.get('replug') or {}).get('text', ''))}</p>"
                     f"<p><b>{e((ms.get('comparison') or {}).get('summary', ''))}</b></p></section>")
        if vals:
            short.append(L(f"SOC difference Ø {mean_d} %", f"SOC-Differenz Ø {mean_d} %"))
    al = [a for a in store.alerts() if t0 <= a["last"] < t1 or a["state"] == "open"]
    parts.append(f"<section class='sec card'><h2>{L('Alerts', 'Meldungen')}</h2><ul>" +
                 ("".join(f"<li>{e(a['title'])}: {e(a['message'])} <span class='muted'>({a['state']})</span></li>" for a in al)
                  or f"<li class='muted'>{L('none', 'keine')}</li>") + "</ul></section>")
    cover = sum(1 for v in days.values() if v.get("source") == "minute")
    parts.append("<section class='sec'><p class='muted'>" + L(
        f"Data: {len(days)} days with data, {cover} of them per minute. Method and limits: README, section 'How it is calculated'.",
        f"Datenlage: {len(days)} Tage mit Daten, davon {cover} minutengenau. Methode und Grenzen: README, Abschnitt 'So wird gerechnet'.")
        + "</p></section>")
    text = f"{cfg.app_name} {period_name(period)}: " + "; ".join(short)
    return page(f"{cfg.app_name} – {period_name(period)}", "".join(parts), cfg), text


def write_monthly(cfg: Config, store: Store, summary: dict, period: str, now: float) -> tuple[Path, str]:
    h, text = build_monthly(cfg, store, summary, period, now)
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    p = cfg.report_dir / f"{L('report', 'bericht')}-{period}.html"
    p.write_text(h, encoding="utf-8")
    store.event(now, "report", f"monthly report {period}")
    return p, text
