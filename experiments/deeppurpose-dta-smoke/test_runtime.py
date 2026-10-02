"""runtime protocol은 실제 모델 없이 검증한다."""

import io
import json
import unittest
from unittest.mock import patch

from runtime import DeepPurposeEngine, arguments_hash, serve

ARGUMENTS = {"canonical_smiles": "CCO", "target_sequence": "ACDEFGHIK"}


class FakeEngine:
    metadata = DeepPurposeEngine().metadata

    def predict(self, arguments):
        return 5.0, {
            "cold_start": False,
            "load_seconds": 0,
            "inference_seconds": 0.01,
            "peak_rss_mib": 10,
        }


class RuntimeTests(unittest.TestCase):
    def request(self, **changes):
        return {"protocol": 1, "request_id": "a" * 32, "arguments": ARGUMENTS} | changes

    def test_two_requests_produce_two_correlated_responses(self):
        request = (json.dumps(self.request()) + "\n").encode()
        output = io.StringIO()
        serve(io.BytesIO(request * 2), output, FakeEngine())
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["arguments_sha256"], arguments_hash(ARGUMENTS))
        self.assertEqual(replies[0]["observations"][0]["value"], 5.0)

    def test_invalid_request_is_rejected_before_inference(self):
        for request in [
            self.request(protocol=True),
            self.request(request_id="bad"),
            self.request(arguments={}),
            self.request(extra="bad"),
        ]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                serve(
                    io.BytesIO((json.dumps(request) + "\n").encode()),
                    io.StringIO(),
                    FakeEngine(),
                )

    def test_long_or_incomplete_line_is_rejected(self):
        for line in [b"x" * 65537 + b"\n", json.dumps(self.request()).encode()]:
            with self.assertRaises(ValueError):
                serve(io.BytesIO(line), io.StringIO(), FakeEngine())

    def test_corrupted_checkpoint_fails_before_loading_pickle(self):
        with (
            patch("runtime.version", return_value="0.1.5"),
            patch("runtime.sha256", return_value="0" * 64),
            self.assertRaisesRegex(RuntimeError, "checksum"),
        ):
            DeepPurposeEngine().predict(ARGUMENTS)
