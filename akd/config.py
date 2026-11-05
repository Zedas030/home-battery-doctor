"""Load and validate the configuration (YAML + token from the environment).

Keys are English; the German keys of earlier versions are accepted as aliases."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .i18n import L, set_lang


class ConfigError(ValueError):
    pass


# German key -> English key (applied recursively to all mapping keys)
ALIASES = {
    "sprache": "language", "zeitzone": "timezone", "speicher_pfad": "data_path", "einheiten": "units",
    "ladeleistung": "charge_power", "entladeleistung": "discharge_power", "solarleistung": "solar_power",
    "ausgangsleistung": "output_power", "entladegrenze": "discharge_limit", "akku_anzahl": "battery_count",
    "temperatur": "temperature", "nennkapazitaet_kwh": "nominal_kwh", "inbetriebnahme": "commissioned",
    "inbetriebnahme_geschaetzt": "commissioned_estimated", "seriennummer": "serial",
    "schlaeft_mit_null": "sleeps_with_zero", "verlauf": "history_aliases",
    "garantie": "warranty", "jahre": "years", "min_restkapazitaet_prozent": "min_remaining_percent",
    "max_vollzyklen": "max_full_cycles", "zyklen_pro_jahr": "cycles_per_year",
    "referenz_alterung_pro_jahr": "reference_fade_per_year", "referenz_alterung_pro_zyklus": "reference_fade_per_cycle",
    "referenz_basis_prozent": "reference_base_percent", "laden_temp_min": "charge_temp_min",
    "laden_temp_max": "charge_temp_max", "entladen_temp_min": "discharge_temp_min",
    "entladen_temp_max": "discharge_temp_max", "warnen_abstand_prozentpunkte": "warn_margin_points",
    "warnen_monate_vor_ablauf": "warn_months_before_end",
    "schaetzung": "estimation", "min_hub_prozent": "min_swing_percent", "min_dauer": "min_duration",
    "max_luecke": "max_gap", "max_soc_sprung_prozent": "max_soc_jump_percent",
    "max_soc_stufe_prozent": "max_soc_step_percent", "min_leistung_w": "min_power_w", "temp_bereich": "temp_range",
    "leistung_fehler_relativ": "power_error_relative", "systematik_relativ": "systematic_relative",
    "min_fenster": "min_windows", "max_unsicherheit_relativ": "max_uncertainty_relative",
    "zeitraum_aktuell": "current_period", "einbruch_zeitraum": "drop_period",
    "einbruch_schwelle_relativ": "drop_threshold_relative", "trend_min_monate": "trend_min_months",
    "stundenwerte_nutzen": "use_hourly_statistics",
    "drossel_status": "throttle_flag", "umstecken": "replug_date", "umstecken_suchbereich": "replug_search_range",
    "vergleich_tage": "compare_days", "min_tage_je_seite": "min_days_per_side",
    "verschlechterung_tage": "worsening_days", "verschlechterung_prozentpunkte": "worsening_points",
    "schneller_altern_relativ": "unequal_aging_relative",
    "ueberwachung": "monitoring", "abfrage_intervall": "poll_interval", "keine_daten_nach": "no_data_after",
    "soc_eingefroren_nach": "soc_frozen_after", "soc_eingefroren_min_wh": "soc_frozen_min_wh",
    "temp_warn_ueber": "temp_warn_above", "ruhezeit": "quiet_hours",
    "bericht": "report", "monatlich_tag": "monthly_day", "monatlich_uhrzeit": "monthly_time",
    "aufbewahrung": "retention", "rohdaten_tage": "raw_days", "sicherung_pfad": "backup_path",
    "sicherung_tage": "backup_days",
}


def _normalize(obj):
    if isinstance(obj, dict):
        return {ALIASES.get(k, k) if isinstance(k, str) else k: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")


def dur(v, default=None) -> float:
    """'90s', '30m', '2h', '7d' or a number (seconds) -> seconds."""
    if v is None:
        if default is None:
            raise ConfigError("duration missing")
        return dur(default)
    if isinstance(v, (int, float)):
        return float(v)
    m = _DUR.match(str(v))
    if not m:
        raise ConfigError(f"invalid duration {v!r} (examples: 90s, 30m, 2h, 7d)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _date(v, what) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError as e:
        raise ConfigError(f"{what}: date YYYY-MM-DD expected, not {v!r}") from e


def _hhmm(v, what) -> tuple[int, int]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(v))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ConfigError(f"{what}: time HH:MM expected, not {v!r}")
    return int(m.group(1)), int(m.group(2))


@dataclass
class Unit:
    key: str
    name: str
    soc: str
    temperature: str | None
    nominal_kwh: float
    commissioned: date
    commissioned_estimated: bool = False
    serial: str = ""
    sleeps_with_zero: bool = False
    history_aliases: dict = field(default_factory=dict)  # earlier entity IDs per role (history import)


@dataclass
class SystemCfg:
    name: str
    soc: str
    charge_power: str
    discharge_power: str
    solar_power: str | None = None
    output_power: str | None = None
    discharge_limit: str | None = None
    battery_count: str | None = None


@dataclass
class Warranty:
    years: float = 10
    min_remaining_percent: float = 50
    max_full_cycles: float = 4000
    cycles_per_year: float = 300
    reference_fade_per_year: float = 1.5
    reference_fade_per_cycle: float = 0.011667
    reference_base_percent: float = 95
    charge_temp_min: float = 0
    charge_temp_max: float = 45
    discharge_temp_min: float = -20
    discharge_temp_max: float = 45
    warn_margin_points: float = 5
    warn_months_before_end: float = 12


@dataclass
class Estimation:
    min_swing_percent: float = 30
    min_duration: float = 1800
    max_gap: float = 600
    soc_band: tuple = (3, 98)
    max_soc_jump_percent: float = 3
    max_soc_step_percent: float = 5
    min_power_w: float = 30
    temp_range: tuple = (10, 35)
    power_error_relative: float = 0.02
    systematic_relative: float = 0.03
    min_windows: int = 5
    max_uncertainty_relative: float = 0.08
    current_period: float = 60 * 86400
    drop_period: float = 14 * 86400
    drop_threshold_relative: float = 0.08
    trend_min_months: int = 6
    use_hourly_statistics: bool = True


@dataclass
class MasterSlave:
    master: str = "master"
    slave: str = "slave"
    throttle_flag: str | None = None
    replug_date: date | None = None  # None = detect automatically
    search_range: tuple = (date(2026, 1, 1), date(2027, 12, 31))
    compare_days: int = 28
    min_days_per_side: int = 5
    worsening_days: int = 14
    worsening_points: float = 3
    unequal_aging_relative: float = 0.05


@dataclass
class Monitoring:
    poll_interval: float = 60
    no_data_after: float = 7200
    soc_frozen_after: float = 3 * 3600
    soc_frozen_min_wh: float = 300
    temp_warn_above: float = 40
    quiet_hours: tuple = ((22, 0), (8, 0))


@dataclass
class Config:
    ha_url: str
    token: str
    notify: str
    persistent_too: bool
    sensor_prefix: str
    prometheus_url: str
    tz: ZoneInfo
    web_port: int
    home_url: str
    home_label: str
    data_dir: Path
    system: SystemCfg
    units: dict[str, Unit]
    warranty: Warranty
    est: Estimation
    ms: MasterSlave | None
    mon: Monitoring
    language: str = "en"
    app_name: str = "Battery Doctor"
    report_day: int = 1
    report_time: tuple = (9, 0)
    raw_days: int = 180
    backup_dir: str = ""
    backup_days: int = 30
    source_path: str = ""

    @property
    def db_path(self) -> str:
        if str(self.data_dir) == ":memory:":
            return ":memory:"
        legacy = self.data_dir / "akku-doktor.sqlite"   # file name of the first (German) installations
        return str(legacy) if legacy.exists() else str(self.data_dir / "battery-doctor.sqlite")

    @property
    def report_dir(self) -> Path:
        legacy = self.data_dir / "berichte"
        return legacy if legacy.exists() else self.data_dir / "reports"

    @property
    def dossier_dir(self) -> Path:
        return self.data_dir / "dossiers"

    @property
    def nominal_total_kwh(self) -> float:
        return sum(u.nominal_kwh for u in self.units.values())

    def scopes(self) -> list[str]:
        """'system' plus every unit."""
        return ["system"] + list(self.units)

    def scope_name(self, scope: str) -> str:
        return self.system.name if scope == "system" else self.units[scope].name

    def nominal(self, scope: str) -> float:
        return self.nominal_total_kwh if scope == "system" else self.units[scope].nominal_kwh

    def start_date(self, scope: str) -> date:
        if scope == "system":
            return min(u.commissioned for u in self.units.values())
        return self.units[scope].commissioned

    def entity_roles(self) -> dict[str, str]:
        """Database key -> Home Assistant entity ID."""
        s = self.system
        roles = {"sys_soc": s.soc, "ch": s.charge_power, "dis": s.discharge_power}
        if s.solar_power:
            roles["sol"] = s.solar_power
        if s.output_power:
            roles["out"] = s.output_power
        if s.battery_count:
            roles["count"] = s.battery_count
        for k, u in self.units.items():
            roles[f"{k}_soc"] = u.soc
            if u.temperature:
                roles[f"{k}_temp"] = u.temperature
        if self.ms and self.ms.throttle_flag:
            roles["throttle"] = self.ms.throttle_flag
        return roles

    def history_aliases(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for k, u in self.units.items():
            for role, ids in (u.history_aliases or {}).items():
                role = {"temperatur": "temp", "temperature": "temp"}.get(role, role)
                out.setdefault(f"{k}_{role}", []).extend(ids if isinstance(ids, list) else [ids])
        return out


def _get(raw: dict, path: str, default=None):
    cur = raw
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _entity(v, what, required=True):
    if v in (None, ""):
        if required:
            raise ConfigError(f"{what}: entity ID missing")
        return None
    if not re.match(r"^[a-z_]+\.[a-z0-9_]+$", str(v)):
        raise ConfigError(f"{what}: {v!r} is not a valid entity ID")
    return str(v)


def load_config(path: str | Path, require_token: bool = True, env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"configuration {p} not found (template: config/config.example.yaml)")
    with open(p, encoding="utf-8") as f:
        raw = _normalize(yaml.safe_load(f) or {})
    language = str(raw.get("language", "en"))
    language = language if language in ("en", "de") else "en"
    set_lang(language)

    token = (env.get("HA_TOKEN") or "").strip()
    if require_token and not token:
        raise ConfigError("HA_TOKEN missing: put it into .env (see README, section 'Secrets')")

    sysraw = raw.get("system") or {}
    system = SystemCfg(
        name=str(sysraw.get("name", L("Storage system", "Speichersystem"))),
        soc=_entity(sysraw.get("soc"), "system.soc"),
        charge_power=_entity(sysraw.get("charge_power"), "system.charge_power"),
        discharge_power=_entity(sysraw.get("discharge_power"), "system.discharge_power"),
        solar_power=_entity(sysraw.get("solar_power"), "system.solar_power", False),
        output_power=_entity(sysraw.get("output_power"), "system.output_power", False),
        discharge_limit=_entity(sysraw.get("discharge_limit"), "system.discharge_limit", False),
        battery_count=_entity(sysraw.get("battery_count"), "system.battery_count", False),
    )
    units: dict[str, Unit] = {}
    for key, u in (raw.get("units") or {}).items():
        if not re.match(r"^[a-z][a-z0-9_]*$", str(key)):
            raise ConfigError(f"units: key {key!r} may only contain a-z, 0-9, _")
        if key == "system":
            raise ConfigError("units: 'system' is reserved")
        u = u or {}
        cap = float(u.get("nominal_kwh") or 0)
        if not 0.1 <= cap <= 100:
            raise ConfigError(f"units.{key}.nominal_kwh: value between 0.1 and 100 expected")
        units[key] = Unit(
            key=key, name=str(u.get("name", key)), soc=_entity(u.get("soc"), f"units.{key}.soc"),
            temperature=_entity(u.get("temperature"), f"units.{key}.temperature", False),
            nominal_kwh=cap, commissioned=_date(u.get("commissioned"), f"units.{key}.commissioned"),
            commissioned_estimated=bool(u.get("commissioned_estimated", False)),
            serial=str(u.get("serial", "")), sleeps_with_zero=bool(u.get("sleeps_with_zero", False)),
            history_aliases=u.get("history_aliases") or {},
        )
    if not units:
        raise ConfigError("units: at least one unit is required")

    w = Warranty(**{k: float(v) for k, v in (raw.get("warranty") or {}).items() if k in Warranty.__dataclass_fields__})

    s = raw.get("estimation") or {}
    est = Estimation()
    for k in ("min_swing_percent", "max_soc_jump_percent", "max_soc_step_percent", "min_power_w",
              "power_error_relative", "systematic_relative", "max_uncertainty_relative", "drop_threshold_relative"):
        if k in s:
            setattr(est, k, float(s[k]))
    for k in ("min_windows", "trend_min_months"):
        if k in s:
            setattr(est, k, int(s[k]))
    for k in ("min_duration", "max_gap", "current_period", "drop_period"):
        if k in s:
            setattr(est, k, dur(s[k]))
    for k in ("soc_band", "temp_range"):
        if k in s:
            a, b = s[k]
            if float(a) >= float(b):
                raise ConfigError(f"estimation.{k}: first value must be smaller")
            setattr(est, k, (float(a), float(b)))
    if "use_hourly_statistics" in s:
        est.use_hourly_statistics = bool(s["use_hourly_statistics"])
    if not 5 <= est.min_swing_percent <= 95:
        raise ConfigError("estimation.min_swing_percent: between 5 and 95")

    ms = None
    msraw = raw.get("master_slave")
    if msraw is not None:
        ms = MasterSlave()
        ms.master = str(msraw.get("master", "master"))
        ms.slave = str(msraw.get("slave", "slave"))
        for k in (ms.master, ms.slave):
            if k not in units:
                raise ConfigError(f"master_slave: unit {k!r} is not defined under 'units'")
        ms.throttle_flag = _entity(msraw.get("throttle_flag"), "master_slave.throttle_flag", False)
        um = msraw.get("replug_date", "auto")
        ms.replug_date = None if str(um).lower() in ("auto", "", "none") else _date(um, "master_slave.replug_date")
        if "replug_search_range" in msraw:
            a, b = msraw["replug_search_range"]
            ms.search_range = (_date(a, "replug_search_range"), _date(b, "replug_search_range"))
        for k in ("compare_days", "min_days_per_side", "worsening_days"):
            if k in msraw:
                setattr(ms, k, int(msraw[k]))
        for k in ("worsening_points", "unequal_aging_relative"):
            if k in msraw:
                setattr(ms, k, float(msraw[k]))

    m = raw.get("monitoring") or {}
    mon = Monitoring()
    for k in ("poll_interval", "no_data_after", "soc_frozen_after"):
        if k in m:
            setattr(mon, k, dur(m[k]))
    for k in ("soc_frozen_min_wh", "temp_warn_above"):
        if k in m:
            setattr(mon, k, float(m[k]))
    if "quiet_hours" in m:
        a, b = m["quiet_hours"]
        mon.quiet_hours = (_hhmm(a, "quiet_hours"), _hhmm(b, "quiet_hours"))
    if mon.poll_interval < 10:
        raise ConfigError("monitoring.poll_interval: at least 10s")

    notify = str(_get(raw, "home_assistant.notify", "") or "")
    if notify and not re.match(r"^[a-z_]+\.[a-z0-9_]+$", notify):
        raise ConfigError("home_assistant.notify: e.g. notify.mobile_app_phone")
    tzname = str(raw.get("timezone", "Europe/Berlin"))
    try:
        tz = ZoneInfo(tzname)
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"timezone {tzname!r} unknown") from e
    home_url = str(_get(raw, "web.home_url", "") or "").strip()
    if home_url and not re.match(r"(https?://|/)", home_url):
        raise ConfigError("web.home_url must start with http://, https:// or /")
    rtime = _hhmm(_get(raw, "report.monthly_time", "09:00"), "report.monthly_time")
    rday = int(_get(raw, "report.monthly_day", 1))
    if not 1 <= rday <= 28:
        raise ConfigError("report.monthly_day: 1 to 28")
    prefix = str(_get(raw, "home_assistant.sensor_prefix", L("battery_doctor", "akku_doktor")))
    if not re.match(r"^[a-z][a-z0-9_]*$", prefix):
        raise ConfigError("home_assistant.sensor_prefix: only a-z, 0-9, _")

    return Config(
        ha_url=str(_get(raw, "home_assistant.url", "http://127.0.0.1:8123")).rstrip("/"),
        token=token, notify=notify,
        persistent_too=bool(_get(raw, "home_assistant.persistent_notification", False)),
        sensor_prefix=prefix,
        prometheus_url=str(_get(raw, "prometheus.url", "") or "").rstrip("/"),
        tz=tz, web_port=int(_get(raw, "web.port", 8104)), home_url=home_url,
        home_label=str(_get(raw, "web.home_label", "Dashboard")),
        data_dir=Path(str(raw.get("data_path", "/data"))),
        system=system, units=units, warranty=w, est=est, ms=ms, mon=mon,
        language=language, app_name=str(raw.get("app_name") or L("Battery Doctor", "Akku-Doktor")),
        report_day=rday, report_time=rtime,
        raw_days=int(_get(raw, "retention.raw_days", 180)),
        backup_dir=str(_get(raw, "retention.backup_path", "") or ""),
        backup_days=int(_get(raw, "retention.backup_days", 30)),
        source_path=str(p),
    )
