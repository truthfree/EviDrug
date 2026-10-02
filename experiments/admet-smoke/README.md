# ADMET-AI 독립 검증

## 목적과 책임

ADMET-AI 1.4.0의 Linux 설치, 모델 로딩, 공개 aspirin 분자 단일 추론을 검증한다.
API/worker 연동이나 예측 정확도 평가가 아니다. 실제 실행 결과 확인 전에는 검증 완료로
간주하지 않는다. 최신 ADMET-AI v2는 모델이 달라 v1 baseline과 혼용하지 않는다.

## 구조와 진입점

- `run.sh`: 사용자가 검토 후 실행하는 고정 build/run 명령
- `Dockerfile`, `pyproject.toml`, `uv.lock`: 독립 CPU 의존성 환경
- `smoke.py`: 모델 로딩 → 추론 → 결과 검증 → JSON 보고서
- `test_smoke.py`: 모델 설치 없이 결과 검증 로직만 테스트

| 항목 | 내용 |
| :--- | :--- |
| 이름 | `main()` / `validate_predictions()` |
| 위치 | `smoke.py` |
| 역할 | 고정 분자의 실행 가능성 확인 / 원본 항목 보존과 실패 판정 |
| 입력·출력 | 고정 공개 SMILES / 예측값·패키지 버전·산출물 SHA-256 JSON |
| 오류 | 로딩 실패, 누락 endpoint, 비수치·NaN·무한대는 비제로 종료 |

## 실행

사용자 터미널에서 **스크립트와 Dockerfile을 먼저 검토**하고 저장소 루트에서 실행한다.
Docker Desktop이 실행 중이어야 하며 이미지 다운로드·빌드에 네트워크와 디스크 공간이
필요하다. 컨테이너 메모리 제한은 6 GiB이며 Docker VM에 그 이상의 여유를 권장한다.

```sh
bash experiments/admet-smoke/run.sh
```

모든 인자는 거부한다. 모델 진행 메시지는 터미널에 표시하고, 성공하면 짧은 요약과 함께
전체 JSON의 `experiments/admet-smoke/report.json` 경로를 출력한다. 보고서는 Git과 Docker
build context에서 제외된다. 실패하면 기존 성공 보고서를 덮어쓰지 않고 종료 직전 오류를
공유한다. 비밀값이나 운영 로그는 필요하지 않다.
이 스크립트에는 자동 재시도나 실패 시 다른 모델로 전환하는 동작이 없다.

`linux/amd64`로 고정했으므로 Apple Silicon에서는 Docker의 amd64 에뮬레이션 지원이
필요하고 느릴 수 있다. native arm64/GPU 환경은 이 검증 범위에 포함하지 않는다.
Python은 3.12, ADMET-AI는 1.4.0, PyTorch는 upstream 요구 버전의 CPU 배포판이며,
나머지 의존성과 배포 파일 hash는 `uv.lock`으로 고정한다.
`chemfunc` 초기화가 RDKit SVG 모듈을 함께 불러오므로 headless 추론 이미지에도 최소 X11
및 Expat 공유 라이브러리가 필요하다. 이는 화면 표시나 GUI 실행을 활성화하는 설정이 아니다.
uv 다운로드 캐시는 이미지 레이어가 아닌 Docker build cache에 보존한다. 이미지 용량을
늘리지 않으면서 후속 재빌드에서 동일 패키지 다운로드를 재사용한다. PyTorch 자체는 큰 파일이므로
레이어 압축 해제 중 `input/output error`가 발생하면 Docker Desktop을 재시작하고 호스트와
Docker VM의 디스크 여유 공간을 확인한 뒤 다시 실행한다. 이 오류는 모델 예측 실패가 아니다.

## 보안 경계

- Codex의 `.env` 및 Docker 소켓 접근 차단을 변경하지 않는다.
- Compose, `--env-file`, 호스트 volume, Docker 소켓 mount를 사용하지 않는다.
- 전용 폴더만 build context로 사용하고 `.dockerignore`에서 네 파일만 허용한다.
- 실행 컨테이너는 비root, read-only rootfs, network none, capability 제거를 적용한다.
- 빌드 중에는 외부 패키지 코드가 실행된다. 신뢰할 수 있는 의존성 검토를 대체하지 않는다.
- 이는 사용자가 검토한 명령의 수동 실행이지, 임의 Docker 작업을 안전하게 승인하는
  범용 보안 실행기가 아니다. 파일이 바뀌면 다시 검토해야 한다.
- 이미지는 로컬에 남으며 실행 컨테이너만 종료 후 자동 제거된다.

## 테스트와 확장 주의점

```sh
python3 -m unittest discover -s experiments/admet-smoke -p 'test_*.py'
bash -n experiments/admet-smoke/run.sh
```

