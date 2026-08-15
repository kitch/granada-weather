#!/usr/bin/env python3
"""Score archived daily forecasts against completed local station days."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


DATA_DIR = Path(os.environ.get("WEATHER_DATA_DIR", "/var/lib/pi-weather"))
DATABASE = DATA_DIR / "forecast-history.sqlite3"
OUTPUT = DATA_DIR / "forecast-accuracy.json"
LOCAL_ZONE = ZoneInfo(os.environ.get("WEATHER_TIMEZONE", "America/New_York"))
WET_THRESHOLD_IN = 0.01
MIN_DAY_SPAN_HOURS = 20
PROVIDER_NAMES = {
    "nws": "National Weather Service",
    "openmeteo_nbm": "NOAA NBM",
    "openmeteo_hrrr": "NOAA HRRR",
    "openmeteo_ecmwf": "ECMWF",
    "openweather": "OpenWeather",
    "wunderground": "Weather Underground",
    "weatherkit": "Apple Weather",
}


def finite(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def utc_files_for(local_day: date) -> list[Path]:
    start = datetime.combine(local_day, time.min, LOCAL_ZONE).astimezone(timezone.utc).date()
    end = datetime.combine(local_day + timedelta(days=1), time.min, LOCAL_ZONE).astimezone(timezone.utc).date()
    files = []
    cursor = start
    while cursor <= end:
        files.append(DATA_DIR / f"{cursor.isoformat()}.ndjson")
        cursor += timedelta(days=1)
    return files


def observed_day(local_day: date) -> dict | None:
    rows = []
    for path in utc_files_for(local_day):
        try:
            handle = path.open(encoding="utf-8")
        except FileNotFoundError:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stamp = parse_timestamp(row.get("observed_at") or row.get("received_at"))
                if stamp and stamp.astimezone(LOCAL_ZONE).date() == local_day:
                    rows.append((stamp, row))
    if not rows:
        return None
    rows.sort(key=lambda item: item[0])
    span_hours = (rows[-1][0] - rows[0][0]).total_seconds() / 3600
    temperatures = [finite(row.get("outdoor_temp_f")) for _, row in rows]
    temperatures = [value for value in temperatures if value is not None]
    if not temperatures or span_hours < MIN_DAY_SPAN_HOURS:
        return None
    increments = [finite(row.get("rain_increment_in")) for _, row in rows]
    increments = [max(0.0, value) for value in increments if value is not None]
    daily = [finite(row.get("rain_daily_in")) for _, row in rows]
    daily = [value for value in daily if value is not None]
    rain = sum(increments) if increments else (max(daily) if daily else 0.0)
    return {
        "valid_date": local_day.isoformat(),
        "samples": len(rows),
        "first_at": rows[0][0].isoformat(timespec="seconds"),
        "last_at": rows[-1][0].isoformat(timespec="seconds"),
        "high_f": round(max(temperatures), 1),
        "low_f": round(min(temperatures), 1),
        "rain_in": round(rain, 3),
    }


def initialize(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS observed_daily (
            valid_date TEXT PRIMARY KEY,
            samples INTEGER NOT NULL,
            first_at TEXT NOT NULL,
            last_at TEXT NOT NULL,
            high_f REAL NOT NULL,
            low_f REAL NOT NULL,
            rain_in REAL NOT NULL
        )
    """)


def backfill_observations(connection: sqlite3.Connection) -> None:
    yesterday = datetime.now(LOCAL_ZONE).date() - timedelta(days=1)
    candidates = set()
    for path in DATA_DIR.glob("????-??-??.ndjson"):
        try:
            utc_day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        candidates.update((utc_day - timedelta(days=1), utc_day))
    existing = {date.fromisoformat(row[0]) for row in connection.execute("SELECT valid_date FROM observed_daily")}
    for local_day in sorted(day for day in candidates if day <= yesterday and day not in existing):
        actual = observed_day(local_day)
        if not actual:
            continue
        connection.execute(
            """INSERT OR REPLACE INTO observed_daily
               (valid_date,samples,first_at,last_at,high_f,low_f,rain_in)
               VALUES(:valid_date,:samples,:first_at,:last_at,:high_f,:low_f,:rain_in)""",
            actual,
        )


