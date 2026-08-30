import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import forecast_collector as forecast


class ConversionTests(unittest.TestCase):
    def test_units_and_non_finite_values(self):
        self.assertAlmostEqual(forecast.c_to_f(0), 32)
        self.assertAlmostEqual(forecast.mm_to_in(25.4), 1)
        self.assertIsNone(forecast.finite("nan"))
        self.assertIsNone(forecast.c_to_f(None))

    def test_daily_row_rounds_normalized_contract(self):
        row = forecast.daily_row("2026-08-16", 90.04, 70.06, 49.8, 0.1236, "Rain")
        self.assertEqual(row["high_f"], 90.0)
        self.assertEqual(row["low_f"], 70.1)
        self.assertEqual(row["rain_chance_pct"], 50)
        self.assertEqual(row["rain_in"], 0.124)

    def test_precipitation_intervals_split_at_local_midnight(self):
        totals = forecast.allocate_intervals([{
            "validTime": "2026-08-15T03:00:00+00:00/PT2H", "value": 2,
        }])
        self.assertEqual(totals, {"2026-08-14": 1.0, "2026-08-15": 1.0})

    def test_dew_point_windows_use_morning_and_afternoon_medians(self):
        result = forecast.dew_point_windows([
            ("2026-08-30T06:00:00-04:00", 72),
            ("2026-08-30T08:00:00-04:00", 70),
            ("2026-08-30T10:00:00-04:00", 68),
            ("2026-08-30T14:00:00-04:00", 61),
            ("2026-08-30T18:00:00-04:00", 57),
            ("2026-08-30T20:00:00-04:00", 99),
        ])
        self.assertEqual(result["2026-08-30"], {"dew_point_am_f": 70, "dew_point_pm_f": 59})