unit test 성공은 실제 모델 검증 성공이 아니다. Docker 실행은 별도로 필요하다.
보고서는 모델 파일 및 동봉 DrugBank/endpoint 메타데이터 hash를 기록한다.
endpoint 단위는 동봉 메타데이터를 그대로 보존하며 percentile을 신뢰도로 해석하지 않는다.
기준 집단은 전체 DrugBank approved이며 질환별/ATC별 보정은 하지 않는다.
베이스 이미지 태그와 apt 패키지는 완전한 불변 잠금이 아니므로 이 환경은 feasibility
검증용이다. 운영 승격 시 이미지 digest와 OS 패키지 재현성까지 별도로 확정한다.

실제 통과 후 이 버전·산출물을 기준으로 ADMET adapter와 fixed-plan 실행을 구현한다.
확장 도구/adaptive/Decision recall을 이번 smoke test에 끼워 넣지 않는다.

관련: [기능 명세](../../docs/features/admet-baseline.md),
[Issue #55](https://github.com/truthfree/Dacon2026/issues/55),
[upstream v1 소스](https://github.com/swansonk14/admet_ai/tree/9c8430862b2afd997ff1d314b30bda4418fa9b33)

## 실제 provider와 SQL 연결 검증 (#75)

기존 단일 smoke의 다음 단계로 `runtime.py`가 동일 모델을 두 호출에 재사용한다.
backend subprocess provider → 기존 정규화 adapter → SQL service를 함께 검증한다.
사용자 터미널에서 저장소 루트 기준으로 실행한다.

```sh
bash experiments/admet-smoke/run-provider.sh
```

backend 개발 `.venv`가 필요하다. 없으면 backend에서 `uv sync --frozen --no-env-file`을 실행한다.
로컬 Docker 1 CPU/2GB 제한이며 Render 구독이나 배포를 변경하지 않는다. 운영 DB·`.env`·
API key 없이 메모리 SQLite를 사용한다. report의 `status=passed`, `sql_roundtrip=true`,
`cold_start=true → false`, 동일 manifest hash로 모델 재사용과 저장·복원을 확인한다.
전체 endpoint JSON을 출력하지 않고 endpoint 수와 manifest hash, 자원 측정만 출력한다.

Free 유사 제한을 확인하려면 `run-provider.sh free`를 실행한다. OOM 또는 timeout이 발생할 수
있으며 `oom_killed`와 `exit_code`를 함께 확인한다. 이 검증은 Mac amd64 에뮬레이션이므로
Render 성능 benchmark가 아니다. DTA와 별도로 ADMET 자체의 2GB 적합성을 확인하는 단계다.
두 모델 동시 상주 메모리는 별도 검증이 필요하다. 입력 CCO는 실행 경로 검증용이다.

새 subprocess는 모델 stderr를 폐기하고 공개 가능한 오류 코드만 전달한다. 통합 harness는
자신이 만든 UUID 이름의 컨테이너만 종료·삭제한다. Docker client 종료만으로 컨테이너가
종료된다고 가정하지 않는다. 운영 provider는 Python을 직접 실행하며 Docker 소켓이 필요 없다.

빌드 단계에서 uv.lock으로 설치된 모델 파일·endpoint CSV·DrugBank 참조 hash를 저장하고
runtime 첫 모델 로드 전에 대조한다. 이를 새로운 upstream 인증으로 해석하지 않는다.

2026-09-18 사용자 실행으로 로컬 Docker 1 CPU/2GB 통합 검증을 통과했다.
`status=passed`, `sql_roundtrip=true`였고 두 실행에서 각각 49개 endpoint를 저장·복원했다.
완료 호출 재전달 시 추가 모델 호출 없이 결과를 재사용하는 검증도 통과했다.

| 항목 | 첫 호출 | 모델 재사용 호출 |
| :--- | ---: | ---: |
| runtime 초기화·모델 로딩 | 92.389초 | 약 0초 |
| 추론 | 0.781초 | 0.576초 |
| 프로세스 누적 peak RSS | 763.949 MiB | 763.949 MiB |
| cold_start | true | false |

두 실행의 manifest SHA-256은
`8f92a234e2fe675050bb57fdc7d802d15cc1f09cce04bef9c43d5268773c7923`으로 같았다.
이는 모델·endpoint·참조 metadata가 같다는 의미이며 예측값 전체의 동일성을 뜻하지 않는다.
수집한 컨테이너 상태는 `oom_killed=false exit_code=0`이었다.
로딩 시간에는 라이브러리 import와 asset hash 검증이 포함되고, peak RSS는 모델 프로세스의
누적 최대치다. API·DB를 포함한 합계 메모리, DTA와 동시 상주, Render·Free·운영 PostgreSQL은
이번 실행으로 검증되지 않았다.
