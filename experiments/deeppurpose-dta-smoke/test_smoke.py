"""실제 모델 없이 smoke 검증기의 엄격한 실패 판정을 확인한다."""

import io
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from prepare_model import safe_members
from smoke import sha256, validate_prediction


class PredictionValidationTests(unittest.TestCase):
    def test_accepts_one_finite_number(self):
        self.assertEqual(validate_prediction([6.25]), 6.25)
        self.assertEqual(validate_prediction((3,)), 3.0)

    def test_rejects_wrong_cardinality_or_container(self):
        for raw in (None, 6.2, [], [1.0, 2.0], {"value": 1.0}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_prediction(raw)

    def test_hashes_files_without_python_311_file_digest(self):
        with patch.object(Path, "open", return_value=io.BytesIO(b"deeppurpose")):
            self.assertEqual(
                sha256(Path("artifact.bin")),
                "b256d36755d36bf4c5caab95e7b08b0a34e3cd20cabecae65e064587652dc122",
            )

    def test_rejects_bool_non_numeric_and_non_finite(self):
        for raw in ([True], ["6.2"], [float("nan")], [float("inf")]):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_prediction(raw)


class ArchiveValidationTests(unittest.TestCase):
    @staticmethod
    def archive_with(names: list[str]) -> zipfile.ZipFile:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name in names:
                archive.writestr(name, b"test")
        buffer.seek(0)
        return zipfile.ZipFile(buffer)

    def test_accepts_only_expected_checkpoint_files(self):
        archive = self.archive_with(
            [
                "model_cnn_cnn_bindingdb/config.pkl",
                "model_cnn_cnn_bindingdb/model.pt",
            ]
        )
        self.assertEqual(len(safe_members(archive, "model_cnn_cnn_bindingdb")), 2)

    def test_accepts_mpnn_checkpoint_directory(self):
        archive = self.archive_with(
            ["model_MPNN_CNN/config.pkl", "model_MPNN_CNN/model.pt"]
        )
        self.assertEqual(len(safe_members(archive, "model_MPNN_CNN")), 2)

    def test_rejects_traversal_and_unexpected_files(self):
        for names in (
            ["../model.pt"],
            [
                "model_cnn_cnn_bindingdb/config.pkl",
                "model_cnn_cnn_bindingdb/model.pt",
                "model_cnn_cnn_bindingdb/extra.txt",
            ],
        ):
            with self.subTest(names=names), self.assertRaises(ValueError):
                safe_members(self.archive_with(names), "model_cnn_cnn_bindingdb")


if __name__ == "__main__":
    unittest.main()
