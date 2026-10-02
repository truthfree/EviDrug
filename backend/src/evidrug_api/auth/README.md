# 공용 접근 코드 인증

## 목적과 책임

공개 랜딩 이후의 분석 기능을 하나의 공용 접근 코드로 보호한다. 이 모듈은 접근 코드
검증, 서명된 세션·브라우저 기록 쿠키, 요청 Origin 검사와 Redis 기반 인증 시도 제한을 담당한다.
사용자 계정, 역할별 권한 및 OAuth는 담당하지 않는다.

## 폴더 구조

```text
auth/
├── dependencies.py       FastAPI에서 재사용할 Origin 및 세션 검사
├── hash_access_code.py   배포용 Argon2 접근 코드 해시 생성 명령
├── models.py             인증 API 요청·응답 모델
├── rate_limit.py         Redis 기반 인증 시도 제한
├── router.py             세션 생성·조회·삭제 endpoint
└── service.py            접근 코드 검증과 세션 서명·만료 검사
```

## 핵심 진입점

| 이름 | 위치 | 역할 | 입력·출력 | 주요 오류 |
| :--- | :--- | :--- | :--- | :--- |
| `create_session` | `router.py` | 접근 코드를 확인하고 쿠키를 발급한다. | 접근 코드 → 세션 상태 | 401, 403, 429, 503 |
| `read_session` | `router.py` | 현재 세션을 확인한다. | 세션 쿠키 → 만료 시각 | 401, 503 |
| `delete_session` | `router.py` | 브라우저의 세션 쿠키를 제거한다. | Origin → 204 | 403 |
| `require_authenticated_session` | `dependencies.py` | 보호할 API에서 세션을 검사한다. | 요청 쿠키 → 인증 세션 | 401, 503 |
| `require_allowed_origin` | `dependencies.py` | 쿠키를 변경하는 요청의 Origin을 검사한다. | Origin → 없음 | 403 |
| `AuthService` | `service.py` | Argon2 검증과 세션 토큰 처리를 수행한다. | 코드 또는 토큰 → 검증 결과 | 설정 오류, 잘못된 토큰 |
| `RedisAuthAttemptLimiter` | `rate_limit.py` | 클라이언트별 인증 시도 횟수를 제한한다. | 클라이언트 식별자 → 허용 판단 | Redis 접근 오류 |

## API 계약

모든 경로에는 `/api/v1` prefix가 붙는다.

| Method | 경로 | 요청 | 성공 응답 |
| :--- | :--- | :--- | :--- |
| `POST` | `/auth/session` | `{"access_code": "..."}` | `200`, 인증 상태와 만료 시각 |
| `GET` | `/auth/session` | 세션 쿠키 | `200`, 인증 상태와 만료 시각 |
| `DELETE` | `/auth/session` | 허용된 Origin | `204` |

오류 응답은 `{"detail": {"code": "...", "message": "..."}}` 형태를 사용한다. 잘못된
접근 코드는 코드의 존재 여부나 해시 검증 세부사항을 노출하지 않는다.

## 데이터 흐름

1. 브라우저가 접근 코드와 Origin을 `POST /auth/session`에 보낸다.
2. API가 Redis에서 클라이언트 IP 기반 시도 제한을 확인한다.
3. `AuthService`가 평문 코드를 설정된 Argon2 해시와 비교한다.
4. 성공하면 실패 횟수를 지우고 무작위 nonce와 만료 시각이 포함된 세션 토큰을 만든다.
5. API가 단기 인증 쿠키와 별도의 무작위 브라우저 ID 쿠키를 `HttpOnly`,
   `SameSite=Lax`, `Path=/`로 전달한다. 유효한 브라우저 ID는 재로그인 시 갱신한다.
6. 보호된 API는 `require_authenticated_session`으로 서명과 만료를 검사한다.

로그아웃은 인증 쿠키만 제거한다. 브라우저 ID는 기본 90일간 유지되지만 그 자체만으로
보호 API에 접근할 수는 없다. 새 형식 도입으로 구형 인증 쿠키는 재로그인이 필요하다.

`staging`과 `production`에서는 쿠키에 `Secure`가 추가된다. 로컬 HTTP 개발과 테스트는
브라우저가 쿠키를 전송할 수 있도록 `Secure`를 사용하지 않는다.

## 설정

| 환경변수 | 기본값 | 설명 |
| :--- | :--- | :--- |
| `EVIDRUG_ACCESS_CODE_HASH` | 없음 | 대화형 명령으로 만든 Argon2 해시 |
| `EVIDRUG_SESSION_SECRET` | 없음 | 세션 서명용 32자 이상 비밀정보 |
| `EVIDRUG_SESSION_COOKIE_NAME` | `evidrug_session` | 인증 쿠키 이름 |
| `EVIDRUG_SESSION_TTL_SECONDS` | `28800` | 세션 유효기간, 기본 8시간 |
| `EVIDRUG_VISITOR_COOKIE_NAME` | `evidrug_visitor` | 브라우저 기록 쿠키 이름 |
| `EVIDRUG_VISITOR_TTL_SECONDS` | `7776000` | 브라우저 기록 ID 유효기간, 기본 90일 |
| `EVIDRUG_AUTH_ATTEMPT_LIMIT` | `5` | 시간 창 안에서 허용할 시도 횟수 |
| `EVIDRUG_AUTH_ATTEMPT_WINDOW_SECONDS` | `300` | 인증 시도 제한 시간 창 |
| `EVIDRUG_CORS_ORIGINS` | 로컬 프론트엔드 | 쿠키 변경 요청을 허용할 Origin 목록 |
| `EVIDRUG_REDIS_URL` | 로컬 Redis | 공유 인증 시도 횟수 저장소 |

다음 명령은 입력 문자를 표시하지 않고 두 번 확인한 뒤 Argon2 해시를 출력한다.

```sh
uv run python -m evidrug_api.auth.hash_access_code
```

접근 코드 해시는 `$` 문자를 포함하므로 Docker Compose용 `.env`에는 명령이 출력한
작은따옴표를 유지해 복사한다. 세션 비밀정보는 접근 코드와 별도로 생성하며 저장소,
프론트엔드 번들 또는 로그에 넣지 않는다.

## 테스트

```sh
uv run pytest tests/test_auth.py
```

테스트는 Redis 대신 동일한 계약의 메모리 제한기를 주입한다. 실제 Redis 연결과 공유
제한 동작은 Docker Compose 통합 검증에서 확인한다.

## 수정과 확장 시 주의점

- 보호할 API에는 `require_authenticated_session`을 의존성으로 추가한다.
- 쿠키를 사용하는 상태 변경 API에는 허용된 Origin 검사를 함께 적용한다.
- 프록시 전달 IP를 신뢰해야 할 때는 신뢰할 프록시 범위를 먼저 정하고 클라이언트 식별 방식을 변경한다.
- 사용자별 기록과 권한이 필요해지면 router 경계를 유지한 채 OAuth 또는 OpenID Connect로 확장한다.
- 접근 코드나 서명 비밀정보를 로그 및 오류 응답에 포함하지 않는다.

## 관련 문서

- [개발 기반 스펙의 접근 제어](../../../../docs/spec.md#7-접근-제어)
- [기여 안내](../../../../CONTRIBUTING.md)
