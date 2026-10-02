"""실제 모델 없이 검증기의 실패 판정을 확인한다. 모델 추론 검증을 대체하지 않는다."""

import unittest

from smoke import validate_predictions


class PredictionValidationTests(unittest.TestCase):
    def test_preserves_all_predictions_including_percentiles(self):
        raw = {"hERG": 0.8, "hERG_drugbank_approved_percentile": 95, "extra": -2.1}
        self.assertEqual(validate_predictions(raw, {"hERG"}), raw)

    def test_rejects_missing_model_endpoint(self):
        with self.assertRaises(ValueError):
            validate_predictions({"hERG": 0.8}, {"hERG", "AMES"})

    def test_rejects_empty_or_unexpected_response(self):
        for raw in ({}, [], None, {"hERG": "0.8"}, {"hERG": True}, {1: 0.8}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_predictions(raw, {"hERG"})

    def test_rejects_non_finite_values(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_predictions({"hERG": value}, {"hERG"})

    def test_rejects_empty_expected_endpoints(self):
        with self.assertRaises(ValueError):
            validate_predictions({"hERG": 0.8}, set())


if __name__ == "__main__":
    unittest.main()
