"""검증된 DeepPurpose checkpoint zip을 안전하게 이미지 안에 추출한다."""

import argparse
import zipfile
from pathlib import Path, PurePosixPath


def safe_members(
    archive: zipfile.ZipFile, expected_directory: str
) -> list[zipfile.ZipInfo]:
    """경로 탈출과 예상하지 않은 파일을 거부한다."""
    files: set[str] = set()
    members = archive.infolist()
    for member in members:
        path = PurePosixPath(member.filename)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe archive member: {member.filename}")
        if not member.is_dir():
            files.add(member.filename)
    expected_files = {
        f"{expected_directory}/config.pkl",
        f"{expected_directory}/model.pt",
    }
    if files != expected_files:
        raise ValueError(f"Unexpected checkpoint archive contents: {sorted(files)}")
    return members


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("expected_directory")
    args = parser.parse_args()

    args.destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(args.archive) as archive:
        members = safe_members(archive, args.expected_directory)
        archive.extractall(args.destination, members=members)


if __name__ == "__main__":
    main()