class ProviderNormalizationTests(unittest.TestCase):
    def test_openmeteo_daily_response(self):
        valid_date = datetime.now(forecast.LOCAL_ZONE).date().isoformat()
        response = {"daily": {
            "time": [valid_date], "temperature_2m_max": [91.2], "temperature_2m_min": [72.4],
            "precipitation_probability_max": [60], "precipitation_sum": [0.25],
            "weather_code": [61], "sunrise": [f"{valid_date}T06:30"], "sunset": [f"{valid_date}T20:00"],
        }, "hourly": {
            "time": [f"{valid_date}T08:00", f"{valid_date}T15:00"], "dew_point_2m": [71, 65],
        }}
        with patch.object(forecast, "get_json", return_value=response):
            result = forecast.fetch_openmeteo("openmeteo_nbm", "model")
        self.assertEqual(result["today_high"], 91.2)
        self.assertEqual(result["today_rain_chance"], 60)
        self.assertEqual(result["today_rain_in"], 0.25)
        self.assertEqual(result["today_summary"], "Light rain")
        self.assertEqual(result["today_dew_point_am_f"], 71)
        self.assertEqual(result["today_dew_point_pm_f"], 65)

    def test_openweather_converts_probability_and_rain_units(self):
        now = datetime.now(forecast.LOCAL_ZONE)
        response = {"current": {"dt": now.timestamp()}, "daily": [{
            "dt": now.timestamp(), "temp": {"max": 90, "min": 70}, "pop": 0.9,
            "rain": 25.4, "weather": [{"description": "heavy rain"}],
        }], "hourly": [
            {"dt": now.replace(hour=8, minute=0, second=0).timestamp(), "dew_point": 69},
            {"dt": now.replace(hour=15, minute=0, second=0).timestamp(), "dew_point": 61},
        ]}
        with patch.dict(os.environ, {"OPENWEATHER_API_KEY": "test"}), patch.object(forecast, "get_json", return_value=response):
            result = forecast.fetch_openweather()
        self.assertEqual(result["today_rain_chance"], 90)
        self.assertEqual(result["today_rain_in"], 1)
        self.assertEqual(result["today_dew_point_am_f"], 69)
        self.assertEqual(result["today_dew_point_pm_f"], 61)

    def test_nws_converts_grid_dew_point_windows(self):
        valid_date = datetime.now(forecast.LOCAL_ZONE).date().isoformat()
        point = {"properties": {"forecast": "forecast-url", "forecastGridData": "grid-url"}}
        periods = {"properties": {"updated": f"{valid_date}T05:00:00-04:00", "periods": [{
            "startTime": f"{valid_date}T06:00:00-04:00", "temperature": 80,
            "temperatureUnit": "F", "isDaytime": True, "shortForecast": "Clear",
            "probabilityOfPrecipitation": {"value": 0},
        }]}}
        grid = {"properties": {
            "quantitativePrecipitation": {"values": []},
            "dewpoint": {"values": [
                {"validTime": f"{valid_date}T06:00:00-04:00/PT5H", "value": 20},
                {"validTime": f"{valid_date}T14:00:00-04:00/PT5H", "value": 10},
            ]},
        }}
        with patch.object(forecast, "get_json", side_effect=[point, periods, grid]):
            result = forecast.fetch_nws()
        self.assertEqual(result["today_dew_point_am_f"], 68)
        self.assertEqual(result["today_dew_point_pm_f"], 50)

    def test_wunderground_adds_hourly_dew_point_windows(self):
        valid_date = datetime.now(forecast.LOCAL_ZONE).date().isoformat()
        daily = {
            "validTimeLocal": [f"{valid_date}T07:00:00-04:00"],
            "calendarDayTemperatureMax": [85], "calendarDayTemperatureMin": [65],
            "qpf": [0], "narrative": ["Clear"], "sunriseTimeLocal": [],
            "sunsetTimeLocal": [], "moonPhase": [], "daypart": [{"precipChance": [0, 0]}],
        }
        hourly = {
            "validTimeLocal": [f"{valid_date}T08:00:00-04:00", f"{valid_date}T15:00:00-04:00"],
            "temperatureDewPoint": [70, 62],
        }
        with patch.dict(os.environ, {"WUNDERGROUND_API_KEY": "test"}), patch.object(forecast, "get_json", side_effect=[daily, hourly]):
            result = forecast.fetch_wunderground()
        self.assertEqual(result["today_dew_point_am_f"], 70)
        self.assertEqual(result["today_dew_point_pm_f"], 62)

    def test_weatherkit_converts_celsius_and_millimeters(self):
        valid_date = datetime.now(forecast.LOCAL_ZONE).date().isoformat()
        response = {"forecastDaily": {"metadata": {"readTime": "2026-08-16T10:00:00Z"}, "days": [{
            "forecastStart": f"{valid_date}T04:00:00Z", "temperatureMax": 30,
            "temperatureMin": 20, "precipitationChance": 0.4,
            "precipitationAmount": 25.4, "conditionCode": "HeavyRain",
        }]}, "forecastHourly": {"hours": [
            {"forecastStart": f"{valid_date}T08:00:00-04:00", "temperatureDewPoint": 20},
            {"forecastStart": f"{valid_date}T15:00:00-04:00", "temperatureDewPoint": 15},
        ]}}
        with patch.object(forecast, "weatherkit_token", return_value="token"), patch.object(forecast, "get_json", return_value=response), patch.object(forecast, "weatherkit_attribution", return_value={}):
            result = forecast.fetch_weatherkit()
        self.assertEqual(result["today_high"], 86)
        self.assertEqual(result["today_low"], 68)
        self.assertEqual(result["today_rain_chance"], 40)
        self.assertEqual(result["today_rain_in"], 1)
        self.assertEqual(result["today_summary"], "Heavy rain")
        self.assertEqual(result["today_dew_point_am_f"], 68)
        self.assertEqual(result["today_dew_point_pm_f"], 59)


class ForecastArchiveTests(unittest.TestCase):
    def test_database_migrates_existing_forecast_table_for_dew_points(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "history.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("""CREATE TABLE forecast_daily (
                run_id INTEGER, valid_date TEXT, lead_days INTEGER, high_f REAL, low_f REAL,
                rain_chance_pct REAL, rain_in REAL, summary TEXT, PRIMARY KEY(run_id, valid_date)
            )""")
            connection.close()
            with patch.object(forecast, "DATA_DIR", Path(root)), patch.object(forecast, "DATABASE", path):
                connection = forecast.database()
                try:
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(forecast_daily)")}
                finally:
                    connection.close()
        self.assertIn("dew_point_am_f", columns)
        self.assertIn("dew_point_pm_f", columns)

    def test_archive_calculates_lead_days_in_local_time(self):
        with tempfile.TemporaryDirectory() as root, patch.object(forecast, "DATA_DIR", Path(root)), patch.object(forecast, "DATABASE", Path(root) / "history.sqlite3"):
            connection = forecast.database()
            try:
                payload = {
                    "provider": "nws", "issued_at": "2026-08-16T01:00:00+00:00",
                    "updated_at": "2026-08-16T01:00:00+00:00",
                    "days": [forecast.daily_row("2026-08-16", 90, 70, 20, 0, dew_point_am=68, dew_point_pm=59)],
                }
                forecast.archive(connection, payload)
                row = connection.execute("SELECT valid_date,lead_days,dew_point_am_f,dew_point_pm_f FROM forecast_daily").fetchone()
            finally:
                connection.close()
        self.assertEqual(row, ("2026-08-16", 1, 68, 59))


if __name__ == "__main__":
    unittest.main()
