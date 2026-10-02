# 003. CToxPred2 RF-SSL 실행 profile

## 상황

유방암 저분자 후보의 심장 이온통로 위험을 보강하기 위해 CToxPred2를 로컬 도구로 추가한다.
Upstream은 hERG, Nav1.5, Cav1.2 각각에 대해 supervised DNN과 semi-supervised random forest
(RF-SSL)를 제공한다.

DNN 경로는 inference에서 모델을 train mode로 두고 100회 MC-dropout 결과를 평균한다. RF-SSL
경로는 fingerprint와 Mordred descriptor를 전처리한 뒤 채널별 random forest의
`predict_proba`와 argmax label을 반환하며 upstream notebook의 기본 profile이다.

## 결정

첫 POC와 trajectory snapshot 생성에는 `rf_ssl` profile만 사용한다.

- upstream repository: `issararab/CToxPred2`
- 검토 commit: `2a31aa119e27b6b69a5588d18a01f2a27fef4524`
- 지원 채널: hERG, Nav1.5, Cav1.2
- label: upstream 1/0을 내부 `positive`/`negative`로 보존
- score: 선택된 class의 `predict_proba`를 `class_probability`로 보존
- preprocessing/model artifact 네 개의 SHA-256을 tool identity에 포함

`class_probability`를 uncertainty, 임상 발생 확률이나 독성 강도로 부르지 않는다. 세 채널의
음성 결과도 심장 안전성 증거로 변환하지 않는다.

## 선택 이유

- upstream notebook의 기본 실행 경로다.
- 고정 artifact와 입력에서 결정적인 snapshot을 만들기 쉽다.
- DNN profile의 100회 stochastic forward와 seed 정책을 먼저 정의하지 않아도 된다.
- PyTorch 1.12.1 의존성을 초기 runtime에서 제외할 수 있다.
- 세 채널을 같은 feature/preprocessing 흐름에서 실행한다.

## 감수하는 단점

- RF `predict_proba`는 calibration이 검증된 confidence가 아니다.
- RF tree ensemble의 합의가 out-of-domain 여부나 epistemic uncertainty를 직접 제공하지 않는다.
- Mordred, PyBioMed, Open Babel과 upstream pickled joblib artifact에 의존한다.
- Upstream artifact는 압축 상태로 약 150MB이며 안전하지 않은 pickle 로딩 위험이 있다.

따라서 artifact hash를 검증한 격리 이미지에서만 로드하고, API 프로세스에서 직접 unpickle하지
않는다. 모델 결과가 서로 다르거나 probability가 낮아도 팀이 임의의 low-confidence threshold를
만들지 않는다.

## 재검토 조건

- RF-SSL이 대표 유방암 약물 fixture에서 재현되지 않는다.
- 논문 또는 upstream이 공식 calibration/uncertainty 계약을 제공한다.
- DNN MC-dropout의 seed·반복 횟수·분산 저장 계약을 고정하고 RF 대비 이득을 확인한다.
- dependency 또는 artifact 보안 문제로 격리 runtime 배포가 불가능하다.
