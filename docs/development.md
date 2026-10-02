# EviDrug 개발자 안내

EviDrug의 기술 구성과 로컬 개발 진입점을 정리한 문서입니다. 기능별 구조와 운영 설정은
각 영역의 상세 문서를 따릅니다.

## 기술 스택

| 영역 | 기술 |
| --- | --- |
| Frontend | React 19, TypeScript 6, Vite 8 |
| Backend API | Python 3.12, FastAPI, Pydantic |
| Agent runtime | OpenAI API, OpenAI Agents SDK |
| 비동기 작업 | Celery, Redis |
| 데이터베이스 | PostgreSQL 17, SQLAlchemy, Alembic |
| 화학정보 처리 | RDKit |
| 로컬 환경 | Docker Compose |
| 배포 | Vercel, Render |
| 품질 검증 | Pytest, Ruff, mypy, Vitest, Testing Library, oxlint |

정확한 패키지 버전은 `backend/uv.lock`과 `frontend/package-lock.json`을 기준으로 합니다.

## 저장소 구조

```text
.
├── backend/       FastAPI API, Celery worker와 분석 도메인
├── frontend/      React 기반 웹 애플리케이션
├── docs/          제품, 기능, 평가와 운영 문서
├── experiments/   기능 도입 전 검증과 재현 자료
├── scripts/       저장소 공용 작업 스크립트
└── compose.yaml   로컬 통합 실행 환경
```

## 빠른 시작

Docker Desktop이 실행 중인 상태에서 저장소 루트에서 전체 서비스를 시작합니다.

```sh
cp .env.example .env
python3 scripts/doctor.py
docker compose up --build
```

- 프론트엔드: `http://localhost:5173`
- API: `http://localhost:8000`
- OpenAPI 문서: `http://localhost:8000/api/v1/docs`
- 상태 확인: `http://localhost:8000/api/v1/health`

종료할 때는 데이터 볼륨을 유지하는 다음 명령을 사용합니다.

```sh
docker compose down
```

기본 Compose worker는 모든 대용량 분석 모델을 포함하지 않습니다. ADMET, DTA와 Decision을
실제로 실행하려면 [PoC 전체 실행 안내](features/poc-full-execution.md)를 따릅니다.
새 checkout의 인증 설정과 실행 수준별 요구사항은 [로컬 실행 안내](local-setup.md)를 먼저
확인합니다.

## 개별 개발 환경

### Backend

```sh
cd backend
uv sync --frozen --dev
uv run uvicorn evidrug_api.main:app --reload
```

데이터베이스 migration, worker와 환경 설정은
[백엔드 개발 안내](../backend/README.md)에 정리되어 있습니다.

### Frontend

```sh
cd frontend
npm ci
npm run dev
```

개발 서버는 `/api` 요청을 로컬 API로 전달합니다. 화면 구조, 테스트와 배포 설정은
[프론트엔드 개발 안내](../frontend/README.md)를 참고합니다.

환경변수 이름과 예시는 각 애플리케이션의 `.env.example`에서 확인합니다. 비밀값은
저장소에 커밋하지 않으며, 첫 개발 전에
[비밀정보 보호 설정](team-rules/secret-protection.md)을 적용합니다.

## 검증

변경한 영역에 맞는 검증을 실행합니다.

```sh
cd backend
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest

cd ../frontend
npm run lint
npm run typecheck
npm test
npm run build

cd ..
docker compose config --quiet
```

## 더 읽기

- [전체 문서 목록](README.md)
- [팀 개발 규칙](team-rules/README.md)
- [백엔드 개발 안내](../backend/README.md)
- [프론트엔드 개발 안내](../frontend/README.md)
- [현재 프로젝트 상태](project-status.md)
