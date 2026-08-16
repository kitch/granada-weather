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
