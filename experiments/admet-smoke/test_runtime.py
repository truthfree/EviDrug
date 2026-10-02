"""모델 없는 ADMET runtime protocol 검증."""

import hashlib
import io
import json
import unittest
from unittest.mock import patch

from runtime import AdmetEngine, serve


class FakeEngine:
    def predict(self, smiles):
        print("library progress")
        return {"smiles": smiles}, {
            "cold_start": False,
            "load_seconds": 0,
            "inference_seconds": 0.01,
            "peak_rss_mib": 10,
        }


class RuntimeTests(unittest.TestCase):
    def test_multiple_replies_preserve_correlation_and_separate_library_output(self):
        request = {"protocol": 1, "request_id": "a" * 32, "smiles": "CCO"}
        output = io.StringIO()
        with patch("sys.stderr", new_callable=io.StringIO):
            serve(io.BytesIO(((json.dumps(request) + "\n") * 2).encode()), output, FakeEngine())
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["input_sha256"], hashlib.sha256(b"CCO").hexdigest())

    def test_rejects_incomplete_large_or_malformed_requests(self):
        requests = [b"x" * 32769 + b"\n", b"{}", b"{}\n"]
        for change in ({"protocol": True}, {"request_id": "bad"}, {"smiles": " "}, {"extra": 1}):
            request = {"protocol": 1, "request_id": "a" * 32, "smiles": "CCO"} | change
            requests.append((json.dumps(request) + "\n").encode())
        for raw in requests:
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError):
                serve(io.BytesIO(raw), io.StringIO(), FakeEngine())

    def test_rejects_unexpected_package_before_model_import(self):
        with patch("runtime.version", return_value="0"), self.assertRaises(RuntimeError):
            AdmetEngine().predict("CCO")
