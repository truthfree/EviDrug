#!/usr/bin/env python3
"""Create a reviewable public-source snapshot from tracked repository files."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

ALLOWED_PATHS = {
    ".dockerignore",
    ".editorconfig",
    ".env.example",
    ".github/workflows/secret-scan.yml",
    ".gitignore",
    ".gitleaks.toml",
    "CONTRIBUTING.md",
    "Dockerfile.poc",
    "LICENSE",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "compose.poc.yaml",
    "compose.yaml",
}
ALLOWED_PREFIXES = (
    ".githooks/",
    "backend/",
    "docs/",
    "frontend/",
    "scripts/",
)
ALLOWED_EXPERIMENT_PREFIXES = (
    "experiments/admet-smoke/",
    "experiments/ctoxpred2-smoke/",
    "experiments/deeppurpose-dta-smoke/",
)
EXCLUDED_PREFIXES = (
    ".git/",
    "docs/deployment/",
    "docs/evaluation/",
    "docs/meetings/",
    "docs/plan/",
    "docs/proposal/",
    "docs/team-rules/",
    "experiments/breast-cancer-runtime/",
    "experiments/dacon-agent-runtime-compatibility/",
    "experiments/evaluation-runtime/",
)
EXCLUDED_PATHS = {
    ".github/workflows/publish-poc-worker.yml",
    "backend/AGENTS.md",
    "docs/competition-overview.md",
    "docs/plan.md",
    "docs/team-guide.md",
    "render.yaml",
}
EXCLUDED_SUFFIXES = {".pdf", ".xlsx", ".xls"}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def is_public(path: str) -> bool:
    pure = PurePosixPath(path)
    allowed = (
        path in ALLOWED_PATHS
        or path.startswith(ALLOWED_PREFIXES)
        or path.startswith(ALLOWED_EXPERIMENT_PREFIXES)
    )
    return (
        allowed
        and path not in EXCLUDED_PATHS
        and not path.startswith(EXCLUDED_PREFIXES)
        and pure.suffix.lower() not in EXCLUDED_SUFFIXES
    )


def validate_destination(destination: Path) -> None:
    if destination.resolve() == ROOT.resolve():
        raise ValueError("destination must not be the source repository")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("destination must be empty; export never overwrites an existing checkout")


def export(destination: Path) -> int:
    validate_destination(destination)
    destination.mkdir(parents=True, exist_ok=True)
    selected = [path for path in tracked_files() if is_public(path)]
    for relative in selected:
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return len(selected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="new or empty snapshot directory")
    parser.add_argument(
        "--list",
        action="store_true",
        help="print selected tracked paths without writing the destination",
    )
    args = parser.parse_args()

    if args.list:
        for path in tracked_files():
            if is_public(path):
                print(path)
        return 0

    try:
        count = export(args.destination)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"export failed: {error}", file=sys.stderr)
        return 1
    print(f"Exported {count} tracked files to {args.destination}")
    print(
        "Review the snapshot and run the secret scan before committing it to the public repository."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
