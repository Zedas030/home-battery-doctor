"""Warranty dossier: PDF (history, method, measurement windows, temperature proof) + ZIP with raw data export (CSV)."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, Line, String
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import __version__
from .config import Config
from .i18n import KIND, REASON, SOURCE, STATUS, WARRANTY, L, fmt_date, num, tr
from .report import period_name
from .store import Store


def method_text(cfg: Config) -> list[str]:
    e = cfg.est
    f = dict(swing=e.min_swing_percent, dur=e.min_duration / 60, gap=e.max_gap / 60, jump=e.max_soc_jump_percent,
             b0=e.soc_band[0], b1=e.soc_band[1], t0=e.temp_range[0], t1=e.temp_range[1], sys=e.systematic_relative * 100,
             minw=e.min_windows, maxu=e.max_uncertainty_relative * 100, tm=e.trend_min_months)
    return [L(
        "The usable capacity is measured from real operation, without test cycles: in periods in which the storage only "
        "discharges (or only charges), the measured power is summed up per minute and divided by the change of the state "
        "of charge (SOC). Energy per percentage point times 100 gives the capacity in kWh.",
        "Die nutzbare Kapazität wird aus dem echten Betrieb gemessen, ohne Testzyklen: In Zeiträumen, in denen der "
        "Speicher nur entlädt (oder nur lädt), wird die gemessene Leistung minutengenau aufsummiert und durch die "
        "Änderung des Ladestands (SOC) geteilt. Energie je Prozentpunkt mal 100 ergibt die Kapazität in kWh."), L(
        "Only clean measurement windows count: at least {swing:.0f} percentage points SOC swing, at least {dur:.0f} minutes, "
        "no data gap over {gap:.0f} minutes, no SOC jump over {jump:.0f} percentage points (calibration, waking up), edges "
        "only between {b0:.0f} and {b1:.0f} % SOC, battery temperature between {t0:.0f} and {t1:.0f} °C, SOC only in flow "
        "direction. Every window starts and ends exactly at a change of the whole-number SOC value, so the rounding error "
        "of whole percent largely cancels out.",
        "Nur saubere Messfenster zählen: mindestens {swing:.0f} Prozentpunkte SOC-Hub, mindestens {dur:.0f} Minuten, keine "
        "Datenlücke über {gap:.0f} Minuten, keine SOC-Sprünge über {jump:.0f} Prozentpunkte (Kalibrierung, Aufwachen), "
        "Ränder nur zwischen {b0:.0f} und {b1:.0f} % SOC, Akkutemperatur zwischen {t0:.0f} und {t1:.0f} °C, SOC nur in "
        "Flussrichtung. Jedes Fenster beginnt und endet genau an einem Wechsel des ganzzahligen SOC-Werts; so fällt der "
        "Rundungsfehler der ganzen Prozent weitgehend heraus.").format(**f), L(
        "The estimate of a period is the weighted mean of the windows (weight by measurement uncertainty); outliers are "
        "rejected robustly (median deviation). The 95 % range comes from a bootstrap over the windows and additionally "
        "contains a possible common deviation of the power sensor of ±{sys:.0f} %. Fewer than {minw} clean windows or a "
        "range wider than ±{maxu:.0f} % explicitly give 'no reliable statement'.",
        "Die Schätzung eines Zeitraums ist das gewichtete Mittel der Fenster (Gewicht nach Messunsicherheit), Ausreißer "
        "werden robust (Median-Abweichung) verworfen. Der 95-%-Bereich kommt aus einem Bootstrap über die Fenster und "
        "enthält zusätzlich eine mögliche gemeinsame Abweichung des Leistungssensors von ±{sys:.0f} %. Weniger als {minw} "
        "saubere Fenster oder ein Bereich breiter als ±{maxu:.0f} % ergeben ausdrücklich 'keine verlässliche Aussage'.").format(**f), L(
        "The capacity per unit is determined jointly from many windows (energy = sum of Wh per percentage point of the unit "
        "times its SOC change, robust regression), because power is only measured for the whole system. Charge windows enter "
        "via the measured efficiency (discharge to charge side).",
        "Die Kapazität je Einheit wird aus vielen Fenstern gemeinsam bestimmt (Energie = Summe aus Wh je Prozentpunkt der "
        "Einheit mal deren SOC-Änderung, robuste Regression), weil die Leistung nur für das Gesamtsystem gemessen wird. "
        "Ladefenster gehen dabei über den gemessenen Wirkungsgrad (Entlade- zu Ladeseite) ein."), L(
        "Full-cycle equivalents: whole system = discharged energy / nominal capacity; per unit = sum of downward SOC steps / "
        "100. The trend is a robust line (Theil-Sen) over the reliable monthly values; forecasts only after {tm} months.",
        "Vollzyklen-Äquivalente: Gesamtsystem = entladene Energie / Nennkapazität; je Einheit = Summe der "
        "SOC-Abwärtsschritte / 100. Der Trend ist eine robuste Gerade (Theil-Sen) über die verlässlichen Monatswerte; "
        "Prognosen erst ab {tm} Monaten.").format(**f)]


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("small", parent=ss["BodyText"], fontSize=8, leading=10))
    return ss


def _table(rows, widths=None, head=True, font=8):
    t = Table(rows, colWidths=widths, repeatRows=1 if head else 0)
    st = [("FONT", (0, 0), (-1, -1), "Helvetica", font), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c8ced8")),
          ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if head:
        st += [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", font), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf4"))]
    t.setStyle(TableStyle(st))
    return t


def _chart(cfg: Config, scope: str, monthly: list[dict]) -> Drawing:
    nom = cfg.nominal(scope)
    pts = list(enumerate(sorted(monthly, key=lambda e: e["period"])))
    d = Drawing(170 * mm, 70 * mm)
    lp = LinePlot()
    lp.x, lp.y, lp.width, lp.height = 30, 25, 170 * mm - 45, 70 * mm - 40
    ok = [(i, e["cap_kwh"] / nom * 100) for i, e in pts if e["status"] == "ok" and e.get("cap_kwh")]
    lo = [(i, e["lo"] / nom * 100) for i, e in pts if e["status"] == "ok" and e.get("lo")]
    hi = [(i, e["hi"] / nom * 100) for i, e in pts if e["status"] == "ok" and e.get("hi")]
    lp.data = [s for s in (ok, lo, hi) if s] or [[(0, 100)]]
    lp.joinedLines = 1
    lp.lines[0].strokeColor = colors.HexColor("#2a78d6")
    lp.lines[0].strokeWidth = 1.6
    lp.lines[0].symbol = makeMarker("FilledCircle", size=3)
    for k in (1, 2):
        lp.lines[k].strokeColor = colors.HexColor("#9fbbe3")
        lp.lines[k].strokeDashArray = [2, 2]
    lp.yValueAxis.valueMin, lp.yValueAxis.valueMax, lp.yValueAxis.valueStep = 40, 105, 10
    n = max(1, len(pts) - 1)
    lp.xValueAxis.valueMin, lp.xValueAxis.valueMax = 0, n
    lp.xValueAxis.valueStep = max(1, n // 10)
    lp.xValueAxis.labelTextFormat = lambda v: (pts[int(v)][1]["period"][5:] + "/" + pts[int(v)][1]["period"][2:4]) \
        if 0 <= int(v) < len(pts) else ""
    d.add(lp)
    for val, label in ((80, "80 %"), (cfg.warranty.min_remaining_percent, L("warranty limit", "Garantiegrenze"))):
        y = lp.y + (val - 40) / 65 * lp.height
        d.add(Line(lp.x, y, lp.x + lp.width, y, strokeColor=colors.HexColor("#d03b3b"), strokeDashArray=[4, 3]))
        d.add(String(lp.x + lp.width - 60, y + 2, label, fontSize=7, fillColor=colors.HexColor("#d03b3b")))
    d.add(String(lp.x, lp.y + lp.height + 6, L(
        "SOH in % of the nominal capacity (dots: monthly estimate, dashed: 95 % range)",
        "SOH in % der Nennkapazität (Punkte: Monatsschätzung, gestrichelt: 95-%-Bereich)"), fontSize=7))
    return d


def build_pdf(cfg: Config, store: Store, summary: dict, scope: str, now: float, hashes: dict) -> bytes:
    ss = _styles()
    buf = io.BytesIO()
    title = L("Warranty dossier", "Garantie-Dossier")
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=f"{title} {cfg.scope_name(scope)}", author=cfg.app_name,
                            subject=L("Capacity proof of a home battery", "Kapazitätsnachweis Heimspeicher"))
    sc = (summary.get("scopes") or {}).get(scope, {})
    cur = sc.get("current") or {}
    det = cur.get("detail") or {}
    g = sc.get("warranty") or {}
    trd = sc.get("trend") or {}
    cyc = summary.get("cycles") or {}
    w = cfg.warranty
    units = [cfg.units[scope]] if scope != "system" else list(cfg.units.values())
    created = datetime.fromtimestamp(now, cfg.tz)
    story = [Paragraph(f"{title}: {cfg.scope_name(scope)}", ss["Title"]),
             Paragraph(L(f"Created {created:%Y-%m-%d %H:%M} with {cfg.app_name} {__version__}. All values are calculated "
                         f"from operating data recorded by Home Assistant; the tool only reads and never interferes "
                         f"with the operation.",
                         f"Erstellt am {created:%d.%m.%Y um %H:%M} mit {cfg.app_name} {__version__}. Alle Werte sind aus "
                         f"den von Home Assistant aufgezeichneten Betriebsdaten berechnet; das Werkzeug liest nur und "
                         f"greift nicht in den Betrieb ein."), ss["small"]), Spacer(0, 4 * mm)]
    est_txt = L(" (estimated from the data)", " (aus Daten geschätzt)")
    rows = [[L("Device", "Gerät"), L("Serial number", "Seriennummer"), L("Nominal capacity", "Nennkapazität"),
             L("Commissioned", "Inbetriebnahme")]]
    for u in units:
        rows.append([u.name, u.serial or "–", f"{num(u.nominal_kwh, 3)} kWh",
                     fmt_date(u.commissioned) + (est_txt if u.commissioned_estimated else "")])
    story += [_table(rows, [60 * mm, 40 * mm, 30 * mm, 44 * mm]), Spacer(0, 4 * mm)]
    story.append(Paragraph(L("Result", "Ergebnis"), ss["Heading2"]))
    days_cur = cfg.est.current_period / 86400
    res = [[L("Quantity", "Größe"), L("Value", "Wert")],
           [L(f"Usable capacity (current, last {days_cur:.0f} days)", f"Nutzbare Kapazität (aktuell, letzte {days_cur:.0f} Tage)"),
            L(f"{num(cur.get('cap_kwh'), 3)} kWh (95 % range {num(cur.get('lo'), 3)} to {num(cur.get('hi'), 3)} kWh)",
              f"{num(cur.get('cap_kwh'), 3)} kWh (95-%-Bereich {num(cur.get('lo'), 3)} bis {num(cur.get('hi'), 3)} kWh)")
            if cur.get("cap_kwh") else "–"],
           [L("Share of nominal capacity (SOH)", "Anteil der Nennkapazität (SOH)"),
            L(f"{num(cur.get('soh'), 1)} % (range {num(det.get('soh_lo'), 1)} to {num(det.get('soh_hi'), 1)} %)",
              f"{num(cur.get('soh'), 1)} % (Bereich {num(det.get('soh_lo'), 1)} bis {num(det.get('soh_hi'), 1)} %)")
            if cur.get("soh") else "–"],
           [L("Significance", "Aussagekraft"), tr(STATUS, cur.get("status")) + (f" – {det['reason']}" if det.get("reason") else "")],
           [L("Clean measurement windows in the period", "Saubere Messfenster im Zeitraum"), str(cur.get("n", 0))],
           [L("Full-cycle equivalents since start of data", "Vollzyklen-Äquivalente seit Datenbeginn"),
            f"{num(cyc.get(scope), 1)} ({L('since', 'seit')} {cyc.get('since') or '–'})"],
           [L("Total discharged energy (system)", "Entladene Energie gesamt (System)"), f"{num(cyc.get('discharged_kwh'), 1)} kWh"],
           ["Trend", (trd.get("forecast_80") or {}).get("text") or trd.get("text", "–")],
           [L("Manufacturer warranty curve today", "Garantiekurve des Herstellers heute"),
            L(f"{num(g.get('reference_soh'), 1)} % of nominal capacity", f"{num(g.get('reference_soh'), 1)} % der Nennkapazität")],
           [L("Warranty status", "Garantiestatus"), "; ".join([tr(WARRANTY, g.get("status")) or "–",
                                                               f"{L('end', 'Ende')} {g.get('end_effective', '–')}"]
                                                              + list(g.get("notes") or []))]]
    if cur.get("eff"):
        res.insert(4, [L("Efficiency discharge to charge side", "Wirkungsgrad Entlade- zu Ladeseite"), f"{num(cur['eff'] * 100, 1)} %"])
    story += [_table(res, [62 * mm, 112 * mm]), Spacer(0, 3 * mm)]
    story.append(Paragraph(L(
        f"Configured warranty terms: {w.years:.0f} years or {w.max_full_cycles:.0f} full cycles, whichever comes first; "
        f"claim if the capacity is below {w.min_remaining_percent:.0f} % of the nominal capacity; basis {w.cycles_per_year:.0f} "
        f"cycles per year. Operation: charging {w.charge_temp_min:.0f} to {w.charge_temp_max:.0f} °C, discharging "
        f"{w.discharge_temp_min:.0f} to {w.discharge_temp_max:.0f} °C. Manufacturers usually require proof of the operating "
        f"data (e.g. total discharged energy) for a claim.",
        f"Konfigurierte Garantiebedingungen: {w.years:.0f} Jahre oder {w.max_full_cycles:.0f} Vollzyklen, was zuerst eintritt; "
        f"Anspruch, wenn die Kapazität unter {w.min_remaining_percent:.0f} % der Nennkapazität liegt; Grundlage "
        f"{w.cycles_per_year:.0f} Zyklen pro Jahr. Betrieb: Laden {w.charge_temp_min:.0f} bis {w.charge_temp_max:.0f} °C, "
        f"Entladen {w.discharge_temp_min:.0f} bis {w.discharge_temp_max:.0f} °C. Für einen Anspruch verlangen Hersteller "
        f"meist einen Nachweis der Betriebsdaten (z. B. gesamte entladene Energie)."), ss["small"]))
    story.append(Paragraph(L("Capacity history", "Kapazitätsverlauf"), ss["Heading2"]))
    monthly = sc.get("months") or []
    story.append(_chart(cfg, scope, monthly))
    mrows = [[L("Month", "Monat"), "Status", L("Capacity kWh", "Kapazität kWh"), L("Range kWh", "Bereich kWh"), "SOH %",
              L("Windows", "Fenster"), L("Source", "Quelle")]]
    for e in sorted(monthly, key=lambda e: e["period"]):
        mrows.append([period_name(e["period"]), tr(STATUS, e["status"]), num(e.get("cap_kwh"), 3),
                      f"{num(e.get('lo'), 3)} – {num(e.get('hi'), 3)}" if e.get("lo") else "–", num(e.get("soh"), 1),
                      str(e.get("n") or 0), ", ".join(tr(SOURCE, s) for s in (e.get("detail") or {}).get("sources") or [])])
    story += [_table(mrows, [30 * mm, 38 * mm, 24 * mm, 32 * mm, 16 * mm, 14 * mm, 20 * mm])]
    days = store.daily()
    story.append(Paragraph(L("Temperature proof (warranty conditions)", "Temperaturnachweis (Garantiebedingungen)"), ss["Heading2"]))
    trows = [[L("Unit", "Einheit"), "min °C", "max °C", L("Minutes outside*", "Minuten außerhalb*"),
              L("Hours outside**", "Stunden außerhalb**"), L("Days with data", "Tage mit Daten")]]
    for u in units:
        mins = [v.get(f"{u.key}_temp_min") for v in days.values() if v.get(f"{u.key}_temp_min") is not None]
        maxs = [v.get(f"{u.key}_temp_max") for v in days.values() if v.get(f"{u.key}_temp_max") is not None]
        viol = sum(v.get(f"{u.key}_temp_violation_min") or 0 for v in days.values())
        viol_h = sum(v.get(f"{u.key}_temp_violation_h") or 0 for v in days.values())
        trows.append([u.name, num(min(mins), 0) if mins else "–", num(max(maxs), 0) if maxs else "–", str(viol), str(viol_h),
                      str(len(mins))])
    story += [_table(trows, [58 * mm, 16 * mm, 16 * mm, 30 * mm, 30 * mm, 24 * mm]), Paragraph(L(
        f"* days with minute data: charging outside {w.charge_temp_min:.0f} to {w.charge_temp_max:.0f} °C, discharging outside "
        f"{w.discharge_temp_min:.0f} to {w.discharge_temp_max:.0f} °C. ** older days with hourly statistics only: hours with "
        f"maximum above {w.charge_temp_max:.0f} °C or minimum below {w.discharge_temp_min:.0f} °C (charge state unknown there). "
        f"Impossible error values (e.g. -273 °C) and the placeholder 0 °C at 0 % (unit offline/asleep) are ignored.",
        f"* minutengenaue Tage: Laden außerhalb {w.charge_temp_min:.0f} bis {w.charge_temp_max:.0f} °C, Entladen außerhalb "
        f"{w.discharge_temp_min:.0f} bis {w.discharge_temp_max:.0f} °C. ** ältere Tage nur mit Stundenstatistik: Stunden mit "
        f"Höchstwert über {w.charge_temp_max:.0f} °C oder Tiefstwert unter {w.discharge_temp_min:.0f} °C (Lade-/Entladezustand "
        f"dort nicht bekannt). Unmögliche Fehlwerte (z. B. -273 °C) und der Platzhalter 0 °C bei gleichzeitig 0 % (Einheit "
        f"offline/schlafend) sind ausgeblendet."), ss["small"])]
    story.append(PageBreak())
    story.append(Paragraph(L("Method", "Methode"), ss["Heading2"]))
    for p in method_text(cfg):
        story.append(Paragraph(p, ss["BodyText"]))
    story.append(Paragraph(L("Data sources and integrity", "Datenquellen und Integrität"), ss["Heading2"]))
    srows = [[L("Role", "Rolle"), L("Entity in Home Assistant", "Entität in Home Assistant")]] + \
        [[k, v] for k, v in cfg.entity_roles().items()]
    story += [_table(srows, [40 * mm, 134 * mm]), Spacer(0, 3 * mm)]
    story += [_table([[L("File in the export", "Datei im Export"), "SHA-256"]] + [[k, v] for k, v in hashes.items()],
                     [40 * mm, 134 * mm], font=7)]
    story.append(PageBreak())
    story.append(Paragraph(L("Measurement windows (used)", "Messfenster (verwendet)"), ss["Heading2"]))
    wins = [x for x in store.windows(used=True) if scope == "system" or x["source"] == "minute"]
    wrows = [[L("Start", "Beginn"), L("End", "Ende"), L("Kind", "Art"), L("Source", "Quelle"), "SOC %",
              L("Energy Wh", "Energie Wh"), "kWh", "±%", "°C"]]
    for x in wins[-400:]:
        wrows.append([datetime.fromtimestamp(x["t0"], cfg.tz).strftime(L("%y-%m-%d %H:%M", "%d.%m.%y %H:%M")),
                      datetime.fromtimestamp(x["t1"], cfg.tz).strftime(L("%m-%d %H:%M", "%d.%m. %H:%M")),
                      tr(KIND, x["kind"]), tr(SOURCE, x["source"]), f"{num(x['soc0'], 0)}-{num(x['soc1'], 0)}",
                      num(x["e_wh"], 0), num(x["cap_kwh"], 3), num(x["sigma_rel"] * 100, 1), num(x["temp"], 0)])
    more = L(", the last 400 shown here; all in windows.csv.", ", hier die letzten 400; alle in windows.csv.")
    story.append(Paragraph(L(f"{len(wins)} windows used", f"{len(wins)} verwendete Fenster") +
                           (more if len(wins) > 400 else "."), ss["small"]))
    story.append(_table(wrows, [24 * mm, 20 * mm, 15 * mm, 15 * mm, 16 * mm, 18 * mm, 16 * mm, 12 * mm, 10 * mm], font=7))
    rej = {}
    for x in store.windows(used=False):
        rej[x["reason"]] = rej.get(x["reason"], 0) + 1
    story.append(Spacer(0, 3 * mm))
    story.append(Paragraph(L("Rejected windows: ", "Verworfene Fenster: ") + (
        ", ".join(f"{tr(REASON, k)} {v}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])) or L("none", "keine")),
        ss["small"]))

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.drawString(18 * mm, 9 * mm, f"{cfg.app_name} {__version__} · {title} {cfg.scope_name(scope)} · "
                                           f"{fmt_date(created.date())}")
        canvas.drawRightString(A4[0] - 18 * mm, 9 * mm, f"{L('Page', 'Seite')} {doc_.page}")
        canvas.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# ------------------------------------------------------------------------ CSV
def _csv_bytes(header, rows) -> bytes:
    s = io.StringIO()
    wr = csv.writer(s, delimiter=";")
    wr.writerow(header)
    wr.writerows(rows)
    return s.getvalue().encode("utf-8-sig")


def export_tables(cfg: Config, store: Store, raw_days: int | None = None) -> dict[str, bytes]:
    tz = cfg.tz
    loc = lambda ts: datetime.fromtimestamp(ts, tz).isoformat()  # noqa: E731
    out = {}
    out["windows.csv"] = _csv_bytes(
        ["scope", "kind", "source", "start", "end", "soc_start", "soc_end", "energy_wh", "capacity_kwh",
         "uncertainty_rel", "temperature_c", "used", "reason", "units"],
        [[w["scope"], w["kind"], w["source"], loc(w["t0"]), loc(w["t1"]), w["soc0"], w["soc1"], w["e_wh"], w["cap_kwh"],
          w["sigma_rel"], w["temp"], w["used"], w["reason"], json.dumps(w["deltas"])] for w in store.windows()])
    out["estimates.csv"] = _csv_bytes(
        ["scope", "period", "computed", "status", "capacity_kwh", "lower", "upper", "soh", "windows", "efficiency",
         "details"],
        [[e["scope"], e["period"], loc(e["computed"]), e["status"], e["cap_kwh"], e["lo"], e["hi"], e["soh"], e["n"],
          e["eff"], json.dumps(e["detail"], ensure_ascii=False)] for e in store.estimates()])
    days = store.daily()
    keys = sorted({k for v in days.values() for k in v})
    out["daily.csv"] = _csv_bytes(["day"] + keys, [[d] + [v.get(k) for k in keys] for d, v in days.items()])
    roles = list(cfg.entity_roles())
    lim = None if raw_days is None else datetime.now(tz).timestamp() - raw_days * 86400
    out["raw_minutes.csv"] = _csv_bytes(["role", "time", "value"],
                                        [[k, loc(ts), v] for k in roles for ts, v in store.series(k, lim or 0, 4e9)])
    out["raw_15min.csv"] = _csv_bytes(["role", "time", "mean", "min", "max", "first", "last", "count"],
                                      [[k, loc(ts), mean, mn, mx, first, last, n] for k in roles
                                       for ts, mean, mn, mx, first, last, n in store.agg_series(k, 0, 4e9)])
    out["hourly_statistics.csv"] = _csv_bytes(["role", "time", "mean", "min", "max"],
                                              [[k, loc(ts), mean, mn, mx] for k in roles
                                               for ts, mean, mn, mx in store.hourly(k, 0, 4e9)])
    out["entities.json"] = json.dumps({"roles": cfg.entity_roles(), "baseline": store.get_meta("entity_baseline"),
                                       "renamed": store.get_meta("renamed")}, ensure_ascii=False, indent=1).encode("utf-8")
    return out


def make_dossier(cfg: Config, store: Store, summary: dict, scope: str, now: float) -> tuple[Path, Path]:
    """Writes <dossiers>/<warranty-dossier>-<scope>-<date>.pdf and .zip (PDF + CSV + checksums)."""
    tables = export_tables(cfg, store)
    hashes = {k: hashlib.sha256(v).hexdigest() for k, v in tables.items()}
    pdf = build_pdf(cfg, store, summary, scope, now, hashes)
    stamp = datetime.fromtimestamp(now, cfg.tz).strftime("%Y-%m-%d-%H%M")
    cfg.dossier_dir.mkdir(parents=True, exist_ok=True)
    base = cfg.dossier_dir / f"{L('warranty-dossier', 'garantie-dossier')}-{scope}-{stamp}"
    pdf_path = base.with_suffix(".pdf")
    pdf_path.write_bytes(pdf)
    zpath = base.with_suffix(".zip")
    with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(pdf_path.name, pdf)
        for k, v in tables.items():
            z.writestr(k, v)
        hashes[pdf_path.name] = hashlib.sha256(pdf).hexdigest()
        z.writestr("SHA256SUMS.txt", "".join(f"{v}  {k}\n" for k, v in hashes.items()))
    store.event(now, "dossier", f"warranty dossier {scope} ({pdf_path.name})")
    return pdf_path, zpath
