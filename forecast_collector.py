#!/usr/bin/env python3
"""Collect and archive normalized forecasts for Granada Weather."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DATA_DIR = Path(os.environ.get("WEATHER_DATA_DIR", "/var/lib/pi-weather"))
FORECAST_DIR = DATA_DIR / "forecasts"
DATABASE = DATA_DIR / "forecast-history.sqlite3"
DEFAULT_FORECAST = DATA_DIR / "forecast.json"
PROVIDERS_FILE = FORECAST_DIR / "providers.json"
WEATHERKIT_LOGO = DATA_DIR / "weatherkit-attribution.png"
ASTRONOMY_FILE = DATA_DIR / "astronomy.json"
LOCAL_ZONE = ZoneInfo(os.environ.get("WEATHER_TIMEZONE", "America/New_York"))
TIMEOUT = 35
USER_AGENT = "GranadaWeather/2.0 (https://weather.thekitch.com/)"
LATITUDE = float(os.environ.get("LAT", "35.77194"))
LONGITUDE = float(os.environ.get("LON", "-78.63889"))

PROVIDER_NAMES = {
    "nws": "National Weather Service",
    "openmeteo_nbm": "NOAA NBM",
    "openmeteo_hrrr": "NOAA HRRR",
    "openmeteo_ecmwf": "ECMWF",
    "openweather": "OpenWeather",
    "wunderground": "Weather Underground",
    "weatherkit": "Apple Weather",
}

ATTRIBUTIONS = {
    "nws": {"text": "Forecast by the National Weather Service", "url": "https://www.weather.gov/rah/"},
    "openmeteo_nbm": {"text": "Weather data by Open-Meteo", "url": "https://open-meteo.com/"},
    "openmeteo_hrrr": {"text": "Weather data by Open-Meteo", "url": "https://open-meteo.com/"},
    "openmeteo_ecmwf": {"text": "Weather data by Open-Meteo", "url": "https://open-meteo.com/"},
    "openweather": {"text": "Forecast by OpenWeather", "url": "https://openweathermap.org/"},
    "wunderground": {"text": "Data provided by Weather Underground", "url": "https://www.wunderground.com/"},
    "weatherkit": {"text": "Weather", "url": "https://weatherkit.apple.com/legal-attribution.html"},
}

WMO_SUMMARIES = {
    0: "Clear sky", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 56: "Light freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Light freezing rain",
    67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains", 80: "Light rain showers", 81: "Rain showers",
    82: "Heavy rain showers", 85: "Light snow showers", 86: "Snow showers",
    95: "Thunderstorms", 96: "Thunderstorms with hail", 99: "Severe thunderstorms with hail",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        target = urllib.parse.urlparse(url)
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GET {target.netloc}{target.path} returned HTTP {exc.code}: {detail}") from exc


def get_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def finite(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def first_present(mapping, *keys):
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def round_or_none(value, digits=1):
    parsed = finite(value)
    return round(parsed, digits) if parsed is not None else None


def c_to_f(value):
    parsed = finite(value)
    return parsed * 9 / 5 + 32 if parsed is not None else None


def mm_to_in(value):
    parsed = finite(value)
    return parsed / 25.4 if parsed is not None else None


def iso_date(value: str) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(LOCAL_ZONE).date().isoformat()


def local_time(value) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(value, timezone.utc).astimezone(LOCAL_ZONE)
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_ZONE)
        parsed = parsed.astimezone(LOCAL_ZONE)
    return parsed.strftime("%-I:%M %p")


def words(value: str | None) -> str:
    if not value:
        return "Forecast"
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(value)).replace("_", "-").replace("-", " ")
    return spaced.strip().capitalize()


def moon_phase_name(day: date) -> str:
    reference = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    target = datetime.combine(day, time(12), LOCAL_ZONE).astimezone(timezone.utc)
    fraction = ((target - reference).total_seconds() / 86400 / 29.53058867) % 1
    phases = (
        "New moon", "Waxing crescent", "First quarter", "Waxing gibbous",
        "Full moon", "Waning gibbous", "Last quarter", "Waning crescent",
    )
    return phases[round(fraction * 8) % 8]


def fetch_astronomy() -> dict:
    query = urllib.parse.urlencode({
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "daily": "sunrise,sunset",
        "timezone": str(LOCAL_ZONE),
        "forecast_days": 16,
    })
    data = get_json(f"https://api.open-meteo.com/v1/forecast?{query}")
    if data.get("error"):
        raise RuntimeError(data.get("reason", "Astronomy request failed"))
    daily = data["daily"]
    days = {}
    for index, valid_date in enumerate(daily["time"]):
        sunrise = daily.get("sunrise", [None] * len(daily["time"]))[index]
        sunset = daily.get("sunset", [None] * len(daily["time"]))[index]
        days[valid_date] = {
            "sunrise": local_time(sunrise),
            "sunset": local_time(sunset),
            "moon_phase": moon_phase_name(date.fromisoformat(valid_date)),
        }
    return {
        "updated_at": iso_now(),
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "days": days,
    }


def canonical_astronomy() -> dict:
    try:
        astronomy = fetch_astronomy()
        atomic_json(ASTRONOMY_FILE, astronomy)
        return astronomy
    except (KeyError, OSError, RuntimeError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        try:
            cached = json.loads(ASTRONOMY_FILE.read_text(encoding="utf-8"))
            if finite(cached.get("latitude")) == LATITUDE and finite(cached.get("longitude")) == LONGITUDE:
                print(f"Using cached astronomy: {exc}", file=sys.stderr)
                return cached
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        print(f"Astronomy unavailable: {exc}", file=sys.stderr)
        return {"days": {}}


def apply_astronomy(forecast: dict, astronomy: dict) -> dict:
    canonical = astronomy.get("days", {})
    for row in forecast.get("days", []):
        valid_date = row.get("date")
        values = canonical.get(valid_date, {})
        row["sunrise"] = values.get("sunrise", "—")
        row["sunset"] = values.get("sunset", "—")
        row["moon_phase"] = values.get("moon_phase") or moon_phase_name(date.fromisoformat(valid_date))

    today = datetime.now(LOCAL_ZONE).date().isoformat()
    first = next((row for row in forecast.get("days", []) if row.get("date") == today), None)
    if first is None and forecast.get("days"):
        first = forecast["days"][0]
    if first:
        forecast["today_sunrise"] = first["sunrise"]
        forecast["today_sunset"] = first["sunset"]
        forecast["today_moon_phase"] = first["moon_phase"]
    return forecast


def daily_row(valid_date: str, high=None, low=None, rain_chance=None, rain_in=None,
              summary="Forecast", sunrise=None, sunset=None, moon_phase=None) -> dict:
    day = date.fromisoformat(valid_date)
    return {
        "date": valid_date,
        "high_f": round_or_none(high, 1),
        "low_f": round_or_none(low, 1),
        "rain_chance_pct": round_or_none(rain_chance, 0),
        "rain_in": round_or_none(rain_in, 3),
        "summary": summary or "Forecast",
        "sunrise": local_time(sunrise),
        "sunset": local_time(sunset),
        "moon_phase": moon_phase or moon_phase_name(day),
    }


def format_cache(provider: str, issued_at: str, rows: list[dict], attribution: dict | None = None) -> dict:
    rows = sorted(rows, key=lambda row: row["date"])
    today = datetime.now(LOCAL_ZONE).date()
    by_date = {row["date"]: row for row in rows}
    first = by_date.get(today.isoformat()) or (rows[0] if rows else {})
    second = by_date.get((today + timedelta(days=1)).isoformat()) or (rows[1] if len(rows) > 1 else {})
    return {
        "provider": provider,
        "provider_name": PROVIDER_NAMES[provider],
        "attribution": attribution or ATTRIBUTIONS[provider],
        "updated_at": iso_now(),
        "issued_at": issued_at,
        "today_high": first.get("high_f"),
        "today_low": first.get("low_f"),
        "today_rain_in": first.get("rain_in"),
        "today_rain_chance": first.get("rain_chance_pct"),
        "today_sunrise": first.get("sunrise", "—"),
        "today_sunset": first.get("sunset", "—"),
        "today_moon_phase": first.get("moon_phase", "—"),
        "today_summary": first.get("summary", "Forecast"),
        "tomorrow_high": second.get("high_f"),
        "tomorrow_low": second.get("low_f"),
        "tomorrow_rain_in": second.get("rain_in"),
        "tomorrow_rain_chance": second.get("rain_chance_pct"),
        "tomorrow_summary": second.get("summary", "Forecast"),
        "days": rows,
    }


def fetch_openmeteo(provider: str, model: str) -> dict:
    query = urllib.parse.urlencode({
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,weather_code,sunrise,sunset",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": str(LOCAL_ZONE),
        "forecast_days": 7,
        "models": model,
    })
    data = get_json(f"https://api.open-meteo.com/v1/forecast?{query}")
    if data.get("error"):
        raise RuntimeError(data.get("reason", "Open-Meteo request failed"))
    daily = data["daily"]
    rows = []
    for index, valid_date in enumerate(daily["time"]):
        item = lambda key: daily.get(key, [None] * len(daily["time"]))[index]
        rows.append(daily_row(
            valid_date,
            item("temperature_2m_max"), item("temperature_2m_min"),
            item("precipitation_probability_max"), item("precipitation_sum"),
            WMO_SUMMARIES.get(item("weather_code"), "Forecast"),
            item("sunrise"), item("sunset"),
        ))
    return format_cache(provider, iso_now(), rows)


def fetch_openweather() -> dict:
    api_key = os.environ.get("OPENWEATHER_API_KEY")
    if not api_key:
        raise KeyError("OPENWEATHER_API_KEY")
    query = urllib.parse.urlencode({
        "lat": LATITUDE, "lon": LONGITUDE, "exclude": "minutely,hourly,alerts",
        "units": "imperial", "appid": api_key,
    })
    data = get_json(f"https://api.openweathermap.org/data/3.0/onecall?{query}")
    rows = []
    for item in data.get("daily", []):
        valid_date = datetime.fromtimestamp(item["dt"], timezone.utc).astimezone(LOCAL_ZONE).date().isoformat()
        phase = item.get("moon_phase")
        phases = ("New moon", "Waxing crescent", "First quarter", "Waxing gibbous", "Full moon", "Waning gibbous", "Last quarter", "Waning crescent")
        phase_name = phases[round(float(phase) * 8) % 8] if phase is not None else None
        rows.append(daily_row(
            valid_date, item.get("temp", {}).get("max"), item.get("temp", {}).get("min"),
            finite(item.get("pop")) * 100 if finite(item.get("pop")) is not None else None,
            mm_to_in(item.get("rain", 0)),
            item.get("weather", [{}])[0].get("description", "Forecast"),
            item.get("sunrise"), item.get("sunset"), phase_name,
        ))
    issued = datetime.fromtimestamp(data.get("current", {}).get("dt", utc_now().timestamp()), timezone.utc).isoformat(timespec="seconds")
    return format_cache("openweather", issued, rows)


def parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", value)
    if not match:
        return timedelta(0)
    days, hours, minutes, seconds = match.groups()
    return timedelta(days=int(days or 0), hours=int(hours or 0), minutes=int(minutes or 0), seconds=float(seconds or 0))


def allocate_intervals(values: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for item in values:
        try:
            start_text, duration_text = item["validTime"].split("/", 1)
            start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
            end = start + parse_duration(duration_text)
            amount = finite(item.get("value"))
            if amount is None or end <= start:
                continue
        except (KeyError, ValueError):
            continue
        cursor = start
        while cursor < end:
            local = cursor.astimezone(LOCAL_ZONE)
            boundary = datetime.combine(local.date() + timedelta(days=1), time(0), LOCAL_ZONE).astimezone(timezone.utc)
            segment_end = min(end, boundary)
            fraction = (segment_end - cursor).total_seconds() / (end - start).total_seconds()
            key = local.date().isoformat()
            totals[key] = totals.get(key, 0.0) + amount * fraction
            cursor = segment_end
    return totals


def fetch_nws() -> dict:
    headers = {"Accept": "application/geo+json"}
    point = get_json(f"https://api.weather.gov/points/{LATITUDE:.5f},{LONGITUDE:.5f}", headers)
    properties = point["properties"]
    forecast = get_json(properties["forecast"], headers)
    grid = get_json(properties["forecastGridData"], headers)
    rows: dict[str, dict] = {}
    for period in forecast.get("properties", {}).get("periods", []):
        valid_date = iso_date(period["startTime"])
        row = rows.setdefault(valid_date, daily_row(valid_date))
        temp = finite(period.get("temperature"))
        if period.get("temperatureUnit") == "C":
            temp = c_to_f(temp)
        if period.get("isDaytime"):
            row["high_f"] = round_or_none(temp, 1)
            row["summary"] = period.get("shortForecast") or row["summary"]
        else:
            row["low_f"] = round_or_none(temp, 1)
        chance = finite(period.get("probabilityOfPrecipitation", {}).get("value"))
        if chance is not None:
            row["rain_chance_pct"] = max(row.get("rain_chance_pct") or 0, round(chance))
    qpf = allocate_intervals(grid.get("properties", {}).get("quantitativePrecipitation", {}).get("values", []))
    for valid_date, amount_mm in qpf.items():
        if valid_date in rows:
            rows[valid_date]["rain_in"] = round_or_none(mm_to_in(amount_mm), 3)
    issued = forecast.get("properties", {}).get("updated") or grid.get("properties", {}).get("updateTime") or iso_now()
    return format_cache("nws", issued, list(rows.values()))


def fetch_wunderground() -> dict:
    api_key = os.environ.get("WUNDERGROUND_API_KEY") or os.environ.get("WU_API_KEY")
    if not api_key:
        raise KeyError("WUNDERGROUND_API_KEY")
    query = urllib.parse.urlencode({
        "geocode": f"{LATITUDE},{LONGITUDE}", "format": "json", "units": "e",
        "language": "en-US", "apiKey": api_key,
    })
    data = get_json(f"https://api.weather.com/v3/wx/forecast/daily/7day?{query}")
    dates = data.get("validTimeLocal") or []
    highs = data.get("calendarDayTemperatureMax") or []
    lows = data.get("calendarDayTemperatureMin") or []
    qpf = data.get("qpf") or []
    narratives = data.get("narrative") or []
    sunrise = data.get("sunriseTimeLocal") or []
    sunset = data.get("sunsetTimeLocal") or []
    moons = data.get("moonPhase") or []
    dayparts = (data.get("daypart") or [{}])[0]
    chances = dayparts.get("precipChance") or []
    rows = []
    for index, value in enumerate(dates):
        pair = [finite(chances[pos]) for pos in (index * 2, index * 2 + 1) if pos < len(chances)]
        pair = [value for value in pair if value is not None]
        rows.append(daily_row(
            iso_date(value),
            highs[index] if index < len(highs) else None,
            lows[index] if index < len(lows) else None,
            max(pair) if pair else None,
            qpf[index] if index < len(qpf) else None,
            narratives[index] if index < len(narratives) else "Forecast",
            sunrise[index] if index < len(sunrise) else None,
            sunset[index] if index < len(sunset) else None,
            moons[index] if index < len(moons) else None,
        ))
    return format_cache("wunderground", iso_now(), rows)


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def der_length(data: bytes, offset: int) -> tuple[int, int]:
    length = data[offset]
    offset += 1
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    return length, offset


def der_to_jose(signature: bytes) -> bytes:
    if not signature or signature[0] != 0x30:
        raise RuntimeError("Unexpected WeatherKit signature format")
    _, offset = der_length(signature, 1)
    values = []
    for _ in range(2):
        if signature[offset] != 0x02:
            raise RuntimeError("Unexpected WeatherKit signature integer")
        length, offset = der_length(signature, offset + 1)
        integer = signature[offset:offset + length]
        offset += length
        values.append(integer.lstrip(b"\x00").rjust(32, b"\x00"))
    return b"".join(values)


def weatherkit_token() -> str:
    team = os.environ.get("WEATHERKIT_TEAM_ID")
    key_id = os.environ.get("WEATHERKIT_KEY_ID")
    service_id = os.environ.get("WEATHERKIT_SERVICE_ID")
    key_file = os.environ.get("WEATHERKIT_KEY_FILE")
    if not all((team, key_id, service_id, key_file)):
        raise KeyError("WeatherKit credentials")
    now = int(utc_now().timestamp())
    header = {"alg": "ES256", "kid": key_id, "id": f"{team}.{service_id}"}
    claims = {"iss": team, "iat": now, "exp": now + 1800, "sub": service_id}
    signing_input = f"{b64url(json.dumps(header, separators=(',', ':')).encode())}.{b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_file],
        input=signing_input.encode(), capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "Unable to sign WeatherKit token")
    return f"{signing_input}.{b64url(der_to_jose(result.stdout))}"


def weatherkit_attribution() -> dict:
    attribution = dict(ATTRIBUTIONS["weatherkit"])
    try:
        data = get_json("https://weatherkit.apple.com/attribution/en")
        legal = data.get("legalPageURL") or data.get("legalPageUrl")
        if legal:
            attribution["url"] = legal
        logo = data.get("logoLight@2x") or data.get("logoLight@1x") or data.get("combinedMarkLightURL")
        if logo:
            logo_url = urllib.parse.urljoin("https://weatherkit.apple.com", logo)
            WEATHERKIT_LOGO.write_bytes(get_bytes(logo_url))
            attribution["logo"] = "/api/forecast/attribution-logo"
    except (OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError):
        pass
    return attribution


def fetch_weatherkit() -> dict:
    token = weatherkit_token()
    query = urllib.parse.urlencode({
        "dataSets": "forecastDaily", "timezone": str(LOCAL_ZONE), "countryCode": "US",
    })
    data = get_json(
        f"https://weatherkit.apple.com/api/v1/weather/en/{LATITUDE}/{LONGITUDE}?{query}",
        {"Authorization": f"Bearer {token}"},
    )
    daily = data.get("forecastDaily", {})
    rows = []
    for item in daily.get("days", []):
        valid_date = iso_date(first_present(item, "forecastStart", "forecastStartTime"))
        chance = finite(item.get("precipitationChance"))
        rows.append(daily_row(
            valid_date,
            c_to_f(first_present(item, "temperatureMax", "maxTemperature")),
            c_to_f(first_present(item, "temperatureMin", "minTemperature")),
            chance * 100 if chance is not None and chance <= 1 else chance,
            mm_to_in(item.get("precipitationAmount")),
            words(item.get("conditionCode")),
            item.get("sunrise"), item.get("sunset"), words(item.get("moonPhase")) if item.get("moonPhase") else None,
        ))
    issued = daily.get("metadata", {}).get("readTime") or iso_now()
    return format_cache("weatherkit", issued, rows, weatherkit_attribution())


FETCHERS = {
    "nws": fetch_nws,
    "openmeteo_nbm": lambda: fetch_openmeteo("openmeteo_nbm", "ncep_nbm_conus"),
    "openmeteo_hrrr": lambda: fetch_openmeteo("openmeteo_hrrr", "ncep_hrrr_conus"),
    "openmeteo_ecmwf": lambda: fetch_openmeteo("openmeteo_ecmwf", "ecmwf_ifs025"),
    "openweather": fetch_openweather,
    "wunderground": fetch_wunderground,
    "weatherkit": fetch_weatherkit,
}


def configured(provider: str) -> bool:
    if provider == "openweather":
        return bool(os.environ.get("OPENWEATHER_API_KEY"))
    if provider == "wunderground":
        return bool(os.environ.get("WUNDERGROUND_API_KEY") or os.environ.get("WU_API_KEY"))
    if provider == "weatherkit":
        return all(os.environ.get(key) for key in ("WEATHERKIT_TEAM_ID", "WEATHERKIT_KEY_ID", "WEATHERKIT_SERVICE_ID", "WEATHERKIT_KEY_FILE"))
    return True


def database() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS forecast_runs (
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            UNIQUE(provider, issued_at)
        );
        CREATE TABLE IF NOT EXISTS forecast_daily (
            run_id INTEGER NOT NULL REFERENCES forecast_runs(id) ON DELETE CASCADE,
            valid_date TEXT NOT NULL,
            lead_days INTEGER NOT NULL,
            high_f REAL,
            low_f REAL,
            rain_chance_pct REAL,
            rain_in REAL,
            summary TEXT,
            PRIMARY KEY(run_id, valid_date)
        );
        CREATE INDEX IF NOT EXISTS forecast_daily_valid_date ON forecast_daily(valid_date);
        CREATE INDEX IF NOT EXISTS forecast_runs_provider_time ON forecast_runs(provider, issued_at);
    """)
    return connection


