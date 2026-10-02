# DTA baseline 선행 검증

## 결정

초기 DTA baseline은 기존 구상대로 DeepPurpose의 `CNN_CNN_BindingDB`로 시작한다.
운영 CPU 서버에서 재현 가능한 로컬 기본 도구를 먼저 확보하고, MAMMAL과 Boltz-2는
전문 DTA Agent가 추가 근거가 필요할 때 선택할 수 있는 보조 provider 후보로 유지한다.

DeepPurpose 선택은 다른 모델보다 정확하다는 결론이 아니다. CPU 자원, 로컬 재현성,
외부 서비스 장애와 호출 제한에 의존하지 않는 기본 실행 경로를 우선 확보하기 위한 결정이다.
동일 평가셋의 정확도 비교 전에는 모델 간 성능 우위를 주장하지 않는다.

## 이번 범위

사용자가 독립 Docker 명령을 검토하고 실행한 뒤 다음 항목을 확인한다.

- DeepPurpose 0.1.5와 고정된 `CNN_CNN_BindingDB` checkpoint 설치
- SMILES와 단백질 서열을 사용한 CPU 단일 추론
- 유한한 `predicted_pkd` scalar 반환
- package·checkpoint hash, 실행 시간과 peak RSS 기록
- 런타임 네트워크와 비밀정보 없이 재현 가능한 실행

정확도 평가, production provider interface, 데이터베이스 저장과 Agent 연결은 후속 Issue로
분리한다. baseline 실행 가능성은 아래 실제 Docker 결과를 기준으로 확인했다.

## 검증 결과

2026-09-18 Apple Silicon Docker Desktop의 Linux/amd64 CPU 환경에서 독립 smoke가 통과했다.

| 항목 | 결과 |
| :--- | :--- |
| 모델 | `CNN_CNN_BindingDB` |
| 고정 입력 | aspirin / SARS-CoV-2 main protease |
| 출력 | `predicted_pkd=5.158909797668457` |
| import / model load / inference | 56.13초 / 0.55초 / 0.38초 |
| peak RSS | 747.4 MiB |
| PyTorch thread | 2 |

import 시간은 Apple Silicon에서 amd64 이미지를 에뮬레이션한 단일 cold process 측정값이므로
운영 x86 CPU latency로 일반화하지 않는다. 모델 load와 단일 inference 자체는 1초 미만이었고,
현재 자원 규모는 CPU baseline 후보로 후속 adapter 검증을 진행할 수 있는 수준이다.
예측값은 실행 확인용 공개 입력의 모델 출력이며 과학적 정확도나 결합 근거로 사용하지 않는다.

NNPACK 최적화가 에뮬레이션 환경에서 비활성화됐다는 경고와 upstream `torch.load`의 향후
`weights_only` 기본값 변경 경고가 발생했다. 둘 다 이번 추론 실패는 아니었다. 패키지와
checkpoint archive/file hash를 고정했지만 pickle 기반 artifact라는 사실은 남으므로,
production adapter에서는 격리된 이미지와 hash 검증을 유지하고 안전 로딩 전환 가능성을
별도로 확인한다.

## 확장 경계

후속 DTA 도구는 같은 provider 계약 뒤에 둔다.

```text
DTA provider
├── DeepPurpose: CPU 로컬 기본 관측
├── MAMMAL: pKd 보조 관측 후보
└── Boltz-2: pIC50-like·binder probability·구조 관측 후보
```

서로 다른 score type을 평균하거나 하나의 공통 confidence로 변환하지 않는다. 각 실행은
provider, model, score type, 원 단위, 버전, latency, 상태와 원본 응답 참조를 보존한다.
보조 provider의 timeout, rate limit 또는 장애는 `unavailable` 관측으로 격리하고 기본 결과를
삭제하지 않는다. Decision은 이를 부정적인 결합 근거가 아니라 데이터 공백으로 해석한다.

실행 안내는 [DeepPurpose DTA 독립 검증](../../experiments/deeppurpose-dta-smoke/README.md)을
따른다. 관련 작업은 [Issue #69](https://github.com/truthfree/EviDrug-Dacon2026/issues/69)다.
