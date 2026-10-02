"""작업 폴더를 읽지 않고 Git index/이력에서 비밀 파일과 자격증명을 차단한다."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL)


def forbidden_path(name: str) -> bool:
    """내용을 읽기 전에 .env 계열 경로를 거부한다. 예시 파일만 허용한다."""
    return any(
        (part == ".env" or part.startswith(".env.")) and part != ".env.example"
        for part in PurePosixPath(name).parts
    )


def check(mode: str) -> int:
    """검사 결과만 출력하며 scanner의 원문/진단 출력은 노출하지 않는다."""
    paths = git("ls-files", "-z").decode("utf-8", errors="replace").split("\0")
    if mode == "history":
        history = git("log", "--all", "--format=", "--name-only", "-z")
        paths += [
            name.lstrip("\n") for name in history.decode("utf-8", errors="replace").split("\0")
        ]
    blocked = sorted({name for name in paths if forbidden_path(name)})
    if blocked:
        print("차단: 비밀 설정 파일이 Git index 또는 이력에 포함되어 있습니다.")
        for name in blocked:
            print(json.dumps(name, ensure_ascii=False))
        return 1

    local_binary = ROOT / ".tools" / "gitleaks"
    binary = str(local_binary) if local_binary.is_file() else shutil.which("gitleaks")
    if not binary:
        print("차단: gitleaks를 설치하세요. docs/team-rules/secret-protection.md 참조.")
        return 2
    command = [
        binary,
        "git",
        "--no-banner",
        "--redact=100",
        "--ignore-gitleaks-allow",
        "--gitleaks-ignore-path",
        "/dev/null",
        "--config",
        str(ROOT / ".gitleaks.toml"),
        "--exit-code",
        "10",
    ]
    command += ["--staged"] if mode == "staged" else ["--log-opts=--all"]
    # 출력 전체를 버리므로 도구 오류에도 비밀 원문이 터미널/CI 로그로 나가지 않는다.
    result = subprocess.run(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode == 10:
        print("차단: 비밀정보 후보가 발견됐습니다. 값을 출력하지 말고 변경 파일을 검토하세요.")
        return 1
    if result.returncode:
        print("차단: 비밀정보 검사를 완료하지 못했습니다. 도구 설치/설정을 확인하세요.")
        return 2
    print("비밀정보 검사 통과 (Git " + mode + ")")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("staged", "history"))
    args = parser.parse_args()
    try:
        return check(args.mode)
    except (OSError, subprocess.SubprocessError):
        print("차단: 검사 실행 오류. 커밋하지 말고 실행 환경을 확인하세요.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
