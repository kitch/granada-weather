#!/usr/bin/env python3
"""Tiny Ambient/Ecowitt receiver and weather dashboard for a Raspberry Pi Zero."""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import queue
import re
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA_DIR = Path(os.environ.get("WEATHER_DATA_DIR", ROOT / "data"))
LATEST = DATA_DIR / "latest.json"
FORECAST = DATA_DIR / "forecast.json"
FORECAST_DIR = DATA_DIR / "forecasts"
FORECAST_PROVIDERS = FORECAST_DIR / "providers.json"
FORECAST_ACCURACY = DATA_DIR / "forecast-accuracy.json"
WEATHERKIT_ATTRIBUTION_LOGO = DATA_DIR / "weatherkit-attribution.png"
ROLLUP_DIR = DATA_DIR / "hourly"
WRITE_LOCK = threading.Lock()
ROLLUP_LOCK = threading.Lock()
WU_STATUS_LOCK = threading.Lock()
WU_QUEUE: queue.Queue[dict] = queue.Queue(maxsize=1)
ROLLUP_VERSION = 2
LOCAL_TIME = ZoneInfo(os.environ.get("WEATHER_TIMEZONE", "America/New_York"))
MAX_BODY = 64 * 1024
RECEIVER_PATHS = ("/data/report", "/receive", "/updateweatherstation.php")
WU_UPLOAD_URL = os.environ.get(
    "WUNDERGROUND_UPLOAD_URL",
    "https://weatherstation.wunderground.com/weatherstation/updateweatherstation.php",
)
WU_FIELD_MAP = {
    "outdoor_temp_f": "tempf",
    "outdoor_humidity_pct": "humidity",
    "dew_point_f": "dewptf",
    "wind_chill_f": "windchillf",
    "wind_direction_deg": "winddir",
    "wind_speed_mph": "windspeedmph",
    "wind_gust_mph": "windgustmph",
    "rain_hour_in": "rainin",
    "rain_daily_in": "dailyrainin",
    "pressure_relative_inhg": "baromin",
    "solar_radiation_wm2": "solarradiation",
    "uv_index": "UV",
}
WU_STATUS = {
    "configured": bool(os.environ.get("WUNDERGROUND_STATION_ID") and os.environ.get("WUNDERGROUND_STATION_KEY")),
    "last_attempt_at": None,
    "last_success_at": None,
    "last_error": None,
}
PUBLIC_FIELDS = {
    "received_at", "observed_at", "outdoor_temp_f", "outdoor_humidity_pct",
    "indoor_temp_f", "indoor_humidity_pct",
    "dew_point_f", "wind_chill_f", "wind_speed_mph", "wind_gust_mph",
    "wind_direction_deg", "pressure_relative_inhg", "solar_radiation_wm2",
    "uv_index", "rain_hour_in", "rain_rate_in_hr", "rain_daily_in",
    "rain_weekly_in", "rain_monthly_in", "rain_yearly_in",
    "rainfall_in", "rain_bucket_seconds", "_range",
}

ROLLUP_MAX_FIELDS = {"wind_gust_mph", "solar_radiation_wm2", "uv_index", "rain_hour_in", "rain_rate_in_hr"}
ROLLUP_LAST_FIELDS = {
    "wind_direction_deg", "rain_daily_in", "rain_weekly_in",
    "rain_monthly_in", "rain_yearly_in",
}

# Ecowitt/Ambient payload keys retained verbatim, with these friendlier aliases.
ALIASES = {
    "tempf": "outdoor_temp_f",
    "humidity": "outdoor_humidity_pct",
    "tempinf": "indoor_temp_f",
    "indoortempf": "indoor_temp_f",
    "humidityin": "indoor_humidity_pct",
    "indoorhumidity": "indoor_humidity_pct",
    "baromrelin": "pressure_relative_inhg",
    "baromin": "pressure_relative_inhg",
    "baromabsin": "pressure_absolute_inhg",
    "absbaromin": "pressure_absolute_inhg",
    "windspeedmph": "wind_speed_mph",
    "windgustmph": "wind_gust_mph",
    "winddir": "wind_direction_deg",
    "rainratein": "rain_rate_in_hr",
    "dailyrainin": "rain_daily_in",
    "weeklyrainin": "rain_weekly_in",
    "monthlyrainin": "rain_monthly_in",
    "yearlyrainin": "rain_yearly_in",
    "solarradiation": "solar_radiation_wm2",
    "uv": "uv_index",
    "dewptf": "dew_point_f",
    "windchillf": "wind_chill_f",
    # The WS-2000's Wunderground-compatible upload uses rainin as a recent
    # rainfall rate, not a true rolling-hour accumulation. Preserve it as a
    # rate and derive rain_hour_in from the cumulative counter in store().
    "rainin": "rain_rate_in_hr",
    "lowbatt": "battery_low",
}

