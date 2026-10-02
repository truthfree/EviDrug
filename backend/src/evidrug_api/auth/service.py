from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe
from typing import Final, Literal, cast

from itsdangerous import BadData, URLSafeSerializer
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from evidrug_api.config import Settings

SESSION_FORMAT_VERSION: Final = 2
SESSION_SCOPE: Final = "analysis"
SESSION_SIGNING_SALT: Final = "evidrug-session-v1"
VISITOR_FORMAT_VERSION: Final = 1
VISITOR_SIGNING_SALT: Final = "evidrug-visitor-v1"
MINIMUM_SESSION_SECRET_LENGTH: Final = 32


class AuthConfigurationError(RuntimeError):
    """인증 비밀정보가 없거나 올바르지 않을 때 발생한다."""


class InvalidSessionToken(ValueError):
    """세션 토큰이 손상되었거나 만료되었을 때 발생한다."""


class InvalidVisitorToken(ValueError):
    """브라우저 기록 쿠키가 손상되었거나 만료되었을 때 발생한다."""


class _SessionPayload(BaseModel):
    """서명된 쿠키 안에 저장하는 최소 세션 데이터."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[2]
    scope: Literal["analysis"]
    session_nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    expires_at: int


class _VisitorPayload(BaseModel):
    """단기 인증 세션과 별도로 같은 브라우저의 분석 소유권을 식별한다."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    visitor_id: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    expires_at: int


@dataclass(frozen=True)
class AuthenticatedSession:
    """서명과 만료 검사를 통과한 세션."""

    expires_at: datetime


class AuthService:
    """접근 코드를 검증하고 만료 가능한 서명 세션을 발급한다."""

    def __init__(
        self,
        access_code_hash: str,
        session_secret: str,
        session_ttl_seconds: int,
        now: Callable[[], datetime] | None = None,
        visitor_ttl_seconds: int = 90 * 24 * 60 * 60,
    ) -> None:
        if not access_code_hash:
            raise AuthConfigurationError("EVIDRUG_ACCESS_CODE_HASH is not configured")
        if len(session_secret) < MINIMUM_SESSION_SECRET_LENGTH:
            raise AuthConfigurationError(
                "EVIDRUG_SESSION_SECRET must contain at least "
                f"{MINIMUM_SESSION_SECRET_LENGTH} characters"
            )

        self._access_code_hash = access_code_hash
        self._session_ttl_seconds = session_ttl_seconds
        self._visitor_ttl_seconds = visitor_ttl_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._password_hash = PasswordHash.recommended()
        self._serializer = URLSafeSerializer(
            secret_key=session_secret,
            salt=SESSION_SIGNING_SALT,
            signer_kwargs={"digest_method": sha256},
        )
        self._visitor_serializer = URLSafeSerializer(
            secret_key=session_secret,
            salt=VISITOR_SIGNING_SALT,
            signer_kwargs={"digest_method": sha256},
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "AuthService":
        """환경변수에서 읽은 비밀정보로 인증 서비스를 구성한다."""

        access_code_hash = _read_required_secret(settings.access_code_hash)
        session_secret = _read_required_secret(settings.session_secret)
        return cls(
            access_code_hash=access_code_hash,
            session_secret=session_secret,
            session_ttl_seconds=settings.session_ttl_seconds,
            visitor_ttl_seconds=settings.visitor_ttl_seconds,
        )

    def verify_access_code(self, access_code: str) -> bool:
        """평문 접근 코드가 설정된 Argon2 해시와 일치하는지 확인한다."""

        try:
            return self._password_hash.verify(access_code, self._access_code_hash)
        except PwdlibError as error:
            raise AuthConfigurationError("EVIDRUG_ACCESS_CODE_HASH is invalid") from error

    def create_session(self) -> tuple[str, AuthenticatedSession]:
        """현재 시각부터 설정된 기간 동안 유효한 서명 토큰을 만든다."""

        expires_at = (self._current_time() + timedelta(seconds=self._session_ttl_seconds)).replace(
            microsecond=0
        )
        payload = _SessionPayload(
            version=SESSION_FORMAT_VERSION,
            scope=SESSION_SCOPE,
            session_nonce=token_urlsafe(32),
            expires_at=int(expires_at.timestamp()),
        )
        token = self._serializer.dumps(payload.model_dump())
        return token, AuthenticatedSession(expires_at=expires_at)

    def read_session(self, token: str) -> AuthenticatedSession:
        """토큰의 서명, 데이터 구조와 만료 시각을 검증한다."""

        try:
            raw_payload = cast(object, self._serializer.loads(token))
            payload = _SessionPayload.model_validate(raw_payload)
        except (BadData, ValidationError, TypeError) as error:
            raise InvalidSessionToken("Session token is invalid") from error

        expires_at = datetime.fromtimestamp(payload.expires_at, tz=UTC)
        if expires_at <= self._current_time():
            raise InvalidSessionToken("Session token has expired")

        return AuthenticatedSession(expires_at=expires_at)

    def create_visitor_token(self, visitor_id: str | None = None) -> tuple[str, str]:
        """새 브라우저 ID를 발급하거나 기존 ID의 유효기간을 갱신한다."""

        stable_id = visitor_id if visitor_id is not None else token_urlsafe(32)
        expires_at = self._current_time() + timedelta(seconds=self._visitor_ttl_seconds)
        payload = _VisitorPayload(
            version=VISITOR_FORMAT_VERSION,
            visitor_id=stable_id,
            expires_at=int(expires_at.timestamp()),
        )
        return self._visitor_serializer.dumps(payload.model_dump()), stable_id

    def read_visitor_token(self, token: str) -> str:
        """서명과 만료가 유효한 브라우저 ID만 반환한다."""

        try:
            raw_payload = cast(object, self._visitor_serializer.loads(token))
            payload = _VisitorPayload.model_validate(raw_payload)
        except (BadData, ValidationError, TypeError) as error:
            raise InvalidVisitorToken("Visitor token is invalid") from error

        if datetime.fromtimestamp(payload.expires_at, tz=UTC) <= self._current_time():
            raise InvalidVisitorToken("Visitor token has expired")
        return payload.visitor_id

    def _current_time(self) -> datetime:
        current_time = self._now()
        if current_time.tzinfo is None:
            raise AuthConfigurationError("AuthService clock must return a timezone-aware datetime")
        return current_time.astimezone(UTC)


def _read_required_secret(secret: SecretStr | None) -> str:
    if secret is None:
        raise AuthConfigurationError("Authentication secrets are not configured")

    secret_value = secret.get_secret_value()
    if not secret_value:
        raise AuthConfigurationError("Authentication secrets are not configured")
    return secret_value
