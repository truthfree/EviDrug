# EviDrug 기여 안내

버그 제보와 기능 제안은 GitHub Issue로 먼저 공유해 주세요. 보안 취약점은 공개 Issue가
아니라 [보안 정책](.github/SECURITY.md)의 비공개 제보 경로를 사용합니다.

## 개발 환경

- 백엔드: Python 3.12와 `uv`
- 프런트엔드: Node.js 22와 npm
- 로컬 인프라: Docker Compose

설치와 실행 방법은 [개발자 안내](docs/development.md), 백엔드 세부 구조는
[`backend/README.md`](backend/README.md), 프런트엔드는
[`frontend/README.md`](frontend/README.md)를 확인하세요.

## 변경 원칙

- 한 Pull Request에는 하나의 목적만 포함합니다.
- 동작 변경에는 관련 테스트와 사용자·개발자 문서 변경을 함께 제출합니다.
- 외부 모델, 데이터 또는 패키지를 추가할 때는 출처, 고정 버전, 라이선스와 재배포 조건을
  `THIRD_PARTY_NOTICES.md`에 기록합니다.
- API 키, 접근 코드, 세션 값과 `.env` 파일을 커밋하지 않습니다.
- 생성한 보고서와 실제 사용자 입력·결과를 테스트 fixture로 추가하지 않습니다.

## 검증

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/check_secrets.py staged

cd backend
uv sync --locked
uv run pytest

cd ../frontend
npm ci
npm test -- --run
npm run build
```

환경이나 변경 범위 때문에 실행하지 못한 검증은 Pull Request에 이유와 영향을 적습니다.
