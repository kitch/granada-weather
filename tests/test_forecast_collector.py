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


class ProviderNormalizationTests(unittest.TestCase):
    def test_openmeteo_daily_response(self):
        valid_date = datetime.now(forecast.LOCAL_ZONE).date().isoformat()
        response = {"daily": {
            "time": [valid_date], "temperature_2m_max": [91.2], "temperature_2m_min": [72.4],
            "precipitation_probability_max": [60], "precipitation_sum": [0.25],
            "weather_code": [61], "sunrise": [f"{valid_date}T06:30"], "sunset": [f"{valid_date}T20:00"],
        }}
        with patch.object(forecast, "get_json", return_value=response):
            result = forecast.fetch_openmeteo("openmeteo_nbm", "model")
        self.assertEqual(result["today_high"], 91.2)
        self.assertEqual(result["today_rain_chance"], 60)
        self.assertEqual(result["today_rain_in"], 0.25)
        self.assertEqual(result["today_summary"], "Light rain")

    def test_openweather_converts_probability_and_rain_units(self):
        now = datetime.now(forecast.LOCAL_ZONE)
        response = {"current": {"dt": now.timestamp()}, "daily": [{
            "dt": now.timestamp(), "temp": {"max": 90, "min": 70}, "pop": 0.9,
            "rain": 25.4, "weather": [{"description": "heavy rain"}],
        }]}
        with patch.dict(os.environ, {"OPENWEATHER_API_KEY": "test"}), patch.object(forecast, "get_json", return_value=response):
            result = forecast.fetch_openweather()
        self.assertEqual(result["today_rain_chance"], 90)
        self.assertEqual(result["today_rain_in"], 1)

    def test_weatherkit_converts_celsius_and_millimeters(self):
        valid_date = datetime.now(forecast.LOCAL_ZONE).date().isoformat()
        response = {"forecastDaily": {"metadata": {"readTime": "2026-08-16T10:00:00Z"}, "days": [{
            "forecastStart": f"{valid_date}T04:00:00Z", "temperatureMax": 30,
            "temperatureMin": 20, "precipitationChance": 0.4,
            "precipitationAmount": 25.4, "conditionCode": "HeavyRain",
        }]}}
        with patch.object(forecast, "weatherkit_token", return_value="token"), patch.object(forecast, "get_json", return_value=response), patch.object(forecast, "weatherkit_attribution", return_value={}):
            result = forecast.fetch_weatherkit()
        self.assertEqual(result["today_high"], 86)
        self.assertEqual(result["today_low"], 68)
        self.assertEqual(result["today_rain_chance"], 40)
        self.assertEqual(result["today_rain_in"], 1)
        self.assertEqual(result["today_summary"], "Heavy rain")


class ForecastArchiveTests(unittest.TestCase):
    def test_archive_calculates_lead_days_in_local_time(self):
        with tempfile.TemporaryDirectory() as root, patch.object(forecast, "DATA_DIR", Path(root)), patch.object(forecast, "DATABASE", Path(root) / "history.sqlite3"):
            connection = forecast.database()
            try:
                payload = {
                    "provider": "nws", "issued_at": "2026-08-16T01:00:00+00:00",
                    "updated_at": "2026-08-16T01:00:00+00:00",
                    "days": [forecast.daily_row("2026-08-16", 90, 70, 20, 0)],
                }
                forecast.archive(connection, payload)
                row = connection.execute("SELECT valid_date,lead_days FROM forecast_daily").fetchone()
            finally:
                connection.close()
        self.assertEqual(row, ("2026-08-16", 1))


if __name__ == "__main__":
    unittest.main()
