import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import server


class StationInputTests(unittest.TestCase):
    def test_credentials_accept_supported_field_names(self):
        with patch.dict(os.environ, {"WEATHER_STATION_ID": "station", "WEATHER_STATION_KEY": "secret"}, clear=False):
            self.assertTrue(server.station_credentials_valid({"ID": "station", "PASSWORD": "secret"}))
            self.assertTrue(server.station_credentials_valid({"station_id": "station", "station_key": "secret"}))
            self.assertFalse(server.station_credentials_valid({"ID": "station", "PASSWORD": "wrong"}))

    def test_credentials_fail_closed_when_server_configuration_is_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(server.station_credentials_valid({"ID": "station", "PASSWORD": "secret"}))

    def test_normalize_aliases_numeric_fields_and_removes_credentials(self):
        reading = server.normalize({
            "ID": "station", "PASSWORD": "secret", "dateutc": "2026-08-16 12:30:00",
            "tempf": "80.5", "humidity": "70", "baromrelin": "nan", "uv": "not-a-number",
            "custom field": "hello",
        })
        self.assertEqual(reading["observed_at"], "2026-08-16T12:30:00+00:00")
        self.assertEqual(reading["outdoor_temp_f"], 80.5)
        self.assertEqual(reading["outdoor_humidity_pct"], 70)
        self.assertEqual(reading["custom_field"], "hello")
        self.assertNotIn("id", reading)
        self.assertNotIn("password", reading)
        self.assertNotIn("pressure_relative_inhg", reading)
        self.assertNotIn("uv_index", reading)

    def test_ws2000_rainin_is_normalized_as_rate(self):
        reading = server.normalize({"rainin": "0.165", "dailyrainin": "0.028"})
        self.assertEqual(reading["rain_rate_in_hr"], 0.165)
        self.assertEqual(reading["rain_daily_in"], 0.028)
        self.assertNotIn("rain_hour_in", reading)

    def test_out_of_range_weather_values_are_discarded(self):
        reading = server.normalize({
            "tempf": "-9999", "humidity": "101", "tempinf": "72",
            "dewptf": "-121", "baromrelin": "19.9", "baromabsin": "35.1",
            "windspeedmph": "-1", "windgustmph": "201", "winddir": "361",
            "rainin": "-0.1", "dailyrainin": "101", "yearlyrainin": "1001",
            "solarradiation": "2001", "uv": "31",
        })
        self.assertEqual(reading["indoor_temp_f"], 72)
        for field in server.FIELD_RANGES:
            if field != "indoor_temp_f":
                self.assertNotIn(field, reading)

    def test_valid_boundary_values_are_retained(self):
        reading = server.normalize({
            "tempf": "-100", "humidity": "100", "baromrelin": "20",
            "yearlyrainin": "1000", "winddir": "360", "uv": "0",
        })
        self.assertEqual(reading["outdoor_temp_f"], -100)
        self.assertEqual(reading["outdoor_humidity_pct"], 100)
        self.assertEqual(reading["pressure_relative_inhg"], 20)
        self.assertEqual(reading["rain_yearly_in"], 1000)
        self.assertEqual(reading["wind_direction_deg"], 360)
        self.assertEqual(reading["uv_index"], 0)

    def test_unusable_packet_is_not_stored_or_forwarded(self):
        with patch.object(server, "store") as store, patch.object(server, "enqueue_wunderground") as forward:
            accepted = server.process_station_fields({
                "tempf": "-9999", "humidity": "-9999", "baromrelin": "-9999",
                "yearlyrainin": "-9999",
            })
        self.assertFalse(accepted)
        store.assert_not_called()
        forward.assert_not_called()

    def test_usable_packet_is_stored_and_forwarded(self):
        fields = {"tempf": "72", "humidity": "50", "baromrelin": "29.9", "yearlyrainin": "1.2"}
        with patch.object(server, "store") as store, patch.object(server, "enqueue_wunderground") as forward:
            accepted = server.process_station_fields(fields)
        self.assertTrue(accepted)
        store.assert_called_once()
        forward.assert_called_once_with(store.call_args.args[0])

    def test_public_reading_uses_allowlist(self):
        public = server.public_reading({
            "outdoor_temp_f": 75, "indoor_temp_f": 70, "battery_low": 1,
            "stationtype": "WS-2000", "passkey": "never-public",
        })
        self.assertEqual(public, {"outdoor_temp_f": 75, "indoor_temp_f": 70})


