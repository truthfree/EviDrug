#!/usr/bin/env python3
"""Check whether a checkout is ready to run EviDrug locally.

The command reports only whether configuration is present. It never prints secret
values and does not contact model or scientific-data APIs.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_FREE_GIB = 8


@dataclass(frozen=True)
class Check:
    level: str
    message: str


def command_available(name: str) -> bool:
    return shutil.which(name) is not None


def run_quiet(command: list[str]) -> bool:
    try:
        return (
            subprocess.run(
                command,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=20,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def configured_names(env_file: Path) -> set[str]:
    """Return non-empty variable names without retaining or displaying values."""
    names = {name for name, value in os.environ.items() if value.strip()}
    if not env_file.is_file():
        return names

    try:
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() and value.strip().strip("'\""):
                names.add(name.strip())
    except (OSError, UnicodeError):
        pass
    return names


def inspect(full: bool, env_file: Path) -> list[Check]:
    checks: list[Check] = []
    docker_present = command_available("docker")
    checks.append(Check("PASS" if docker_present else "FAIL", "Docker CLI installed"))

    daemon_ready = docker_present and run_quiet(["docker", "info"])
    checks.append(Check("PASS" if daemon_ready else "FAIL", "Docker daemon reachable"))

    compose_ready = docker_present and run_quiet(["docker", "compose", "version"])
    checks.append(Check("PASS" if compose_ready else "FAIL", "Docker Compose available"))
    if compose_ready:
        valid = run_quiet(["docker", "compose", "config", "--quiet"])
        checks.append(Check("PASS" if valid else "FAIL", "Base Compose configuration valid"))

    free_gib = shutil.disk_usage(ROOT).free / (1024**3)
    checks.append(
        Check(
            "PASS" if free_gib >= MIN_FREE_GIB else "WARN",
            f"Free disk space {free_gib:.1f} GiB (recommended: {MIN_FREE_GIB}+ GiB)",
        )
    )

    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        checks.append(Check("WARN", "ARM host detected; full model worker uses amd64 emulation"))
    else:
        checks.append(Check("PASS", f"Host architecture: {machine or 'unknown'}"))

    configured = configured_names(env_file)
    auth_names = {"EVIDRUG_ACCESS_CODE_HASH", "EVIDRUG_SESSION_SECRET"}
    missing_auth = sorted(auth_names - configured)
    checks.append(
        Check(
            "WARN" if missing_auth else "PASS",
            "Authentication configuration "
            + ("missing: " + ", ".join(missing_auth) if missing_auth else "present"),
        )
    )

    if full:
        api_key = "EVIDRUG_OPENAI_API_KEY"
        checks.append(
            Check(
                "FAIL" if api_key not in configured else "PASS",
                "LLM API configuration " + ("missing" if api_key not in configured else "present"),
            )
        )
        if compose_ready:
            valid = run_quiet(
                [
                    "docker",
                    "compose",
                    "-f",
                    "compose.yaml",
                    "-f",
                    "compose.poc.yaml",
                    "config",
                    "--quiet",
                ]
            )
            checks.append(
                Check("PASS" if valid else "FAIL", "Full PoC Compose configuration valid")
            )

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="also check full model-pipeline requirements",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ROOT / ".env",
        help="configuration file to inspect without printing values (default: .env)",
    )
    args = parser.parse_args()

    checks = inspect(args.full, args.env_file)
    for check in checks:
        print(f"[{check.level}] {check.message}")

    failures = sum(check.level == "FAIL" for check in checks)
    warnings = sum(check.level == "WARN" for check in checks)
    print(f"\nResult: {failures} failure(s), {warnings} warning(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