NUMERIC_FIELDS = set(ALIASES.values()) | {"realtime", "rtfreq"}
FIELD_RANGES = {
    "outdoor_temp_f": (-100, 160),
    "indoor_temp_f": (-20, 160),
    "outdoor_humidity_pct": (0, 100),
    "indoor_humidity_pct": (0, 100),
    "dew_point_f": (-120, 160),
    "wind_chill_f": (-150, 160),
    "pressure_relative_inhg": (20, 35),
    "pressure_absolute_inhg": (20, 35),
    "wind_speed_mph": (0, 200),
    "wind_gust_mph": (0, 200),
    "wind_direction_deg": (0, 360),
    "rain_rate_in_hr": (0, 30),
    "rain_hour_in": (0, 30),
    "rain_daily_in": (0, 100),
    "rain_weekly_in": (0, 500),
    "rain_monthly_in": (0, 500),
    "rain_yearly_in": (0, 1000),
    "solar_radiation_wm2": (0, 2000),
    "uv_index": (0, 30),
    "battery_low": (0, 1),
}
REQUIRED_WEATHER_FIELDS = {
    "outdoor_temp_f", "outdoor_humidity_pct", "pressure_relative_inhg", "rain_yearly_in",
}
STATION_ID_FIELDS = ("id", "stationid", "station_id")
STATION_KEY_FIELDS = ("password", "passkey", "stationkey", "station_key")
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def scalar(value: str):
    value = value.strip()
    if not value:
        return ""
    try:
        return float(value) if any(c in value for c in ".eE") else int(value)
    except ValueError:
        return value


def number(value: str):
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return int(parsed) if parsed.is_integer() else parsed


def numeric_value_valid(field: str, value: int | float) -> bool:
    bounds = FIELD_RANGES.get(field)
    return bounds is None or bounds[0] <= value <= bounds[1]


def station_credentials_valid(fields: dict[str, str]) -> bool:
    expected_id = os.environ.get("WEATHER_STATION_ID", "")
    expected_key = os.environ.get("WEATHER_STATION_KEY", "")
    if not expected_id or not expected_key:
        return False
    lowered = {key.lower(): str(value) for key, value in fields.items()}
    supplied_id = next((lowered[key] for key in STATION_ID_FIELDS if key in lowered), "")
    supplied_key = next((lowered[key] for key in STATION_KEY_FIELDS if key in lowered), "")
    return hmac.compare_digest(supplied_id, expected_id) and hmac.compare_digest(supplied_key, expected_key)


def normalize(fields: dict[str, str]) -> dict:
    observed = datetime.now(timezone.utc)
    raw_date = fields.get("dateutc", "")
    if raw_date and raw_date.lower() != "now":
        try:
            observed = datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    reading = {
        "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "observed_at": observed.isoformat(timespec="seconds"),
    }
    for key, value in fields.items():
        clean_key = re.sub(r"[^a-zA-Z0-9_]", "_", key).lower()
        if clean_key in {*STATION_ID_FIELDS, *STATION_KEY_FIELDS}:
            continue
        normalized_key = ALIASES.get(clean_key, clean_key)
        if normalized_key in NUMERIC_FIELDS:
            parsed_number = number(value)
            if parsed_number is not None and numeric_value_valid(normalized_key, parsed_number):
                reading[normalized_key] = parsed_number
        else:
            reading[normalized_key] = scalar(value)
    return reading


def reading_is_usable(reading: dict) -> bool:
    return all(
        isinstance(reading.get(field), (int, float)) and math.isfinite(reading[field])
        for field in REQUIRED_WEATHER_FIELDS
    )


