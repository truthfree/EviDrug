from getpass import getpass

from pwdlib import PasswordHash

MINIMUM_ACCESS_CODE_LENGTH = 12


def hash_access_code(access_code: str) -> str:
    """평문 접근 코드를 Argon2 해시로 변환한다."""

    if len(access_code) < MINIMUM_ACCESS_CODE_LENGTH:
        raise ValueError(
            f"Access code must contain at least {MINIMUM_ACCESS_CODE_LENGTH} characters."
        )
    return PasswordHash.recommended().hash(access_code)


def main() -> None:
    """셸 기록에 평문 코드를 남기지 않고 배포용 해시를 생성한다."""

    access_code = getpass("Access code: ")
    confirmation = getpass("Confirm access code: ")
    if access_code != confirmation:
        raise SystemExit("Access codes do not match.")

    try:
        access_code_hash = hash_access_code(access_code)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    print("\nAdd the following value to a protected environment variable:")
    print(f"EVIDRUG_ACCESS_CODE_HASH='{access_code_hash}'")


if __name__ == "__main__":
    main()
