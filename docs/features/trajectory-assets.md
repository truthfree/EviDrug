# 학습 가능한 Agent trajectory 자산

## 배경과 목표

현재 EviDrug는 고정 DAG에서 Target Hypothesis, ADMET, DTA와 Decision을 실행하고 Agent run,
도구 관측과 사용량을 저장한다. 다음 단계는 제한된 도구와 예산 안에서 여러 행동 경로를
생성·비교하고 이후 retrieval, tool router, preference 학습과 value model에 재사용할 수 있는
trajectory 자산을 만드는 것이다.

trajectory는 LLM의 비공개 장문 사고과정이 아니다. 다음 행동을 검증할 수 있는 상태,
행동 후보, 선택 목적, 저장된 관측 참조, 상태 변화와 평가를 구조화한 실행 기록이다.

## 이번 구현 범위

- episode마다 live/replay, snapshot version, 정책과 자원 상한 고정
- 각 step의 전후 상태, 가능한 행동 전체, 선택과 관측 참조 저장
- 완료·실패 episode의 불변 종료
- 자동 검사, LLM judge 또는 사람 평가를 독립적으로 추가
- 같은 분석의 episode 사이 preference pair 저장
- Alembic migration과 SQLite/PostgreSQL SQL 검증

현재 capability metadata와 결정적 matcher는 `admet_ai`, `ctoxpred2`, `dta`의 지원 범위와
명시적 제외 사유를 `TrajectoryActionCandidate`로 투영한다. #156의 adaptive selector는
이 후보에 고정 allowlist·버전·예산·비용/지연 등급을 적용해 다음 도구를 결정한다.
선택은 도구 실행 승인이 아니며 기존 registry/admission 권한 경계를 유지한다.

다음 항목은 후속 범위다.

- LLM 기반 adaptive selector 및 production 자동 rollout
- DTA observation snapshot·replay executor
- counterfactual branch와 대량 생성 runner
- Decision 품질 평가와 사용자 API/UI
- 사용자 API와 화면
- SFT, preference optimization 또는 offline RL 실행

## 핵심 불변 조건

1. 기존 고정 DAG와 production Agent 구현은 변경하지 않는다.
2. trajectory는 기존 run/tool 원장을 참조하며 원 관측을 임의로 다시 작성하지 않는다.
3. 선택된 행동은 당시 저장된 `available_actions` 안에 있어야 한다.
4. stop 행동은 새 관측을 만들지 않는다.
5. replay는 고정 snapshot version이 없으면 시작하지 않는다.
6. step은 1부터 연속적으로 추가하며 종료 뒤 수정하지 않는다.
7. 서로 다른 분석의 episode는 직접 preference pair로 만들지 않는다.
8. rejected·failed·ambiguous 기록도 삭제하지 않고 학습·감사 자산으로 유지한다.

## ADMET snapshot·replay

ADMET의 terminal tool call을 source로 삼아 tool version, 정규화 입력 hash, 결과 hash와
모델·endpoint metadata·reference population hash를 versioned snapshot에 고정한다. replay는
같은 typed `ToolObservation[AdmetToolResult]`를 반환하고 원 result를 artifact로 참조한다.

snapshot에 없거나 입력·도구 버전이 다른 요청은 명시적으로 실패하며 live provider로 자동
전환하지 않는다. 원 실행이 실패했다면 같은 고정 오류를 실패 관측으로 재현한다. replay의
소요 시간과 사용량은 원 live 실행의 latency·비용으로 보고하지 않는다.

## 다음 마일스톤

같은 분석의 ADMET·CToxPred2 snapshot에서 `baseline`, `all_tools`, `decision_recall` 세 정책의
episode를 생성하고 검증·비교하는 파일럿 runner가 있다. snapshot 원본의 hash/model 출처를
runner가 사전 검증하고 각 typed replay executor가 입력/버전을 검사한다. 구조 validator는
저장된 profile/step 해시, 순서, 상태·예산 변화와 run 참조를 읽기 전용으로 검사한다.
adaptive episode의 고정 selector 입력은 validator가 재계산한다. Decision 품질 평가는
후속 범위다. 정책별 다중 case 집계는
실패·miss를 분모에 포함한다. 실 provider 호출은 snapshot 생성 시에만
허용하고 replay miss를 live 호출로 숨기지 않는다. 기계적 경로 비교를 Decision 임상 판단
품질 비교로 해석해서는 안 된다.
거부된 live admission은 유효한 관측 snapshot으로 만들지 않으며, 해당 입력의 비교는
시작 전에 실패한다. 정책상 제외된 도구와 요청 대기 중인 도구는 각기 다른 reason code로
기록한다.
replay episode는 Decision 요청·ADMET 응답·재검토의 step 관계와 Agent별 호출 번호를
명시하지만 실제 Agent run이나 Decision LLM 결과를 생성하지 않는다.

현재 Decision cardiac recall은 별도의 3-step episode를 저장한다. Decision의 gap 요청,
ADMET의 capability match/제외 사유와 tool observation 참조, Decision의 재판단을 기록한다.
ADMET recall에 CToxPred2 snapshot ID가 주입되면 같은 typed 관측을 replay하고 snapshot
누락은 `replay_miss`로 남긴다. 대량 episode 생성은 아직 후속 작업이다.
