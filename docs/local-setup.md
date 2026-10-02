# 로컬 실행 안내

이 문서는 새 checkout에서 EviDrug를 실행할 때 필요한 조건과 실행 수준별 차이를 설명합니다.
저장소만 clone하면 기본 웹/API 환경은 구성할 수 있지만, 실제 전체 분석에는 별도의 인증 정보와
모델 이미지가 필요합니다.

## 준비 사항

- Git
- Docker Desktop 또는 Docker Engine과 Compose v2
- 기본 실행 기준 여유 디스크 8 GiB 이상
- 전체 모델 실행 시 amd64 컨테이너 실행 지원과 추가 빌드 시간

먼저 예제 설정을 복사하고 실제 값은 로컬 `.env`에만 둡니다.

```sh
cp .env.example .env
python3 scripts/doctor.py
```

진단 스크립트는 설정값 자체를 출력하지 않으며 외부 API를 호출하지 않습니다. `.env`를 Git에
추가하지 마세요.

## 인증 설정

로그인을 사용하려면 접근 코드의 해시와 32자 이상의 세션 비밀값이 필요합니다.

```sh
cd backend
uv run python -m evidrug_api.auth.hash_access_code
```

출력된 해시를 `EVIDRUG_ACCESS_CODE_HASH`로 설정하고, 별도로 생성한 충분히 긴 임의 문자열을
`EVIDRUG_SESSION_SECRET`로 설정합니다. 두 값이 없더라도 컨테이너와 health endpoint는 뜰 수
있지만 로그인 API는 `503 auth_unavailable`을 반환합니다.

## 기본 서비스 실행

```sh
docker compose up --build
```

- 프론트엔드: `http://localhost:5173`
- API: `http://localhost:8000`
- API 문서: `http://localhost:8000/api/v1/docs`
- 상태 확인: `http://localhost:8000/api/v1/health`

기본 worker에는 대용량 ADMET/DTA 모델이 포함되지 않습니다. 따라서 UI와 API 개발, DB migration,
인증 및 일부 외부 데이터 경로를 확인하는 용도이며 전체 분석 성공을 보장하지 않습니다.

종료 시 데이터 볼륨을 보존하려면 다음을 사용합니다.

```sh
docker compose down
```

`docker compose down -v`는 로컬 DB와 Redis 볼륨도 삭제하므로 초기화가 명확히 필요한 경우에만
사용합니다.

## 전체 PoC 실행

전체 분석에는 다음이 추가로 필요합니다.

- 대회에서 제공한 호환 LLM API key와 사용 가능한 quota
- Dacon gateway가 허용하는 model 설정
- ADMET-AI, DeepPurpose, CToxPred2 이미지 빌드에 필요한 네트워크와 디스크
- Open Targets, UniProt 등 외부 과학 데이터 API에 대한 네트워크 접근

현재 기본 gateway는 일반 OpenAI endpoint가 아니라 Dacon 전용 endpoint와 `api-key` 헤더 계약을
사용합니다. 임의의 OpenAI API key를 넣는 것만으로 호환된다고 가정하면 안 됩니다.

```sh
python3 scripts/doctor.py --full
bash scripts/build-poc.sh
docker compose -f compose.yaml -f compose.poc.yaml up -d --build
```

Apple Silicon에서는 worker가 `linux/amd64` 에뮬레이션으로 실행되어 최초 빌드와 추론이 더 오래
걸릴 수 있습니다. 더 자세한 실행 계약과 알려진 제한은
[PoC 전체 실행 연결](features/poc-full-execution.md)을 참고합니다.

## 문제 확인 순서

1. `python3 scripts/doctor.py`의 `FAIL`을 먼저 해결합니다.
2. `docker compose ps`에서 postgres, redis, api 상태를 확인합니다.
3. 로그인 실패 시 인증 환경변수 두 개가 비어 있지 않은지 확인합니다.
4. 전체 분석 실패 시 `--full` 진단과 worker 로그를 확인합니다.
5. 외부 API 오류는 로컬 코드 오류와 구분해 endpoint, quota, 네트워크 상태를 확인합니다.
