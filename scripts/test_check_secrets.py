"""실제 비밀 파일 없이 임시 Git 저장소와 합성 키로 유출 방지 동작을 검증한다."""

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_secrets as guard


class SecretGuardTests(unittest.TestCase):
    def setUp(self):
        # 일부 샌드박스는 생성한 .git 디렉터리의 삭제도 제한한다.
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Secret guard test")
        self.git("config", "core.hooksPath", "/dev/null")
        local = guard.ROOT / ".tools" / "gitleaks"
        self.binary = str(local) if local.is_file() else shutil.which("gitleaks")
        if not self.binary:
            self.fail("Install gitleaks before running guard tests")
        shutil.copyfile(guard.ROOT / ".gitleaks.toml", self.root / ".gitleaks.toml")
        self.git("add", ".gitleaks.toml")
        self.git("commit", "-qm", "initial config")
        self.fake_key = "sk-" + "proj-" + "aB7cD8eF9gH0jK1mN2pQ3rS4tU5vW6xY"

    def git(self, *args, input=None):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, input=input, stderr=subprocess.DEVNULL
        )

    def stage(self, name, text):
        (self.root / name).write_text(text)
        self.git("add", name)

    def check(self, mode="staged", binary=True):
        output = io.StringIO()
        with (
            patch.object(guard, "ROOT", self.root),
            patch.object(guard.shutil, "which", return_value=self.binary if binary else None),
            contextlib.redirect_stdout(output),
        ):
            result = guard.check(mode)
        self.assertNotIn(self.fake_key, output.getvalue())
        return result

    def test_safe_example_passes(self):
        self.stage(".env.example", "OPENAI_API_KEY=replace-me\n")
        self.assertEqual(self.check(), 0)

    def test_key_in_source_is_blocked_even_if_worktree_is_cleaned(self):
        self.stage("settings.txt", self.fake_key)
        (self.root / "settings.txt").write_text("redacted in worktree only")
        self.assertEqual(self.check(), 1)

    def test_example_files_do_not_bypass_content_scan(self):
        self.stage(".env.example", "OPENAI_API_KEY=" + self.fake_key)
        self.assertEqual(self.check(), 1)

    def test_forced_env_index_entry_is_blocked_without_reading_a_file(self):
        blob = self.git("hash-object", "-w", "--stdin", input=b"dummy").decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "100644", blob, ".env.production")
        self.assertEqual(self.check(), 1)

    def test_history_finds_key_deleted_from_latest_commit(self):
        self.stage("settings.txt", self.fake_key)
        self.git("commit", "-qm", "synthetic credential")
        self.git("rm", "settings.txt")
        self.git("commit", "-qm", "delete synthetic credential")
        self.assertEqual(self.check("history"), 1)

    def test_missing_scanner_blocks_commit(self):
        self.assertEqual(self.check(binary=False), 2)

    def test_installed_hook_rejects_actual_commit(self):
        for name in ("scripts", ".githooks", ".tools"):
            (self.root / name).mkdir()
        shutil.copyfile(
            guard.ROOT / "scripts/check_secrets.py", self.root / "scripts/check_secrets.py"
        )
        shutil.copy2(guard.ROOT / ".githooks/pre-commit", self.root / ".githooks/pre-commit")
        (self.root / ".tools/gitleaks").symlink_to(self.binary)
        self.git("config", "core.hooksPath", ".githooks")
        self.stage("settings.txt", self.fake_key)
        result = subprocess.run(
            ["git", "commit", "-m", "must be rejected"], cwd=self.root, capture_output=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("비밀정보 후보".encode(), result.stdout + result.stderr)
        self.assertNotIn(self.fake_key.encode(), result.stdout + result.stderr)

    def test_scanner_execution_error_blocks_commit(self):
        original_run = subprocess.run

        def fail_scanner(command, **kwargs):
            if command[0] == self.binary:
                return subprocess.CompletedProcess(command, 2)
            return original_run(command, **kwargs)

        with patch.object(guard.subprocess, "run", side_effect=fail_scanner):
            self.assertEqual(self.check(), 2)

    def test_env_paths_are_blocked_but_exact_examples_are_allowed(self):
        for name in (".env", "backend/.env", ".env.local", "nested/.env.production"):
            self.assertTrue(guard.forbidden_path(name))
        for name in (".env.example", "backend/.env.example", "config.py"):
            self.assertFalse(guard.forbidden_path(name))


if __name__ == "__main__":
    unittest.main()