def archive(connection: sqlite3.Connection, forecast: dict) -> None:
    provider = forecast["provider"]
    issued = forecast["issued_at"]
    fetched = forecast["updated_at"]
    connection.execute(
        "INSERT OR IGNORE INTO forecast_runs(provider,issued_at,fetched_at,latitude,longitude) VALUES(?,?,?,?,?)",
        (provider, issued, fetched, LATITUDE, LONGITUDE),
    )
    run = connection.execute(
        "SELECT id FROM forecast_runs WHERE provider=? AND issued_at=?", (provider, issued)
    ).fetchone()
    if not run:
        return
    issued_date = datetime.fromisoformat(fetched.replace("Z", "+00:00")).astimezone(LOCAL_ZONE).date()
    for row in forecast.get("days", []):
        valid_date = date.fromisoformat(row["date"])
        connection.execute(
            """INSERT OR REPLACE INTO forecast_daily
               (run_id,valid_date,lead_days,high_f,low_f,rain_chance_pct,rain_in,summary)
               VALUES(?,?,?,?,?,?,?,?)""",
            (run[0], row["date"], (valid_date - issued_date).days, row.get("high_f"), row.get("low_f"),
             row.get("rain_chance_pct"), row.get("rain_in"), row.get("summary")),
        )


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def cache_forecast(forecast: dict) -> None:
    atomic_json(FORECAST_DIR / f"{forecast['provider']}.json", forecast)


