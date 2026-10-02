# Target 후보·근거 결과 UI

## 목적과 범위

Target 단계의 완료 여부를 넘어 추천 후보와 판단 근거를 화면에서 이해한다. #95 공개 요약과
#103 인과성 확장을 소비하는 프론트엔드 #97 작업이다. API·DB·Agent 정책은 변경하지 않는다.
DTA 결과·최종 Decision 판정·과거 분석 목록·자동 replay는 이번 범위가 아니다.

## 사용자 흐름

1. 실제 분석의 polling 응답에 `target_prioritization`이 있으면 단계 카드 아래에 표시한다.
2. primary는 우선 추천 카드, alternatives는 순위별 대안 카드로 구분한다.
3. 질환 연관성, 저분자 표적화 가능성, 인과 근거, 정책 계산 방향, LLM 제안 방향을 각각 읽는다.
4. 필요한 경우 정책 사유와 개별 근거·출처를 펼친다. 추가 네트워크 요청은 없다.
5. 제외 후보와 제외 사유를 별도로 확인한다. 제외를 생물학적 무효로 단정하지 않는다.

Target 완료 후 다른 단계가 실패해도 저장된 Target 결과는 표시한다. 전체 실행 상태와 Target
가설을 분리하며, Target 추천을 최종 약물 효능 판정으로 제시하지 않는다.

## 표현 원칙

- association은 소수점 세 자리의 0–1 지표다. 백분율·성공 확률·druggability로 바꾸지 않는다.
- `eligible`: 승인·임상 단계의 저분자 약물 선례나 리간드 결합 구조 등, 이 표적을 저분자로 표적화할 수 있다는 근거가 확인된 상태다. 해당 질환에서의 치료 효과나 임상적 성공을 뜻하지 않으므로 초록 성공 배지로 단순화하지 않는다.
- `exploratory`: 탐색 후보·추가 검증 필요. 양성 접근성 근거가 없다는 사실을 표시한다.
- `ineligible`: 저분자 표적화 기준 미충족. 일반 실행 실패와 다른 문구를 사용한다.
- 인과 근거 상태와 치료 방향 충돌은 별개다. `supported`도 방향 충돌 경고와 함께 표시할 수 있다.
- 정책 계산 방향과 LLM 제안 방향은 각자 서버 값을 표시하고, 프론트에서 새 방향을 추론하지 않는다.
- LLM 설명은 해석·연구 가설로 표시하며 독립적인 결합/임상 근거 또는 치료 권고로 제시하지 않는다.
- 승인 약물·임상 선례는 표적 수준 정보이며 입력 약물의 해당 질환 치료 승인으로 해석하지 않는다.
- 기본 화면은 요약이며 정책 코드·개별 근거는 키보드로 조작 가능한 `details/summary`로 제공한다.
- 알려지지 않은 정책 사유는 코드를 그대로 남긴다. null/과거 결과에는 없는 후보를 만들지 않는다.

## 공개 계약과 보안

기존 `GET /api/v1/analyses/{id}`의 optional `target_prioritization`을 사용한다.
`targetPrioritization.ts`에서 중첩 enum·점수 범위·정수·ID·필수 필드를 검사한다.
과거 응답의 생략/null은 허용하고, 과거 후보에서 인과 필드가 생략됐으면 서버의 기본값과 동일하게
unknown으로 표시한다. 존재하지만 잘못된 필드는 `unexpected_response`로 처리한다.

public 필드만 새 객체로 투영해 예상치 못한 단백질 서열을 UI 상태에 보존하지 않는다.
LLM 문구는 React 텍스트로 렌더링하고 HTML을 삽입하지 않는다. 원문 링크나 외부 API를 자동으로
열지 않으며, 후보/근거 펼침에 새 LLM 호출이 없다.

## 무료 화면 확인

```sh
docker compose up -d --build
```

로그인 후 **실행 목업 → 전체 성공 → 목업 실행**을 누른다. Target 단계가 완료되면 같은 결과
컴포넌트가 표시된다. **실제 분석 실행**과는 다른 버튼이며 이 목업은 토큰을 사용하지 않는다.

- 질환: breast cancer
- primary: CDK4의 실제 공개 응답 예시
- 약물 표시: palbociclib
- 대안 PIK3CA, 제외 ESR1 및 대안의 점수·접근성·충돌 근거: UI 상태 검증용 합성 데이터

합성 데이터는 실제 평가 결과가 아니며 화면의 데모 안내를 유지한다. partial/failed 목업에
성공 shortlist를 재사용하지 않는다. 실제 분석 결과는 `Live workflow`에서 서버 응답을 표시한다.

## 완료 확인과 테스트

```sh
cd frontend
npm ci
npm run lint
npm run typecheck
npm test
npm run build
```

자동 검증: 실제 CDK4 공개 fixture round-trip, 과거/null/잘못된 중첩 응답, 서열 필드 제외,
후보 구분, 추가 검증 상태, 알 수 없는 사유, 방향 충돌, HTML 문자열, 긴 표적명,
전체 실패 중 Target 표시, 근거 펼침 시 추가 요청 없음.
fixture는 `TargetPrioritizationSummary` 및 개발 DB의 실제 공개 projection과 동일함을 확인했다.

사람이 확인할 항목:

- [ ] 데스크톱(약 1440px)과 모바일(320/390px)에서 가로 스크롤·잘린 텍스트가 없는가
- [ ] 긴 표적명/버전/근거 ID가 카드 밖으로 넘치지 않는가
- [ ] 한글은 가능한 어절 단위로 줄바꿈하고 긴 영문 ID는 필요한 경우 줄바꿈하는가
- [ ] 키보드 Tab·Enter로 근거/제외 목록을 열고 닫을 수 있는가
- [ ] 실제 Target 성공·후속 단계 실패 결과가 최종 성공처럼 보이지 않는가

현재 환경에서 자동 브라우저 실행이 OS/sandbox 오류로 막혀 viewport 기반 화면 캡처는
완료하지 못했다. `word-break: keep-all`, `overflow-wrap: anywhere`, 가변 열과 `min-width: 0`
스타일 및 긴 텍스트 DOM 테스트를 적용했지만, 실제 레이아웃 검증을 대체하지 않는다.
2026-09-21 사용자 확인: 데스크톱 목업과 실제 분석에서 후보 카드 표시 및 근거/제외 목록
펼침을 확인했다. 실제 분석에서 Target 결과가 후속 단계 실패와 별도로 표시되는 것도
확인했다. 단계 카드에는 어절 단위 줄바꿈을 추가했고 타입 검사·빌드·58개 테스트를
재검증했다. 모바일 viewport 및 키보드 전용 조작은 별도 수동 확인 대상으로 남긴다.

## 수정 위치

- 타입/공개 응답 검증: `frontend/src/analysis-progress/targetPrioritization.ts`
- 상태·정책 코드 번역: `targetLabels.ts`
- 카드/접기 UI·반응형: `TargetPrioritizationPanel.tsx`, `.css`
- 실데이터 연결: `api.ts`, `AnalysisRunPanel.tsx`
- 무료 목업: `targetDemo.ts`, `AnalysisProgressPanel.tsx`

관련: #97,
#95,
#103.
