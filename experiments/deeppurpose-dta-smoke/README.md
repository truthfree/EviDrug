# DeepPurpose DTA 독립 검증

## 목적과 범위

초기 DTA baseline인 DeepPurpose 0.1.5의 Linux/amd64 CPU 설치, 공식
`CNN_CNN_BindingDB` checkpoint 로딩과 공개 입력 한 쌍의 단일 추론을 검증한다.
API/worker 연결, 정확도 평가, SQL 저장과 MAMMAL·Boltz-2 provider 구현은 포함하지 않는다.
실제 Docker 실행 결과를 확인하기 전에는 검증 완료로 간주하지 않는다.

이 checkpoint는 SMILES CNN과 단백질 서열 CNN을 결합하며 upstream 문서상 BindingDB Kd를
log scale로 변환해 학습했다. 출력은 `predicted_pkd`로 기록하되 checkpoint 자체에는
machine-readable endpoint·split metadata가 없다는 한계도 함께 보존한다.

## 고정 항목

| 항목 | 값 |
| :--- | :--- |
| DeepPurpose | 0.1.5 PyPI sdist, SHA-256 고정 |
| 모델 | `CNN_CNN_BindingDB` |
| checkpoint | Harvard Dataverse datafile `4159715`, archive SHA-256 고정 |
| Python | 3.10.18 |
| PyTorch | 2.5.1 CPU |
| 실행 환경 | Linux/amd64, CPU 2개, 메모리 4 GiB |

DeepPurpose의 전체 선택 의존성을 설치하지 않고 CNN-CNN 추론 경로에서 import되는 의존성만
독립 `uv.lock`으로 고정한다. 사용하지 않는 DGL 및 Ax 학습 도구는 이 이미지에 포함하지 않는다.
upstream의 `wget` import는 네트워크 호출을 거부하는 로컬 shim으로 대체하며 checkpoint는
빌드 단계에서 hash 검증 후 이미지에 포함한다. 추론에서 사용하지 않는 `lifelines` 평가
함수도 호출 시 실패하는 shim으로 제한해 학습·평가 기능을 smoke 이미지가 제공하는 것처럼
보이지 않게 한다.

## 실행

사용자 터미널에서 파일을 검토하고 저장소 루트에서 다음을 실행한다.

```sh
bash experiments/deeppurpose-dta-smoke/run.sh
```

빌드 단계에만 네트워크를 사용해 고정된 패키지와 checkpoint를 받는다. 런타임은
`--network none`, read-only root filesystem, 비root 사용자로 동작하며 `.env`, host volume,
Docker socket과 비밀값을 전달하지 않는다. 성공 보고서는 Git에서 제외된
`experiments/deeppurpose-dta-smoke/report.json`에 저장된다.

Apple Silicon에서도 upstream wheel 조건과 운영 Linux 환경을 동일하게 유지하기 위해
`linux/amd64`로 실행한다. 따라서 첫 빌드와 추론은 에뮬레이션 때문에 느릴 수 있다.

## 검증과 해석

```sh
python3 -m unittest discover -s experiments/deeppurpose-dta-smoke -p 'test_*.py'
bash -n experiments/deeppurpose-dta-smoke/run.sh
```

unit test는 실제 모델 추론을 대신하지 않는다. Docker 보고서에는 입력, 예측 score type,
checkpoint 파일 hash, 선택 패키지 버전, import·load·inference 시간과 peak RSS가 포함된다.
결과는 실행 가능성 증거이며 정확도나 임상적 결합 증거가 아니다.

2026-09-18 실제 Linux/amd64 CPU 실행에서 `predicted_pkd=5.158909797668457`, model load
0.55초, 단일 inference 0.38초, peak RSS 747.4 MiB를 기록했다. 전체 57.05초 중 56.13초는
Apple Silicon의 amd64 에뮬레이션 환경에서 Python·라이브러리를 import한 cold-start였다.
이 값은 운영 x86 CPU latency benchmark가 아니며 정확도 평가에도 사용하지 않는다.

실행 중 NNPACK이 에뮬레이션 CPU에서 활성화되지 않았다는 경고와 upstream
`torch.load(weights_only=False)` FutureWarning이 출력된다. 이번 smoke에서는 고정 SHA-256의
공식 archive만 격리 이미지에서 로드했으며 두 경고 모두 추론을 중단하지 않았다. production
승격 시 pickle 기반 checkpoint·config 로딩을 계속 격리하고 안전 로딩 전환을 검토한다.

DeepPurpose 0.1.5는 오래된 패키지이고 pretrained checkpoint의 split·calibration 정보가
충분하지 않다. 실제 baseline 채택 후에는 동일 held-out dataset에서 성능을 평가해야 한다.
MAMMAL과 Boltz-2는 이 결과를 덮어쓰는 fallback이 아니라 서로 다른 provider 관측으로
추가하며, provider 장애가 전체 분석을 실패시키지 않도록 후속 adapter에서 격리한다.