class RainStorageTests(unittest.TestCase):
    def test_rain_increment_handles_normal_increase_and_counter_reset(self):
        increment, total = server.rain_increment({"rain_yearly_in": 10.2}, 10.0)
        self.assertAlmostEqual(increment, 0.2)
        self.assertEqual(total, 10.2)
        self.assertEqual(server.rain_increment({"rain_yearly_in": 0.1}, 10.2), (0.1, 0.1))
        self.assertEqual(server.rain_increment({}, 10.2), (0.0, 10.2))

    def test_store_persists_increment_and_reset_without_negative_rain(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            with patch.object(server, "DATA_DIR", root), patch.object(server, "LATEST", root / "latest.json"):
                server.store({"observed_at": "2026-08-16T10:00:00+00:00", "rain_yearly_in": 10.0})
                server.store({"observed_at": "2026-08-16T10:01:00+00:00", "rain_yearly_in": 10.1})
                second = json.loads((root / "latest.json").read_text())
                server.store({"observed_at": "2026-08-16T10:02:00+00:00", "rain_yearly_in": 0.05})
                reset = json.loads((root / "latest.json").read_text())
            self.assertAlmostEqual(second["rain_increment_in"], 0.1)
            self.assertEqual(reset["rain_increment_in"], 0.05)

    def test_store_derives_last_hour_from_counter_increments(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            with patch.object(server, "DATA_DIR", root), patch.object(server, "LATEST", root / "latest.json"):
                server.store({
                    "observed_at": "2026-08-16T10:00:00+00:00", "rain_yearly_in": 1.0,
                    "rain_daily_in": 0,
                })
                server.store({
                    "observed_at": "2026-08-16T10:01:00+00:00", "rain_yearly_in": 1.008,
                    "rain_daily_in": 0.008, "rain_rate_in_hr": 0.047,
                })
                server.store({
                    "observed_at": "2026-08-16T10:03:00+00:00", "rain_yearly_in": 1.029,
                    "rain_daily_in": 0.028, "rain_rate_in_hr": 0.165,
                })
                latest = json.loads((root / "latest.json").read_text())
            self.assertEqual(latest["rain_rate_in_hr"], 0.165)
            self.assertAlmostEqual(latest["rain_hour_in"], 0.028)

    def test_rainfall_bars_sum_raw_increments(self):
        start = datetime(2026, 8, 16, 12, tzinfo=timezone.utc).timestamp()
        rows = [
            {"observed_at": "2026-08-16T11:55:00+00:00", "rain_yearly_in": 1.0},
            {"observed_at": "2026-08-16T12:05:00+00:00", "rain_yearly_in": 1.01},
            {"observed_at": "2026-08-16T12:30:00+00:00", "rain_yearly_in": 1.03},
            {"observed_at": "2026-08-16T13:05:00+00:00", "rain_yearly_in": 1.07},
        ]
        bars = server.rainfall_bars(rows, start, start + 2 * 3600, 168, True)
        nonzero = [bar["rainfall_in"] for bar in bars if bar["rainfall_in"]]
        self.assertEqual(nonzero, [0.03, 0.04])


class HistoryResolutionTests(unittest.TestCase):
    def test_heat_index_is_calculated_below_forty_percent_humidity_when_hot_enough(self):
        apparent = server.apparent_temperature_f({
            "outdoor_temp_f": 97.7,
            "outdoor_humidity_pct": 37,
            "wind_speed_mph": 1.34,
        })
        self.assertAlmostEqual(apparent, 102.4, places=1)

    def test_hourly_rollup_preserves_true_feels_like_extrema(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "2026-08-30.ndjson"
            path.write_text("\n".join(json.dumps(row) for row in [
                {
                    "observed_at": "2026-08-30T16:00:00+00:00", "outdoor_temp_f": 82,
                    "outdoor_humidity_pct": 70, "wind_speed_mph": 1,
                },
                {
                    "observed_at": "2026-08-30T16:30:00+00:00", "outdoor_temp_f": 90,
                    "outdoor_humidity_pct": 75, "wind_speed_mph": 2,
                },
            ]) + "\n")
            rows = server.build_hourly_rollup(path)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["_range"]["feels_like_f"][1], 100)
        self.assertLess(rows[0]["_range"]["feels_like_f"][0], rows[0]["_range"]["feels_like_f"][1])

    def test_seven_day_history_uses_detailed_observations(self):
        end = datetime(2026, 8, 30, 12, tzinfo=timezone.utc).timestamp()
        with patch.object(server, "read_raw_range", return_value=[]) as raw, \
                patch.object(server, "read_hourly_day") as hourly, \
                patch.object(server, "rainfall_bars", return_value=[]):
            server.read_history(24 * 7, end=end)
        raw.assert_called_once()
        hourly.assert_not_called()

    def test_thirty_day_history_uses_range_preserving_hourly_rollups(self):
        end = datetime(2026, 8, 30, 12, tzinfo=timezone.utc).timestamp()
        with patch.object(server, "read_raw_range") as raw, \
                patch.object(server, "read_hourly_day", return_value=[]) as hourly, \
                patch.object(server, "rainfall_bars", return_value=[]), \
                patch.object(server, "read_latest", return_value={}):
            server.read_history(24 * 30, end=end)
        raw.assert_not_called()
        self.assertGreater(hourly.call_count, 0)


class PublicHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.latest_patch = patch.object(server, "LATEST", root / "latest.json")
        self.latest_patch.start()
        server.LATEST.write_text(json.dumps({"outdoor_temp_f": 75, "battery_low": 1}))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.PublicHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.latest_patch.stop()
        self.temp.cleanup()

    def test_public_current_is_filtered_and_has_security_headers(self):
        with urllib.request.urlopen(self.base + "/api/current", timeout=3) as response:
            payload = json.load(response)
            self.assertEqual(payload, {"outdoor_temp_f": 75})
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_public_write_methods_are_rejected_uniformly(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            request = urllib.request.Request(self.base + "/data/report", data=b"x=1", method=method)
            with self.subTest(method=method), self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 405)
            self.assertEqual(raised.exception.headers["Allow"], "GET")
            raised.exception.close()


if __name__ == "__main__":
    unittest.main()