def process_station_fields(fields: dict[str, str]) -> bool:
    reading = normalize(fields)
    if not reading_is_usable(reading):
        print(f"Discarded unusable station packet at {reading['received_at']}")
        return False
    store(reading)
    enqueue_wunderground(reading)
    return True


def store(reading: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with WRITE_LOCK:
        previous = read_latest()
        previous_total = previous and previous.get("rain_yearly_in")
        current_total = reading.get("rain_yearly_in")
        if isinstance(current_total, (int, float)) and isinstance(previous_total, (int, float)):
            delta = current_total - previous_total
            reading["rain_increment_in"] = round(delta if delta >= 0 else current_total, 4)
        observed = None
        try:
            observed = datetime.fromisoformat(reading["observed_at"]).timestamp()
            recent = read_raw_range(observed - 3600, observed)
        except (KeyError, ValueError):
            recent = []
        increments = [row.get("rain_increment_in") for row in recent]
        increments.append(reading.get("rain_increment_in"))
        rain_hour = sum(
            float(value) for value in increments
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
        )
        daily_rain = reading.get("rain_daily_in")
        if observed is not None and isinstance(daily_rain, (int, float)) and math.isfinite(daily_rain):
            cutoff_day = datetime.fromtimestamp(observed - 3600, LOCAL_TIME).date()
            observed_day = datetime.fromtimestamp(observed, LOCAL_TIME).date()
            if cutoff_day == observed_day:
                rain_hour = min(rain_hour, float(daily_rain))
        reading["rain_hour_in"] = round(rain_hour, 4)
        day = reading["observed_at"][:10]
        line = json.dumps(reading, separators=(",", ":"), sort_keys=True)
        with (DATA_DIR / f"{day}.ndjson").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        temp = DATA_DIR / ".latest.tmp"
        temp.write_text(line + "\n", encoding="utf-8")
        temp.replace(LATEST)


def wunderground_payload(reading: dict) -> dict[str, str]:
    payload = {
        "ID": os.environ.get("WUNDERGROUND_STATION_ID", ""),
        "PASSWORD": os.environ.get("WUNDERGROUND_STATION_KEY", ""),
        "dateutc": "now",
        "action": "updateraw",
        "softwaretype": "GranadaWeather",
    }
    for source, target in WU_FIELD_MAP.items():
        value = reading.get(source)
        if isinstance(value, (int, float)) and math.isfinite(value):
            payload[target] = str(value)
    return payload


def enqueue_wunderground(reading: dict) -> None:
    if not WU_STATUS["configured"]:
        return
    payload = wunderground_payload(reading)
    try:
        WU_QUEUE.put_nowait(payload)
    except queue.Full:
        try:
            WU_QUEUE.get_nowait()
            WU_QUEUE.task_done()
        except queue.Empty:
            pass
        try:
            WU_QUEUE.put_nowait(payload)
        except queue.Full:
            pass


def wunderground_worker() -> None:
    while True:
        payload = WU_QUEUE.get()
        attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        error = None
        try:
            request = Request(
                f"{WU_UPLOAD_URL}?{urlencode(payload)}",
                headers={"User-Agent": "GranadaWeather/1.0"},
            )
            with urlopen(request, timeout=12) as response:
                body = response.read(256).decode("utf-8", errors="replace").strip()
                if response.status < 200 or response.status >= 300 or not body.lower().startswith("success"):
                    error = f"unexpected response ({response.status}): {body[:120]}"
        except HTTPError as exc:
            error = f"HTTP {exc.code}"
        except (URLError, TimeoutError, OSError) as exc:
            error = type(exc).__name__
        with WU_STATUS_LOCK:
            WU_STATUS["last_attempt_at"] = attempted_at
            WU_STATUS["last_error"] = error
            if error is None:
                WU_STATUS["last_success_at"] = attempted_at
        if error:
            print(f"Weather Underground forward failed: {error}")
        WU_QUEUE.task_done()


def read_latest() -> dict | None:
    try:
        return json.loads(LATEST.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_forecast(provider: str | None = None) -> dict | None:
    path = FORECAST
    if provider and re.fullmatch(r"[a-z0-9_]+", provider):
        candidate = FORECAST_DIR / f"{provider}.json"
        if candidate.exists():
            path = candidate
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_forecast_providers() -> dict:
    try:
        return json.loads(FORECAST_PROVIDERS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        forecast = read_forecast()
        if not forecast:
            return {"default": None, "providers": []}
        return {
            "default": forecast.get("provider"),
            "providers": [{
                "id": forecast.get("provider", "openweather"),
                "name": forecast.get("provider_name", "OpenWeather"),
                "updated_at": forecast.get("updated_at"),
                "attribution": forecast.get("attribution", {}),
            }],
        }


def public_forecast_providers() -> dict:
    providers = read_forecast_providers()
    return {"default": providers.get("default"), "providers": providers.get("providers", [])}


def read_forecast_accuracy() -> dict:
    try:
        return json.loads(FORECAST_ACCURACY.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"updated_at": None, "scored_days": 0, "providers": [], "days": []}


def dated_files(start: float, end: float) -> list[Path]:
    day = datetime.fromtimestamp(start, timezone.utc).date()
    final_day = datetime.fromtimestamp(end, timezone.utc).date()
    files: list[Path] = []
    while day <= final_day:
        files.append(DATA_DIR / f"{day.isoformat()}.ndjson")
        day += timedelta(days=1)
    return files


def read_raw_range(start: float, end: float) -> list[dict]:
    rows: list[dict] = []
    for path in dated_files(start, end):
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                        timestamp = datetime.fromisoformat(row["observed_at"]).timestamp()
                        if start <= timestamp <= end:
                            rows.append(row)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
        except FileNotFoundError:
            continue
    return rows


def previous_rain_total(path: Path) -> float | None:
    previous_path = path.with_name((datetime.fromisoformat(path.stem).date() - timedelta(days=1)).isoformat() + ".ndjson")
    try:
        with previous_path.open(encoding="utf-8") as handle:
            for line in reversed(handle.readlines()):
                try:
                    value = json.loads(line).get("rain_yearly_in")
                    if isinstance(value, (int, float)) and math.isfinite(value):
                        return float(value)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return None


def rain_increment(row: dict, previous_total: float | None) -> tuple[float, float | None]:
    total = row.get("rain_yearly_in")
    if not isinstance(total, (int, float)) or not math.isfinite(total):
        return 0.0, previous_total
    stored = row.get("rain_increment_in")
    if isinstance(stored, (int, float)) and math.isfinite(stored) and stored >= 0:
        return float(stored), float(total)
    if previous_total is None:
        return 0.0, float(total)
    delta = float(total) - previous_total
    return max(0.0, delta if delta >= 0 else float(total)), float(total)


def build_hourly_rollup(path: Path) -> list[dict]:
    buckets: dict[int, dict] = {}
    prior_rain = previous_rain_total(path)
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    timestamp = datetime.fromisoformat(row["observed_at"]).timestamp()
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                hour = int(timestamp // 3600)
                bucket = buckets.setdefault(hour, {"last": row, "metrics": {}})
                bucket["last"] = row
                increment, prior_rain = rain_increment(row, prior_rain)
                bucket["rainfall_in"] = bucket.get("rainfall_in", 0.0) + increment
                for key in PUBLIC_FIELDS - {"received_at", "observed_at", "_range"}:
                    value = row.get(key)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        continue
                    metric = bucket["metrics"].setdefault(
                        key, {"sum": 0.0, "count": 0, "min": value, "max": value, "last": value}
                    )
                    metric["sum"] += value
                    metric["count"] += 1
                    metric["min"] = min(metric["min"], value)
                    metric["max"] = max(metric["max"], value)
                    metric["last"] = value
    except FileNotFoundError:
        return []

    rows: list[dict] = []
    for hour, bucket in sorted(buckets.items()):
        row = {
            "observed_at": datetime.fromtimestamp(hour * 3600, timezone.utc).isoformat(timespec="seconds"),
            "received_at": bucket["last"].get("received_at"),
            "_range": {},
        }
        for key, metric in bucket["metrics"].items():
            if key in ROLLUP_MAX_FIELDS:
                row[key] = metric["max"]
            elif key in ROLLUP_LAST_FIELDS:
                row[key] = metric["last"]
            else:
                row[key] = metric["sum"] / metric["count"]
            row["_range"][key] = [metric["min"], metric["max"]]
        row["rainfall_in"] = round(bucket.get("rainfall_in", 0.0), 4)
        rows.append(row)
    return rows


def read_hourly_day(path: Path) -> list[dict]:
    ROLLUP_DIR.mkdir(parents=True, exist_ok=True)
    cache = ROLLUP_DIR / f"{path.stem}.v{ROLLUP_VERSION}.json"
    with ROLLUP_LOCK:
        try:
            if cache.stat().st_mtime_ns >= path.stat().st_mtime_ns:
                return json.loads(cache.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        rows = build_hourly_rollup(path)
        if path.exists():
            temp = ROLLUP_DIR / f".{path.stem}.{threading.get_ident()}.tmp"
            temp.write_text(json.dumps(rows, separators=(",", ":")) + "\n", encoding="utf-8")
            temp.replace(cache)
        return rows


def merge_ranges(previous: dict, current: dict) -> dict:
    merged = dict(previous)
    for key, values in current.items():
        if key in merged:
            merged[key] = [min(merged[key][0], values[0]), max(merged[key][1], values[1])]
        else:
            merged[key] = values
    return merged


def downsample(rows: list[dict], start: float, end: float, limit: int) -> list[dict]:
    bucket_seconds = max(1, math.ceil((end - start) / limit))
    sampled: list[dict] = []
    last_bucket = None
    for row in rows:
        try:
            timestamp = datetime.fromisoformat(row["observed_at"]).timestamp()
        except (KeyError, ValueError):
            continue
        bucket = int((timestamp - start) // bucket_seconds)
        if bucket == last_bucket:
            ranges = merge_ranges(sampled[-1].get("_range", {}), row.get("_range", {}))
            sampled[-1] = row
            if ranges:
                sampled[-1]["_range"] = ranges
        else:
            sampled.append(row)
            last_bucket = bucket
    return sampled


def rainfall_bucket_seconds(hours: int) -> int:
    if hours <= 24:
        return 15 * 60
    if hours <= 24 * 7:
        return 60 * 60
    if hours <= 24 * 30:
        return 24 * 60 * 60
    return 7 * 24 * 60 * 60


def rainfall_bucket_start(timestamp: float, bucket_seconds: int) -> float:
    local = datetime.fromtimestamp(timestamp, LOCAL_TIME)
    if bucket_seconds == 15 * 60:
        local = local.replace(minute=(local.minute // 15) * 15, second=0, microsecond=0)
    elif bucket_seconds == 60 * 60:
        local = local.replace(minute=0, second=0, microsecond=0)
    elif bucket_seconds == 24 * 60 * 60:
        local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        local = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.timestamp()


def rainfall_bars(rows: list[dict], start: float, end: float, hours: int, raw: bool) -> list[dict]:
    seconds = rainfall_bucket_seconds(hours)
    totals: dict[float, float] = {}
    prior_rain = None
    if raw:
        for row in rows:
            try:
                timestamp = datetime.fromisoformat(row["observed_at"]).timestamp()
            except (KeyError, ValueError):
                continue
            increment, prior_rain = rain_increment(row, prior_rain)
            if start <= timestamp <= end:
                bucket = rainfall_bucket_start(timestamp, seconds)
                totals[bucket] = totals.get(bucket, 0.0) + increment
    else:
        for row in rows:
            try:
                timestamp = datetime.fromisoformat(row["observed_at"]).timestamp()
                increment = float(row.get("rainfall_in", 0))
            except (KeyError, ValueError, TypeError):
                continue
            if start <= timestamp <= end and math.isfinite(increment):
                bucket = rainfall_bucket_start(timestamp, seconds)
                totals[bucket] = totals.get(bucket, 0.0) + max(0.0, increment)

    bucket = rainfall_bucket_start(start, seconds)
    bars: list[dict] = []
    while bucket <= end:
        local = datetime.fromtimestamp(bucket, LOCAL_TIME)
        next_bucket = (local + timedelta(seconds=seconds)).timestamp()
        bars.append({
            "observed_at": datetime.fromtimestamp(bucket, timezone.utc).isoformat(timespec="seconds"),
            "rainfall_in": round(totals.get(bucket, 0.0), 4),
            "rain_bucket_seconds": round(next_bucket - bucket),
        })
        bucket = next_bucket
    return bars


def merge_rainfall(rows: list[dict], bars: list[dict]) -> list[dict]:
    merged = []
    for source in rows:
        row = dict(source)
        row.pop("rainfall_in", None)
        row.pop("rain_bucket_seconds", None)
        merged.append(row)
    merged.extend(bars)
    return sorted(merged, key=lambda row: row.get("observed_at", ""))


def read_history(hours: int, end: float | None = None, limit: int = 2000) -> list[dict]:
    end = min(time.time(), end or time.time())
    start = end - hours * 3600
    if hours <= 24 * 31:
        source = read_raw_range(start - 24 * 3600, end)
        visible = [row for row in source if datetime.fromisoformat(row["observed_at"]).timestamp() >= start]
        return merge_rainfall(downsample(visible, start, end, limit), rainfall_bars(source, start, end, hours, True))

    rows: list[dict] = []
    for path in dated_files(start, end):
        for row in read_hourly_day(path):
            try:
                timestamp = datetime.fromisoformat(row["observed_at"]).timestamp()
                if start <= timestamp <= end:
                    rows.append(row)
            except (KeyError, ValueError):
                continue
    sampled = downsample(rows, start, end, limit)
    bars = rainfall_bars(rows, start, end, hours, False)
    latest = read_latest()
    if latest and end >= time.time() - 180:
        try:
            if datetime.fromisoformat(latest["observed_at"]).timestamp() >= start:
                sampled.append(latest)
        except (KeyError, ValueError):
            pass
    return merge_rainfall(sampled, bars)


def history_parameters(query: dict[str, list[str]]) -> tuple[int, float]:
    try:
        hours = min(24 * 366, max(1, int(query.get("hours", [24])[-1])))
    except ValueError:
        hours = 24
    end = time.time()
    raw_end = query.get("end", [""])[-1]
    if raw_end:
        try:
            end = datetime.fromisoformat(raw_end.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return hours, min(time.time(), end)


def public_reading(reading: dict | None) -> dict:
    if not reading:
        return {}
    return {key: value for key, value in reading.items() if key in PUBLIC_FIELDS}


class Handler(BaseHTTPRequestHandler):
    server_version = "GranadaWeather"
    sys_version = ""
    allowed_methods = "GET, POST"

    def log_message(self, fmt: str, *args) -> None:
        status = args[1] if len(args) > 1 else "-"
        path = urlparse(self.path).path
        path = next((receiver for receiver in RECEIVER_PATHS if path.startswith(receiver)), path)
        print(f"{self.address_string()} {self.command} {path} {status}")

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", self.allowed_methods)
        self.send_header("Content-Type", "application/json")
        body = b'{"error":"method not allowed"}'
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, data, status=HTTPStatus.OK) -> None:
        body = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        matched_path = next((path for path in RECEIVER_PATHS if parsed.path.startswith(path)), None)
        if matched_path:
            # Some WS-2000 firmware omits the '?' and concatenates its WU query
            # directly onto the configured path: /data/reportID=...&tempf=...
            query = parsed.query or parsed.path[len(matched_path):].lstrip("?")
            fields = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}
            if not fields:
                self.json_response({"error": "no weather fields supplied"}, HTTPStatus.BAD_REQUEST)
                return
            if not station_credentials_valid(fields):
                self.json_response({"error": "invalid station credentials"}, HTTPStatus.FORBIDDEN)
                return
            process_station_fields(fields)
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        elif parsed.path == "/api/current":
            self.json_response(read_latest() or {})
        elif parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            hours, end = history_parameters(query)
            self.json_response(read_history(hours, end))
        elif parsed.path == "/api/forecast":
            query = parse_qs(parsed.query)
            self.json_response(read_forecast(query.get("provider", [None])[-1]) or {})
        elif parsed.path == "/api/forecast/providers":
            self.json_response(public_forecast_providers())
        elif parsed.path == "/api/forecast/accuracy":
            self.json_response(read_forecast_accuracy())
        elif parsed.path == "/api/forecast/attribution-logo":
            self.serve_weatherkit_logo()
        elif parsed.path == "/api/health":
            latest = read_latest()
            with WU_STATUS_LOCK:
                wu_status = dict(WU_STATUS)
            self.json_response({
                "ok": True,
                "has_data": bool(latest),
                "latest": latest and latest.get("received_at"),
                "wunderground": wu_status,
            })
        else:
            self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in RECEIVER_PATHS:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        try:
            if "application/json" in content_type:
                payload = json.loads(raw)
                fields = {str(k): str(v) for k, v in payload.items()}
            else:
                parsed_form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                fields = {k: v[-1] for k, v in parsed_form.items()}
            if not station_credentials_valid(fields):
                self.json_response({"error": "invalid station credentials"}, HTTPStatus.FORBIDDEN)
                return
            process_station_fields(fields)
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            self.send_error(HTTPStatus.BAD_REQUEST)

    do_HEAD = method_not_allowed
    do_OPTIONS = method_not_allowed
    do_TRACE = method_not_allowed
    do_PUT = method_not_allowed
    do_DELETE = method_not_allowed
    do_PATCH = method_not_allowed

    def serve_weatherkit_logo(self) -> None:
        try:
            body = WEATHERKIT_ATTRIBUTION_LOGO.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, path: str) -> None:
        names = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/accuracy": ("accuracy.html", "text/html; charset=utf-8"),
                 "/accuracy.html": ("accuracy.html", "text/html; charset=utf-8"),
                 "/app.css": ("app.css", "text/css; charset=utf-8"),
                 "/accuracy.css": ("accuracy.css", "text/css; charset=utf-8"),
                 "/buttons.css": ("buttons.css", "text/css; charset=utf-8"),
                 "/forecast.css": ("forecast.css", "text/css; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
                 "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
                 "/icon-192.png": ("icon-192.png", "image/png"),
                 "/icon-512.png": ("icon-512.png", "image/png"),
                 "/favicon.png": ("favicon.png", "image/png")}
        names["/forecast.js"] = ("forecast.js", "text/javascript; charset=utf-8")
        names["/accuracy.js"] = ("accuracy.js", "text/javascript; charset=utf-8")
        item = names.get(path)
        if not item:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        file_name, mime = item
        body = (STATIC / file_name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


class PublicHandler(Handler):
    """Read-only, privacy-filtered surface intended for Cloudflare Tunnel."""

    allowed_methods = "GET"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/current":
            self.json_response(public_reading(read_latest()))
        elif parsed.path == "/api/history":
            query = parse_qs(parsed.query)
            hours, end = history_parameters(query)
            self.json_response([public_reading(row) for row in read_history(hours, end)])
        elif parsed.path == "/api/forecast":
            query = parse_qs(parsed.query)
            self.json_response(read_forecast(query.get("provider", [None])[-1]) or {})
        elif parsed.path == "/api/forecast/providers":
            self.json_response(public_forecast_providers())
        elif parsed.path == "/api/forecast/accuracy":
            self.json_response(read_forecast_accuracy())
        elif parsed.path == "/api/forecast/attribution-logo":
            self.serve_weatherkit_logo()
        elif parsed.path == "/api/health":
            latest = read_latest()
            self.json_response({"ok": True, "has_data": bool(latest)})
        elif parsed.path in {
            "/", "/accuracy", "/accuracy.html", "/app.css", "/accuracy.css", "/buttons.css", "/forecast.css", "/app.js", "/forecast.js", "/accuracy.js",
            "/manifest.webmanifest", "/apple-touch-icon.png", "/icon-192.png", "/icon-512.png", "/favicon.png",
        }:
            self.serve_static(parsed.path)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        self.method_not_allowed()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEATHER_PORT", "8080")))
    parser.add_argument("--public-host", default="127.0.0.1")
    parser.add_argument("--public-port", type=int, default=int(os.environ.get("WEATHER_PUBLIC_PORT", "8081")))
    args = parser.parse_args()
    if WU_STATUS["configured"]:
        threading.Thread(
            target=wunderground_worker,
            name="wunderground-forwarder",
            daemon=True,
        ).start()
        print("Weather Underground forwarding enabled")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    public_server = ThreadingHTTPServer((args.public_host, args.public_port), PublicHandler)
    public_thread = threading.Thread(target=public_server.serve_forever, name="public-weather", daemon=True)
    public_thread.start()

    def stop(*_) -> None:
        threading.Thread(target=server.shutdown).start()
        threading.Thread(target=public_server.shutdown).start()

    signal.signal(signal.SIGTERM, stop)
    print(f"Pi Weather listening on http://{args.host}:{args.port}")
    print(f"Public read-only weather listening on http://{args.public_host}:{args.public_port}")
    server.serve_forever()
    public_server.shutdown()


if __name__ == "__main__":
    main()
