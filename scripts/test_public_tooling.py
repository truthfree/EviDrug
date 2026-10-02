from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exporter = load_script("export_public_repo")


class PublicExportPolicyTests(unittest.TestCase):
    def test_keeps_application_source_and_public_docs(self) -> None:
        self.assertTrue(exporter.is_public("backend/src/evidrug_api/main.py"))
        self.assertTrue(exporter.is_public("frontend/src/App.tsx"))
        self.assertTrue(exporter.is_public("docs/development.md"))
        self.assertTrue(exporter.is_public("experiments/admet-smoke/runtime.py"))

    def test_excludes_internal_and_binary_material(self) -> None:
        excluded = (
            "docs/meetings/2026-10-01.md",
            "docs/evaluation/results.json",
            "docs/team-rules/README.md",
            "docs/plan/Evidrug_plan.pdf",
            "render.yaml",
            ".github/workflows/publish-poc-worker.yml",
            "private-notes/new-team-file.md",
            "experiments/new-internal-runtime/run.py",
        )
        for path in excluded:
            with self.subTest(path=path):
                self.assertFalse(exporter.is_public(path))

    def test_refuses_nonempty_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            (destination / "keep.txt").write_text("do not replace", encoding="utf-8")
            with self.assertRaises(ValueError):
                exporter.validate_destination(destination)


if __name__ == "__main__":
    unittest.main()
