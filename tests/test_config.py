import os
import unittest
import unittest.mock

from trackfixer.config import tuning_number


class TuningNumberTest(unittest.TestCase):
    def test_uses_the_default_when_not_set(self):
        with unittest.mock.patch.dict(os.environ, {"SOME_TUNING_VALUE": " "}):
            self.assertEqual(tuning_number("SOME_TUNING_VALUE", 25), 25)

    def test_reads_whole_and_decimal_numbers(self):
        with unittest.mock.patch.dict(os.environ, {"SOME_TUNING_VALUE": "3"}):
            self.assertEqual(tuning_number("SOME_TUNING_VALUE", 25), 3)
            self.assertIsInstance(tuning_number("SOME_TUNING_VALUE", 25), int)
        with unittest.mock.patch.dict(os.environ, {"SOME_TUNING_VALUE": "1.5"}):
            self.assertEqual(tuning_number("SOME_TUNING_VALUE", 25), 1.5)

    def test_refuses_what_is_not_a_number_of_0_or_more(self):
        for value in ("lots", "-1", "nan", "inf"):
            with self.subTest(value=value), unittest.mock.patch.dict(os.environ, {"SOME_TUNING_VALUE": value}):
                with self.assertRaises(SystemExit):
                    tuning_number("SOME_TUNING_VALUE", 25)
