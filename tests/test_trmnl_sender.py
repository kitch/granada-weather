import unittest

from trmnl_sender import safe


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


if __name__ == "__main__":
    unittest.main()
