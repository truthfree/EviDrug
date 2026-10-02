# CToxPred2 심장 이온통로 도구

## 현재 범위

CToxPred2 RF-SSL runtime의 report를 hERG, Nav1.5, Cav1.2 typed observation으로 검증한다.
현재 단계는 계약, 격리 subprocess provider, 공통 runner 기반 SQL 저장, admission binding과
live fallback 없는 observation snapshot/replay를 제공한다. 실제 RF-SSL CPU smoke에서 model
load 후 peak RSS 약 486 MiB, 첫 추론 후 약 511 MiB, warm 중앙값 약 0.165초를 확인했다.
배포 worker의 Linux 재측정과 동시성 검증은 후속 운영 Gate다. 이미지 설치와 실행 절차는
[`docs/deployment/ctoxpred2-recall.md`](../../../../docs/deployment/ctoxpred2-recall.md)에 있다.

코드 읽는 순서는 다음과 같다.

1. `contracts.py`: RF-SSL model identity, 채널별 label과 class probability
2. `adapter.py`: 격리 runtime report 검증과 typed result 변환
3. `tests/test_ctoxpred2_adapter.py`: 정상·입력 불일치·누락 채널·잘못된 출력 fixture
4. `provider.py`: 관리자가 고정한 argv만 실행하는 JSON-lines subprocess 경계
5. `service.py` / `repository.py`: 공통 실행 ledger와 채널별 SQL round-trip
6. `trajectory/ctox_snapshot.py`: source hash·모델 identity를 검증하는 replay
7. `relationship.py`: ADMET hERG와의 비교 가능성 및 추가 채널 관계 계약

## 호출 정책

일반 분석에서는 ADMET-AI baseline을 항상 한 번 실행한다. CToxPred2는 ADMET Agent가
자동으로 연쇄 호출하지 않는다. Decision이 `cardiac_ion_channel_evidence` 공백과 목적·사유를
요청하면 서버가 이를 ADMET Agent로 전달한다. ADMET recall run은 capability matcher에서
CToxPred2를 후보로 선택하고 기존 admission으로 최대 한 번 실행한다. Decision 출력에는
provider나 tool 이름을 선택할 권한이 없다.

오프라인 trajectory 생성에서는 대표 고유 SMILES별 live observation을 한 번 snapshot한 뒤
`baseline`, `all_tools`, `decision_recall` 정책을 replay한다. replay miss는 live 실행으로
보충하지 않는다.

ADMET recall Agent에 분석 소유 snapshot ID를 주입하면 CToxPred2 도구 관측을 replay한다.
Decision은 snapshot hash와 원 실행의 분석·run·모델 identity를 다시 확인한다. replay
episode는 snapshot version과 `replay_miss`를 기록하며 새 모델 호출을 만들지 않는다.

## ADMET-AI와의 관계

- ADMET-AI `hERG`와 CToxPred2 `hERG`는 같은 위험 영역을 다루지만 endpoint 정의와 판정
  기준의 동등성이 확인되지 않았으므로 항상 `unresolved`로 둔다.
- 같은 방향처럼 보여도 `concordant`, 다른 방향처럼 보여도 `discordant`로 만들지 않는다.
- ADMET percentile과 CToxPred2 `class_probability`를 평균하거나 서로 변환하지 않는다.
- Nav1.5와 Cav1.2는 baseline의 직접 대응 endpoint가 없는 `related_signal`로 별도 보존한다.

## 출력 의미

- `positive`: 해당 채널에 대해 upstream model의 positive class가 선택됨
- `negative`: 해당 채널에 대해 upstream model의 negative class가 선택됨
- `class_probability`: random forest `predict_proba`에서 선택된 class의 값

`class_probability`는 임상 위험 확률, 독성 강도나 검증된 uncertainty가 아니다. 결과에는 항상
다음 범위 제한을 함께 전달한다.

- ion-channel liability이며 임상 QT 연장 또는 부정맥 확정이 아님
- 음성 결과가 심장 안전성을 입증하지 않음
- 심근병증, 좌심실 기능 저하와 심부전은 평가 범위 밖임

## 고정 artifact

Upstream commit `2a31aa119e27b6b69a5588d18a01f2a27fef4524`의 다음 파일을 사용한다.

| artifact | SHA-256 |
| :--- | :--- |
| `decriptors_preprocessing/global_preprocessing_pipeline.sav` | `58ca80d195ca2f261aa50e68e88a82aebdccdc106149f89d5bb9d10d54e7f301` |
| `random_forest/hERG/_ssl_herg_model.joblib` | `1060d8afee2df325410e11fe5b1a6457656f158f63dc4baab0dc1ccc9a0a98c6` |
| `random_forest/Nav1.5/_ssl_nav_model.joblib` | `0b57bc75c80518c1ec01a597f0fde3cbeb5a6a55f8f48d7afc26ea5b8ff30b8b` |
| `random_forest/Cav1.2/_ssl_cav_model.joblib` | `d7be3bcb59890058208a18167246c395d5ac4ad5b9d762ff07b0b0643ac375e9` |

파일은 upstream RAR에서 추출되며 pickle/joblib이므로 hash를 확인한 격리 runtime에서만 로드한다.
현재 저장소는 가중치 파일 자체를 vendoring하지 않는다. PoC 이미지 빌드가 고정 URL에서
아카이브를 가져와 checksum을 확인하고, 필요한 4개 파일만 추출한다. worker는 recall
활성화 시 시작과 분석 실행 전에 같은 SHA-256을 검증한다.

실제 runtime과 2026-09-25 smoke 결과는
[`experiments/ctoxpred2-smoke`](../../../../experiments/ctoxpred2-smoke)에 기록한다.

## 검증

```sh
uv run --no-env-file pytest tests/test_ctoxpred2_adapter.py
uv run --no-env-file ruff check src/evidrug_api/ctoxpred2 tests/test_ctoxpred2_adapter.py
uv run --no-env-file mypy src/evidrug_api/ctoxpred2 tests/test_ctoxpred2_adapter.py
```

관련 결정은 [CToxPred2 RF profile](../../../../docs/decisions/003-ctoxpred2-rf-profile.md)을 따른다.
