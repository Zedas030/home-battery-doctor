"""Language selection (English default, German) and number/date formatting."""
from __future__ import annotations

LANGS = ("en", "de")
_lang = "en"


def set_lang(lang: str | None) -> None:
    global _lang
    _lang = lang if lang in LANGS else "en"


def lang() -> str:
    return _lang


def L(en: str, de: str) -> str:
    """Pick the text for the active language."""
    return de if _lang == "de" else en


MONTHS = {"en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                 "November", "December"],
          "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
                 "November", "Dezember"]}


def month_name(m: int, y: int) -> str:
    return f"{MONTHS[_lang][m - 1]} {y}"


def num(x, digits=2) -> str:
    """Number in the active language (German: 1.234,56)."""
    if x is None:
        return "–"
    s = f"{x:,.{digits}f}"
    if _lang == "de":
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return s


def fmt_dt(dt) -> str:
    return dt.strftime("%d.%m.%Y %H:%M") if _lang == "de" else dt.strftime("%Y-%m-%d %H:%M")


def fmt_short(dt) -> str:
    return dt.strftime("%d.%m. %H:%M") if _lang == "de" else dt.strftime("%m-%d %H:%M")


def fmt_date(d) -> str:
    return d.strftime("%d.%m.%Y") if _lang == "de" else d.isoformat()


# Display names of internal codes
KIND = {"charge": ("charge", "laden"), "discharge": ("discharge", "entladen")}
SOURCE = {"minute": ("minutes", "minuten"), "hourly": ("hourly", "stunden")}
STATUS = {"ok": ("reliable", "verlässlich"), "uncertain": ("no reliable statement", "keine verlässliche Aussage"),
          "no_data": ("no data", "keine Daten")}
WARRANTY = {"ok": ("fine", "in Ordnung"), "warning": ("warning", "Warnung"),
            "claim": ("warranty claim likely", "Garantiefall wahrscheinlich"),
            "ending": ("ends soon", "Ablauf naht"), "expired": ("expired", "abgelaufen")}
REASON = {
    "swing_too_small": ("SOC swing too small", "SOC-Hub zu klein"), "too_short": ("too short", "zu kurz"),
    "power_too_low": ("power too low", "Leistung zu klein"),
    "unit_missing": ("a unit had no values (asleep?)", "eine Einheit ohne Werte (schläft?)"),
    "temperature": ("temperature outside range", "Temperatur außerhalb des Bereichs"),
    "soc_reversal": ("SOC moved backwards", "SOC läuft zurück"), "implausible": ("implausible result", "Ergebnis unplausibel"),
    "soc_jump": ("SOC jump", "SOC-Sprung"), "soc_wrong_direction": ("SOC against flow direction", "SOC gegen Flussrichtung"),
    "soc_band": ("edge outside the SOC band", "Rand außerhalb des SOC-Bands"),
    "outlier": ("outlier (robust check)", "Ausreißer (robuste Prüfung)"), "gap": ("data gap", "Datenlücke"),
    "soc_too_coarse": ("SOC reported in coarse steps only", "SOC nur in groben Stufen"),
    "battery_count": ("not all batteries reported", "nicht alle Akkus gemeldet"),
}


def tr(table: dict, code) -> str:
    v = table.get(code)
    if not v:
        return str(code or "")
    return v[1] if _lang == "de" else v[0]
