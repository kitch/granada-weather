import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import accuracy_collector as accuracy


def forecast_schema(connection):
    connection.executescript("""
        CREATE TABLE forecast_runs (
            id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL
        );
        CREATE TABLE forecast_daily (
            run_id INTEGER NOT NULL,
            valid_date TEXT NOT NULL,
            lead_days INTEGER NOT NULL,
            high_f REAL,
            low_f REAL,
            rain_chance_pct REAL,
            rain_in REAL,
            summary TEXT
        );
    """)
    accuracy.initialize(connection)


def add_forecast(connection, provider, fetched_at, valid_date, high, low, chance, rain):
    cursor = connection.execute(
        "INSERT INTO forecast_runs(provider,issued_at,fetched_at,latitude,longitude) VALUES(?,?,?,?,?)",
        (provider, fetched_at, fetched_at, 35.77, -78.64),
    )
    connection.execute(
        "INSERT INTO forecast_daily VALUES(?,?,?,?,?,?,?,?)",
        (cursor.lastrowid, valid_date, 1, high, low, chance, rain, "Forecast"),
    )


class ObservationAggregationTests(unittest.TestCase):
    def write_rows(self, root, utc_date, rows):
        path = Path(root) / f"{utc_date}.ndjson"
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def test_observations_are_grouped_by_local_date_across_utc_files(self):
        with tempfile.TemporaryDirectory() as root, patch.object(accuracy, "DATA_DIR", Path(root)):
            self.write_rows(root, "2026-08-14", [
                {"observed_at": "2026-08-14T03:30:00+00:00", "outdoor_temp_f": 60, "rain_increment_in": 1},
                {"observed_at": "2026-08-14T04:00:00+00:00", "outdoor_temp_f": 70, "rain_increment_in": 0},
                {"observed_at": "2026-08-14T16:00:00+00:00", "outdoor_temp_f": 95, "rain_increment_in": 0.01},
            ])
            self.write_rows(root, "2026-08-15", [
                {"observed_at": "2026-08-15T03:59:00+00:00", "outdoor_temp_f": 75, "rain_increment_in": 0.02},
                {"observed_at": "2026-08-15T04:30:00+00:00", "outdoor_temp_f": 55, "rain_increment_in": 2},
            ])
            result = accuracy.observed_day(date(2026, 8, 14))
        self.assertEqual(result["samples"], 3)
        self.assertEqual(result["low_f"], 70)
        self.assertEqual(result["high_f"], 95)
        self.assertEqual(result["rain_in"], 0.03)

    def test_incomplete_day_is_not_scored(self):
        with tempfile.TemporaryDirectory() as root, patch.object(accuracy, "DATA_DIR", Path(root)):
            self.write_rows(root, "2026-08-14", [
                {"observed_at": "2026-08-14T12:00:00+00:00", "outdoor_temp_f": 70},
                {"observed_at": "2026-08-14T16:00:00+00:00", "outdoor_temp_f": 80},
            ])
            self.assertIsNone(accuracy.observed_day(date(2026, 8, 14)))

    def test_daily_rain_counter_is_fallback_when_increments_are_missing(self):
        with tempfile.TemporaryDirectory() as root, patch.object(accuracy, "DATA_DIR", Path(root)):
            self.write_rows(root, "2026-08-14", [
                {"observed_at": "2026-08-14T04:00:00+00:00", "outdoor_temp_f": 70, "rain_daily_in": 0},
                {"observed_at": "2026-08-15T03:59:00+00:00", "outdoor_temp_f": 75, "rain_daily_in": 0.24},
            ])
            result = accuracy.observed_day(date(2026, 8, 14))
        self.assertEqual(result["rain_in"], 0.24)


class ForecastSelectionTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        forecast_schema(self.connection)
        self.connection.execute(
            "INSERT INTO observed_daily VALUES(?,?,?,?,?,?,?)",
            ("2026-08-14", 1440, "first", "last", 90, 70, 0.1),
        )

    def tearDown(self):
        self.connection.close()

    def test_latest_forecast_before_local_midnight_is_selected(self):
        add_forecast(self.connection, "weatherkit", "2026-08-14T02:00:00+00:00", "2026-08-14", 88, 68, 20, 0)
        add_forecast(self.connection, "weatherkit", "2026-08-14T03:55:00+00:00", "2026-08-14", 91, 71, 80, 0.2)
        add_forecast(self.connection, "weatherkit", "2026-08-14T04:05:00+00:00", "2026-08-14", 99, 79, 100, 1)
        selected = accuracy.last_prior_forecasts(self.connection)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["forecast_at"], "2026-08-14T03:55:00+00:00")
        self.assertEqual(selected[0]["high_f"], 91)

    def test_winter_midnight_uses_standard_time_cutoff(self):
        self.connection.execute(
            "INSERT INTO observed_daily VALUES(?,?,?,?,?,?,?)",
            ("2026-01-15", 1440, "first", "last", 50, 30, 0),
        )
        add_forecast(self.connection, "nws", "2026-01-15T04:55:00+00:00", "2026-01-15", 51, 31, 0, 0)
        add_forecast(self.connection, "nws", "2026-01-15T05:05:00+00:00", "2026-01-15", 60, 40, 100, 1)
        selected = [row for row in accuracy.last_prior_forecasts(self.connection) if row["date"] == "2026-01-15"]
        self.assertEqual(selected[0]["high_f"], 51)

    def test_scorecard_metrics_and_rain_classification(self):
        add_forecast(self.connection, "nws", "2026-08-14T03:00:00+00:00", "2026-08-14", 92, 68, 80, 0.2)
        payload = accuracy.build_payload(self.connection)
        provider = payload["providers"][0]
        self.assertEqual(payload["scored_days"], 1)
        self.assertEqual(provider["high_mae_f"], 2)
        self.assertEqual(provider["high_bias_f"], 2)
        self.assertEqual(provider["low_mae_f"], 2)
        self.assertEqual(provider["low_bias_f"], -2)
        self.assertEqual(provider["rain_mae_in"], 0.1)
        self.assertEqual(provider["rain_brier"], 0.04)
        self.assertEqual(provider["wet_accuracy_pct"], 100)


if __name__ == "__main__":
    unittest.main()

