# Analysis progress

백엔드 orchestration의 고정 DAG를 화면에서 표현한다. 실행 양식을 검토하는 데모 시나리오와
검증된 입력으로 실제 작업을 생성하고 영속 상태를 polling하는 화면을 분리한다. 데모 결과는
과학적 판정으로 사용하지 않는다.

## 구성

| 파일 | 역할 |
| :--- | :--- |
| `AnalysisProgressPanel.tsx` | 데모 시나리오 선택, 단계별 상태와 결과 표현 |
| `useMockAnalysis.ts` | 일정한 간격으로 영속 상태 snapshot을 재생 |
| `mockScenarios.ts` | 전체 성공, 부분 실패, 전체 실패 상태 예시 |
| `contracts.ts` | 목업 상태와 단계 계약 |
| `AnalysisRunPanel.tsx` | 실제 작업 ID와 단계 상태만 표시하고, 종료 후 결과 상세로 이동 |
| `TargetResultsLoader.tsx` | 저장 결과 화면에서 Target 근거를 별도 GET으로 조회 |
| `useAnalysisRun.ts` | 작업 생성, polling, 일시 오류 backoff와 종료 판정 |
| `api.ts` | 분석 생성·조회 API 응답 계약 검증 |
| `recentAnalyses.ts` | 같은 브라우저의 최근 분석 목록 API 응답 계약 검증 |
| `SavedResultsLookup.tsx` | 최근 목록의 입력 요약과 ID 수동 조회, 상세 페이지 이동 |
| `targetPrioritization.ts` | 공개 Target 요약 타입·중첩 응답 검증·서열 필드 제외 |
| `TargetPrioritizationPanel.tsx` / `.css` | primary·대안·제외 후보, 접근성·인과성·방향과 개별 근거 |
| `targetLabels.ts` | 정책 코드·상태·근거 항목의 한국어 표시 |
| `targetDemo.ts` | breast cancer / CDK4 저장 예시와 명시적인 합성 대안 |
| `fixtures/cdk4-api-summary.json` | 실제 DB output의 공개 API projection 회귀 자료 |

## 상태 흐름

- `Target Hypothesis`와 `ADMET`은 병렬로 시작한다.
- 표적 입력이 준비되면 `ADMET` 완료 전에도 `DTA`를 시작할 수 있다.
- 사용 가능한 전문 근거를 모은 뒤 `Decision`을 실행한다.
- 필요한 입력이 없어서 `skipped`가 된 단계는 음성 예측으로 해석하지 않는다.
- 전문 근거가 하나도 없으면 `Decision`도 실행하지 않고 전체 실패로 종료한다.

실제 실행은 `Idempotency-Key`로 작업을 생성한 뒤 2.5초 기본 간격으로 PostgreSQL 상태를
조회한다. 단일 조회 실패는 전체 분석 실패로 해석하지 않고 backoff하며, 3회 연속 실패 시
사용자에게 수동 재시도를 제공한다. terminal 상태에서는 polling을 중지한다.

`/results`에서는 최근 분석의 질환·표적 방식·SMILES·상태·생성 시각만 먼저 불러온다.
결과 본문은 사용자가 항목을 선택해 `/results/{analysis_id}`로 이동할 때만 기존 저장 결과 GET으로 조회한다. 목록 조회 실패와
빈 목록을 구분하며, ID 수동 조회를 계속 제공한다. 브라우저별 소유권은 백엔드 #176이 검증한다.

## Target 결과 표시

`api.ts` → `parseTargetPrioritization` → `AnalysisRunPanel` → `TargetPrioritizationPanel` 순서로
읽는다. `target_prioritization`은 생략/null을 허용하며, 존재하는 잘못된 중첩 필드에는
`unexpected_response`를 반환한다. 화면은 기존 polling만 사용하고 추가 provider/LLM을 호출하지 않는다.

후보별 `pharos_evidence`는 없거나 null인 과거 결과와 ablation을 허용한다. 값이 있으면
TDL, target family, novelty, ligand/publication 수를 별도 표적 성숙도 블록으로 표시하며,
질환 인과성·저분자 접근성·임상 성공 가능성과 분리해 설명한다.