def refresh_provider_index(statuses: dict[str, dict]) -> dict:
    available = []
    for provider, name in PROVIDER_NAMES.items():
        path = FORECAST_DIR / f"{provider}.json"
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            available.append({
                "id": provider,
                "name": name,
                "updated_at": cached.get("updated_at"),
                "attribution": cached.get("attribution", ATTRIBUTIONS[provider]),
            })
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    payload = {"default": os.environ.get("WEATHER_FORECAST_DEFAULT", "nws"), "providers": available, "status": statuses}
    atomic_json(PROVIDERS_FILE, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", action="append", choices=FETCHERS.keys())
    args = parser.parse_args()
    selected = args.provider or list(FETCHERS)
    FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    WEATHERKIT_LOGO.parent.mkdir(parents=True, exist_ok=True)
    astronomy = canonical_astronomy()
    statuses = {}
    successes = []
    with database() as connection:
        for provider in selected:
            if not configured(provider):
                statuses[provider] = {"configured": False}
                continue
            try:
                forecast = apply_astronomy(FETCHERS[provider](), astronomy)
                if not forecast.get("days"):
                    raise RuntimeError("provider returned no daily forecasts")
                archive(connection, forecast)
                cache_forecast(forecast)
                successes.append(provider)
                statuses[provider] = {"configured": True, "ok": True, "updated_at": forecast["updated_at"]}
                print(f"Collected {PROVIDER_NAMES[provider]} ({len(forecast['days'])} days)")
            except (KeyError, OSError, RuntimeError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
                statuses[provider] = {"configured": True, "ok": False, "error": str(exc)[:240]}
                print(f"{PROVIDER_NAMES[provider]} unavailable: {exc}", file=sys.stderr)
        connection.commit()
    index = refresh_provider_index(statuses)
    default = index["default"]
    default_path = FORECAST_DIR / f"{default}.json"
    if not default_path.exists() and successes:
        default_path = FORECAST_DIR / f"{successes[0]}.json"
    if default_path.exists():
        atomic_json(DEFAULT_FORECAST, json.loads(default_path.read_text(encoding="utf-8")))
    return 0 if successes else 1


if __name__ == "__main__":
    raise SystemExit(main())
