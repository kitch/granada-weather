#!/usr/bin/env python3
"""Send cached Granada Weather observations and forecasts to TRMNL."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


LOCAL_WEATHER_URL = "http://127.0.0.1/api/current"
TIMEOUT = 30


def get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "granada-weather/2.0"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        target = urllib.parse.urlparse(url)
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GET {target.netloc}{target.path} returned HTTP {exc.code}: {detail}") from exc


def post_json(url: str, payload: dict) -> int:
    body = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "granada-weather/2.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"TRMNL returned HTTP {exc.code}: {detail}") from exc


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def safe(value, suffix: str = "", digits: int | None = None) -> str:
    if value is None or value == "":
        return "—"
    if digits is not None:
        value = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    return f"{value}{suffix}"


def cached_forecast() -> dict:
    provider = os.environ.get("TRMNL_FORECAST_PROVIDER", os.environ.get("WEATHER_FORECAST_DEFAULT", "nws"))
    query = urllib.parse.urlencode({"provider": provider})
    forecast = get_json(f"http://127.0.0.1/api/forecast?{query}")
    if not forecast.get("updated_at"):
        raise RuntimeError(f"No cached {provider} forecast is available")
    return forecast


def empty_forecast() -> dict:
    return {
        "today_high": None, "today_low": None, "today_rain_in": None, "today_rain_chance": None,
        "today_sunrise": "—", "today_sunset": "—", "tomorrow_high": None, "tomorrow_low": None,
        "tomorrow_rain_in": None, "tomorrow_rain_chance": None,
    }


def merge_variables(weather: dict, outlook: dict) -> dict[str, str]:
    pressure = weather.get("pressure_relative_inhg")
    pressure_mbar = round(float(pressure) * 33.8639, 1) if pressure is not None else None
    return {
        "indoor_temp": safe(weather.get("indoor_temp_f"), "°", 1),
        "outdoor_temp": safe(weather.get("outdoor_temp_f"), "°", 1),
        "indoor_humidity": safe(weather.get("indoor_humidity_pct"), "%", 0),
        "outdoor_humidity": safe(weather.get("outdoor_humidity_pct"), "%", 0),
        "co2": "—", "air_quality": "—", "noise": "—",
        "pressure": safe(pressure_mbar, " mbar", 1),
        "rain_1h": safe(weather.get("rain_hour_in"), " in", 3),
        "rain_24h": safe(weather.get("rain_daily_in"), " in", 3),
        "wind_speed": safe(weather.get("wind_speed_mph"), " mph", 1),
        "gust_speed": safe(weather.get("wind_gust_mph"), " mph", 1),
        "today_high": safe(outlook.get("today_high"), "°", 0),
        "today_low": safe(outlook.get("today_low"), "°", 0),
        "today_rain_in": safe(outlook.get("today_rain_in"), " in"),
        "today_rain_chance": safe(outlook.get("today_rain_chance"), "%", 0),
        "tomorrow_rain_in": safe(outlook.get("tomorrow_rain_in"), " in"),
        "tomorrow_rain_chance": safe(outlook.get("tomorrow_rain_chance"), "%", 0),
        "today_sunrise": outlook.get("today_sunrise", "—"),
        "today_sunset": outlook.get("today_sunset", "—"),
        "tomorrow_high": safe(outlook.get("tomorrow_high"), "°", 0),
        "tomorrow_low": safe(outlook.get("tomorrow_low"), "°", 0),
    }


def main() -> int:
    try:
        weather = get_json(LOCAL_WEATHER_URL)
        if not weather.get("received_at"):
            raise RuntimeError("Granada Weather has not received an observation yet")
        try:
            outlook = cached_forecast()
        except (RuntimeError, KeyError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"Forecast unavailable; sending station data only: {exc}", file=sys.stderr)
            outlook = empty_forecast()
        status = post_json(require_env("TERMINAL_WEBHOOK_URL"), {"merge_variables": merge_variables(weather, outlook)})
        print(f"Sent Granada Weather to TRMNL (HTTP {status}; forecast={outlook.get('provider', 'none')})")
        return 0
    except (RuntimeError, KeyError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"TRMNL update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