- primary와 alternatives는 순위로 구분하고, 연관성 점수는 퍼센트가 아닌 0–1 값으로 표시한다.
- 저분자 표적화 가능성, 인과 근거 상태, 정책 계산 방향과 LLM 제안 방향은 별도 항목이다.
- 방향 충돌은 `therapeutic_direction_conflicting`을 기준으로 표시한다. 인과 근거가
  `supported`여도 방향이 충돌할 수 있으므로 두 상태를 하나로 합치지 않는다.
- `eligible`은 임상적 성공이 아니며, `exploratory`에는 추가 검증 필요를 표시한다.
- 사유 코드·개별 근거는 기본 접힘 상태이며 키보드로 열 수 있다. 알 수 없는 사유 코드도 숨기지 않는다.
- 전체 분석이 실패해도 저장된 Target 결과는 유지하고 최종 판정과 구분한다.
- 공개 필드만 복사하므로 예상치 못한 `target_sequence` 필드는 UI 모델에도 남기지 않는다.
- 결과 상세의 `결과 JSON 다운로드`는 입력 맥락과 Agent별 결과·근거·한계만 내보낸다.
  실행 ID, 도구 호출 ID, 호출 흐름, 토큰 사용량과 소요시간은 복사하지 않는다.

인증 후 별도 `/preview` 화면에서 **전체 성공 → 목업 실행**으로 결과 카드를 무료로 확인할 수 있다.
예시 질환은 breast cancer, 표적은 CDK4, 약물은 palbociclib다. CDK4는 실제 공개 응답 예시이며
PIK3CA 대안·ESR1 제외 후보는 화면 검증용 합성 데이터다. 데모를 현재 입력의 실제 분석 결과로
표시하지 않는다. 부분 실패/전체 실패 목업에는 이 합성 성공 결과를 삽입하지 않는다.

## 검증

#299의 `fixtures/decision-v57-server-projection.json`은 #307 병합 후 최신 dev
`62b26d6`의 두 첨부 개발 사례를 `case_fixture` → `validate_assessment` →
`DecisionResult` JSON 저장/읽기 → `project_decision` → 공개 DTO JSON 직렬화로
만든 고정 응답이다. `DecisionV57.test.tsx`가 이 응답을 클라이언트 파서 → 화면 →
JSON export에 연결해 필드와 서버 메타데이터 보존을 검사한다. 앞선 수동 재구성 fixture와
구분하며 HTTP/운영 DB/유료 모델 실행을 대신하지 않는다.

자동 계약 검사와 별개로 모바일/데스크톱 실제 렌더링의 overflow·캡처 및 사람이
10초 안에 판정을 이해하는지 확인해야 한다. 이 검토와 #300의 지정 개발 사례 각 3회
검증은 완료되지 않았다.

DTA 결과 카드의 `model_runs`는 Agent 호출 횟수와 다른 모델별 도구 실행이다. 각 모델의
pKd·상태·도구 호출 ID를 분리해 표시하고, 과거 응답에서 목록이 없으면 기존 대표 모델
필드를 표시한다. 값의 평균이나 실패값 0 대체는 하지 않는다.

```sh
npm run lint
npm run typecheck
npm test
npm run build
```

실제 CDK4 fixture는 2026-09-21 개발 DB의 공개 projection 및 백엔드
`TargetPrioritizationSummary`와 일치함을 읽기 전용으로 확인했다. 내부 run ID·서열·비밀정보는
fixture에 포함하지 않았다. 테스트는 성공/실패 중 결과 유지, null/과거 schema, 잘못된 수치·enum,
서열 필드 제외, 합성 방향 충돌, 알려지지 않은 사유·긴 표적명·HTML 문자열을 검증한다.

실제 브라우저의 모바일/데스크톱 줄바꿈 확인은 [검증 절차](../../../docs/features/target-prioritization-ui.md)를 따른다.

관련 작업은 #85,
#91과
`docs/features/analysis-orchestration.md`를 참고한다.