관련: [Issue #69](https://github.com/truthfree/EviDrug-Dacon2026/issues/69),
[upstream](https://github.com/kexinhuang12345/DeepPurpose),
[DTA 선행 검증](../../docs/features/dta-baseline.md)

## 실제 provider와 SQL 연결 검증 (#73)

`runtime.py`는 같은 고정 패키지/checkpoint로 JSON-lines 입력을 받는다. 첫 요청에서 파일 hash를
검증하고 모델을 로드하며 후속 요청은 재사용한다. CPU thread 1개와 DataLoader worker 0개로
실행한다. backend `DeepPurposeProvider`가 입력·모델 identity를 검증하고 `DtaExecutionService`가
결과를 SQL로 저장한다. 기존 `run.sh`는 그대로 단일 smoke 용도로 유지한다.

사용자 터미널에서 backend 개발 의존성을 준비한 뒤 저장소 루트에서 실행한다.

```sh
bash experiments/deeppurpose-dta-smoke/run-provider.sh
```

backend `.venv`가 없다면 backend 디렉터리에서 `uv sync --frozen --no-env-file`을 먼저 실행한다.
이 명령은 로컬 Docker에 **1 CPU/2GB** 제한을 적용하며 Render 구독을 변경하지 않는다.
합성 분자·서열 입력 두 번, cold→warm 모델 재사용, SQL 저장/조회, 완료 호출의 재전달을 검증한다.
보고서는 stdout으로 반환하고 자동 저장하지 않는다. 메모리 DB를 사용하므로 운영 DB와 `.env`,
OpenAI key는 사용하지 않는다. 결과는 결합 정확도/유효성 검증에 사용하지 않는다.

Free에 가까운 자원 제한을 별도로 확인하려면 다음을 실행한다.

```sh
bash experiments/deeppurpose-dta-smoke/run-provider.sh free
```

0.1 CPU/512MB에서는 OOM 또는 300초 timeout이 날 수 있다. Mac amd64 에뮬레이션이며
Render 성능을 그대로 재현하는 benchmark는 아니다. 마지막의 `oom_killed`와 `exit_code`는
생성한 컨테이너의 상태다. 137만으로 OOM을 단정하지 않는다. 컨테이너의 실행 상태에 따라
SIGKILL 전에 상태가 수집될 수 있다. 이 실험은 다른 API 컨테이너를 함께 종료하지 않는다.

검증용 harness만 Docker CLI를 사용한다. 운영 provider의 command는 별도 환경의 Python을
직접 가리키며 Docker 소켓을 필요로 하지 않는다. Docker client 강제 종료 후 컨테이너가 남을
수 있으므로 harness는 자신이 만든 UUID 이름의 컨테이너만 마지막에 삭제한다.
두 Python 환경을 같은 Render 인스턴스에 배치할 때는 합계 메모리가 제한을 공유한다.
현재 Render Dockerfile에는 아직 runtime을 설치하지 않았다.

모델 없는 protocol 검증:

```sh
python3 -m unittest discover -s experiments/deeppurpose-dta-smoke -p 'test_*.py'
```

2026-09-18 사용자 실행 결과로 Docker provider 통합 검증을 통과했다.
로컬 Linux/amd64, 1 CPU/2GB 제한에서 `status=passed`, `sql_roundtrip=true`를 확인했다.

| 항목 | 첫 호출 | 모델 재사용 호출 |
| :--- | ---: | ---: |
| provider 전체 시간 | 71.835초 | 0.486초 |
| runtime 초기화·모델 로딩 | 58.768초 | 약 0초 |
| 추론 | 0.590초 | 0.482초 |
| 프로세스 누적 peak RSS | 747.109 MiB | 747.109 MiB |
| cold_start | true | false |

두 호출의 합성 입력 출력은 `predicted_pkd=3.261671781539917`로 같았다.
메모리 SQLite 저장·복원과 완료 호출 재전달 시 결과 재사용도 검증했다.
종료 시 수집한 컨테이너 상태는 `oom_killed=false`, `exit_code=0`이었다.
첫 호출의 provider 전체 시간은 컨테이너 시작·통신 등을 포함하며 runtime 로딩 시간과 다르다.
peak RSS는 모델 프로세스의 누적 최대치로, API·worker·DB를 합한 메모리가 아니다.
Render 실측, 운영 PostgreSQL 및 Free(512MB) 실행은 이번 결과로 검증되지 않았다.
관련: [Issue #73](https://github.com/truthfree/EviDrug-Dacon2026/issues/73).
