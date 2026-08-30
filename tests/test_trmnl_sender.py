import unittest

from trmnl_sender import dew_point_summary, merge_variables, safe


class SafeFormattingTests(unittest.TestCase):
    def test_whole_number_keeps_significant_trailing_zero(self):
        self.assertEqual(safe(90, "°", 0), "90°")
        self.assertEqual(safe(100, "%", 0), "100%")

    def test_fractional_format_only_strips_decimal_zeroes(self):
        self.assertEqual(safe(70.0, "°", 1), "70°")
        self.assertEqual(safe(70.5, "°", 1), "70.5°")

    def test_zero_and_missing_values(self):
        self.assertEqual(safe(0, " mph", 1), "0 mph")
        self.assertEqual(safe(None, "°", 1), "—")


class MergeVariableTests(unittest.TestCase):
    def test_dew_point_summary_handles_complete_and_partial_forecasts(self):
        self.assertEqual(dew_point_summary({"today_dew_point_am_f": 70, "today_dew_point_pm_f": 59.5}, "today"), "AM 70° → PM 59.5°")
        self.assertEqual(dew_point_summary({"today_dew_point_pm_f": 61}, "today"), "PM 61°")
        self.assertEqual(dew_point_summary({}, "today"), "—")

    def test_payload_contract_formats_weather_and_forecast_values(self):
        weather = {
            "outdoor_temp_f": 80, "outdoor_humidity_pct": 90,
            "dew_point_f": 76.5,
            "indoor_temp_f": 70, "indoor_humidity_pct": 50,
            "pressure_relative_inhg": 30, "rain_hour_in": 0.01,
            "rain_daily_in": 0.2, "wind_speed_mph": 5, "wind_gust_mph": 10,
        }
        outlook = {
            "today_high": 90, "today_low": 70, "today_rain_in": 0.1,
            "today_rain_chance": 100, "today_sunrise": "6:30 AM", "today_sunset": "8:00 PM",
            "today_dew_point_am_f": 72, "today_dew_point_pm_f": 65,
            "tomorrow_high": 100, "tomorrow_low": 80, "tomorrow_rain_in": 0,
            "tomorrow_rain_chance": 0, "tomorrow_dew_point_am_f": 70,
            "tomorrow_dew_point_pm_f": 58.5,
        }
        payload = merge_variables(weather, outlook)
        self.assertEqual(payload["outdoor_humidity"], "90%")
        self.assertEqual(payload["outdoor_dew_point"], "76.5°")
        self.assertEqual(payload["today_high"], "90°")
        self.assertEqual(payload["tomorrow_high"], "100°")
        self.assertEqual(payload["today_rain_chance"], "100%")
        self.assertEqual(payload["pressure"], "1015.9 mbar")
        self.assertEqual(payload["today_sunrise"], "6:30 AM")
        self.assertEqual(payload["today_dew_point"], "AM 72° → PM 65°")
        self.assertEqual(payload["tomorrow_dew_point_pm"], "58.5°")

    def test_missing_values_use_display_placeholder(self):
        payload = merge_variables({}, {})
        self.assertEqual(payload["outdoor_temp"], "—")
        self.assertEqual(payload["pressure"], "—")
        self.assertEqual(payload["today_high"], "—")


if __name__ == "__main__":
    unittest.main()