def last_prior_forecasts(connection: sqlite3.Connection) -> list[dict]:
    # Select in Python because SQLite's UTC modifier does not understand the
    # configured local zone or daylight-saving transitions.
    raw = connection.execute("""
        SELECT o.valid_date,o.high_f,o.low_f,o.rain_in,r.provider,r.fetched_at,
               d.high_f,d.low_f,d.rain_chance_pct,d.rain_in
          FROM observed_daily o
          JOIN forecast_daily d ON d.valid_date=o.valid_date
          JOIN forecast_runs r ON r.id=d.run_id
         ORDER BY o.valid_date,r.provider,r.fetched_at
    """).fetchall()
    chosen = {}
    for row in raw:
        local_day = date.fromisoformat(row[0])
        cutoff = datetime.combine(local_day, time.min, LOCAL_ZONE).astimezone(timezone.utc)
        fetched = parse_timestamp(row[5])
        if fetched and fetched < cutoff:
            chosen[(row[0], row[4])] = row
    return [
        {
            "date": row[0], "actual_high_f": row[1], "actual_low_f": row[2],
            "actual_rain_in": row[3], "provider": row[4], "forecast_at": row[5],
            "high_f": row[6], "low_f": row[7], "rain_chance_pct": row[8], "rain_in": row[9],
        }
        for row in chosen.values()
    ]


def average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def build_payload(connection: sqlite3.Connection) -> dict:
    rows = last_prior_forecasts(connection)
    provider_ids = [row[0] for row in connection.execute("SELECT DISTINCT provider FROM forecast_runs ORDER BY provider")]
    grouped = defaultdict(list)
    by_day = defaultdict(list)
    actuals = {}
    for row in rows:
        high_error = finite(row["high_f"])
        high_error = high_error - row["actual_high_f"] if high_error is not None else None
        low_error = finite(row["low_f"])
        low_error = low_error - row["actual_low_f"] if low_error is not None else None
        rain_error = finite(row["rain_in"])
        rain_error = rain_error - row["actual_rain_in"] if rain_error is not None else None
        chance = finite(row["rain_chance_pct"])
        wet = row["actual_rain_in"] >= WET_THRESHOLD_IN
        predicted_wet = chance is not None and chance >= 50
        scored = dict(row)
        scored.update({
            "high_error_f": round(high_error, 1) if high_error is not None else None,
            "low_error_f": round(low_error, 1) if low_error is not None else None,
            "rain_error_in": round(rain_error, 3) if rain_error is not None else None,
            "actual_wet": wet,
            "predicted_wet": predicted_wet if chance is not None else None,
            "brier": round((chance / 100 - int(wet)) ** 2, 4) if chance is not None else None,
        })
        grouped[row["provider"]].append(scored)
        by_day[row["date"]].append(scored)
        actuals[row["date"]] = {
            "high_f": row["actual_high_f"], "low_f": row["actual_low_f"], "rain_in": row["actual_rain_in"],
        }

    providers = []
    for provider in provider_ids:
        items = grouped.get(provider, [])
        high_errors = [item["high_error_f"] for item in items if item["high_error_f"] is not None]
        low_errors = [item["low_error_f"] for item in items if item["low_error_f"] is not None]
        rain_errors = [item["rain_error_in"] for item in items if item["rain_error_in"] is not None]
        briers = [item["brier"] for item in items if item["brier"] is not None]
        wet_results = [item["predicted_wet"] == item["actual_wet"] for item in items if item["predicted_wet"] is not None]
        providers.append({
            "id": provider,
            "name": PROVIDER_NAMES.get(provider, provider.replace("_", " ").title()),
            "days": len({item["date"] for item in items}),
            "high_mae_f": average([abs(value) for value in high_errors]),
            "high_bias_f": average(high_errors),
            "low_mae_f": average([abs(value) for value in low_errors]),
            "low_bias_f": average(low_errors),
            "rain_mae_in": average([abs(value) for value in rain_errors]),
            "rain_brier": average(briers),
            "wet_accuracy_pct": round(sum(wet_results) / len(wet_results) * 100, 1) if wet_results else None,
        })
    days = [
        {"date": valid_date, "actual": actuals[valid_date], "providers": by_day[valid_date]}
        for valid_date in sorted(by_day, reverse=True)[:90]
    ]
    observed = connection.execute("SELECT min(valid_date),max(valid_date),count(*) FROM observed_daily").fetchone()
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "timezone": str(LOCAL_ZONE),
        "method": "Last forecast collected before local midnight; completed local station days only.",
        "wet_threshold_in": WET_THRESHOLD_IN,
        "observed_days": observed[2] if observed else 0,
        "observed_from": observed[0] if observed else None,
        "observed_through": observed[1] if observed else None,
        "scored_days": len(by_day),
        "providers": providers,
        "days": days,
    }


def atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DATABASE, timeout=30) as connection:
        initialize(connection)
        backfill_observations(connection)
        connection.commit()
        payload = build_payload(connection)
    atomic_json(OUTPUT, payload)
    print(f"Updated forecast accuracy: {payload['scored_days']} scored days, {len(payload['providers'])} providers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
