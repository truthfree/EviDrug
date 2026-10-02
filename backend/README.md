# EviDrug Backend

FastAPI API와 Celery worker가 도메인 코드를 공유하는 Python 패키지다. Python 3.12와
uv 잠금 파일을 사용한다.

새 도구·provider 또는 Agent 실행 연결에는 출처, 고정 버전, 입력·출력 계약, 실패 방식,
실행 예산과 재배포 조건을 문서화한다.

```sh
uv sync --frozen --dev
uv run uvicorn evidrug_api.main:app --reload
```

API 문서는 `http://localhost:8000/api/v1/docs`, health endpoint는
`http://localhost:8000/api/v1/health`에서 확인할 수 있다.

배포 환경에는 `.env.example`의 값을 실제 비밀 관리 기능으로 주입하고, 공개 API 앞에는
인증, 요청량·동시 실행·본문 크기 제한과 비용 상한을 구성한다.

## 주요 기능

- [공용 접근 코드 인증](src/evidrug_api/auth/README.md)
- [Dacon OpenAI Responses 연동](src/evidrug_api/openai_gateway/README.md)
- [Open Targets 질환 검색](src/evidrug_api/disease_search/README.md)
- [타깃 방식 및 SMILES 입력 검증](src/evidrug_api/analysis_input/README.md)
- [Target Prioritization과 causal support](src/evidrug_api/target_hypothesis/README.md)
- [분석 작업 생성 및 상태 조회](src/evidrug_api/analysis_jobs/README.md)
- [고정 DAG 분석 orchestration](src/evidrug_api/orchestration/README.md)
- [저장 결과 재사용·Agent 단독 실행 CLI](../docs/features/agent-replay.md)
- [ADMET·DTA 공통 실행 상태와 장애 회수](src/evidrug_api/tool_execution/README.md)
- [Agent별 도구 등록·승인·실행 예산](src/evidrug_api/tool_admission/README.md)

인증을 사용하려면 먼저 접근 코드 해시와 세션 비밀정보를 `.env`에 설정한다. 평문
접근 코드를 셸 기록에 남기지 않도록 대화형 해시 생성 명령을 제공한다.

```sh
uv run python -m evidrug_api.auth.hash_access_code
```

출력된 `EVIDRUG_ACCESS_CODE_HASH`는 작은따옴표를 포함한 형태로 `.env`에 복사한다.
`EVIDRUG_SESSION_SECRET`에는 접근 코드와 다른 32자 이상의 무작위 값을 사용한다.

```sh
openssl rand -hex 32
```

로그인 세션은 무작위 nonce를 포함하며, 분석 기록에는 별도의 브라우저 ID를 사용한다.
브라우저 ID는 서명된 `HttpOnly` 쿠키로 기본 90일간 유지되고 재로그인할 때 갱신된다.
로그아웃은 단기 로그인만 종료하므로 같은 브라우저에서 다시 로그인하면 새 분석 기록을
찾을 수 있다. 쿠키 삭제·만료·다른 기기에서는 복구되지 않으며, 배포 전에
`EVIDRUG_SESSION_SECRET`을 변경하면 기존 로그인 및 브라우저 쿠키가 모두 무효화된다.
동작과 제약은 [브라우저별 분석 기록](../docs/features/browser-analysis-history.md)에 정리했다.

worker는 Redis가 실행 중일 때 다음 명령으로 시작한다.

```sh
uv run celery -A evidrug_api.worker:celery_app worker --loglevel=INFO
```

PostgreSQL schema는 API와 worker를 시작하기 전에 Alembic으로 적용한다.

```sh
uv run alembic upgrade head
```

도구 및 분석 실행 ledger migration도 먼저 적용해야 한다. 주기적인 lease 회수는 worker 외에
별도 Beat 프로세스가 필요하다(환경당 하나만 실행).

```sh
uv run celery -A evidrug_api.worker:celery_app beat --loglevel=INFO --schedule=/tmp/evidrug-celerybeat
```

Compose에서는 운영자가 루트에서 `docker compose --profile maintenance up -d scheduler`로
선택적으로 활성화한다. 매 60초 회수 작업을 큐에 넣으며 실제 처리 시점은 worker 가용성에
따라 지연될 수 있다. 기본 worker는 Target만 연결하고 전용 PoC worker에서 ADMET/DTA/Decision을
연결한다. [PoC 실행 안내](../docs/features/poc-full-execution.md)를 참고한다.
도구 수동 회수는 `uv run python -m evidrug_api.tool_execution.recover`로 실행한다.

검증 명령은 다음과 같다.

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```
