# EviDrug Frontend

React, TypeScript, Vite와 npm으로 구성한 EviDrug 웹 애플리케이션이다. Node 24 LTS를
사용한다. 공개 서비스 안내, 공용 접근 코드 인증과 이후 분석 작업공간의 사용자 경험을
담당하며 백엔드에는 HTTP API로만 접근한다.

## 폴더 구조

```text
frontend/src/
├── authentication/          # 세션 확인, 접근 코드 인증과 로그아웃
│   ├── AccessCodePanel.tsx  # 접근 코드 입력 UI
│   ├── AccessCodePanel.css  # 접근 코드 화면 스타일
│   ├── api.ts               # 인증 API 계약과 응답 검증
│   └── useAuthentication.ts # 앱 인증 상태와 사용자용 오류
├── disease-search/          # Open Targets 질환 자동완성과 명시적 후보 선택
├── analysis-input/          # 타깃 방식과 SMILES 입력 및 백엔드 검증
├── analysis-progress/       # orchestration 실행 상태와 결과 목업
├── landing/                 # 공개 서비스 안내와 근거 연결 시각화
├── shared/                  # 브랜드와 공용 아이콘
├── workspace/               # 인증 후 분석 작업공간
├── App.tsx                  # 인증 상태에 따른 최상위 화면 전환
├── App.css                  # 공용 레이아웃과 조작 요소 스타일
└── index.css                # 전역 디자인 토큰과 기본 스타일
```

## 코드 읽는 순서

1. `src/App.tsx`에서 인증 상태에 따른 공개 화면과 작업공간 전환을 확인한다.
2. `src/authentication/useAuthentication.ts`에서 세션 상태와 사용자 행동 흐름을 확인한다.
3. `src/authentication/api.ts`에서 FastAPI 요청·응답 계약과 오류 분기를 확인한다.
4. `src/landing/PublicLanding.tsx`와 `src/authentication/AccessCodePanel.tsx`에서 공개 화면과 입력 동작을 확인한다.
5. `src/workspace/Workspace.tsx`에서 인증 후 화면을 확인한다.
6. `src/disease-search/`에서 질환 자동완성과 후보 확인 흐름을 확인한다.
7. `src/analysis-input/`에서 타깃 방식과 SMILES 입력 검증 흐름을 확인한다.
8. `src/analysis-progress/`에서 orchestration 실행 목업·영속 상태 및 Target 결과 표시를 확인한다.
9. `src/index.css`와 각 기능의 CSS 파일에서 디자인 토큰과 화면 표현을 확인한다.

## 주요 함수와 컴포넌트

| 이름 | 위치 | 역할 | 입력·출력 | 주요 오류 |
| :--- | :--- | :--- | :--- | :--- |
| `App` | `src/App.tsx` | 인증 상태에 따라 공개 랜딩 또는 작업공간을 표시 | 입력 없음, React 화면 반환 | 연결 실패 시 공개 안내 유지 |
| `AccessCodePanel` | `src/authentication/AccessCodePanel.tsx` | 공용 접근 코드 입력과 표시 전환 제공 | 제출·취소 callback, React 화면 반환 | 인증 오류를 입력 가까이에 표시 |
| `useAuthentication` | `src/authentication/useAuthentication.ts` | 세션 확인, 로그인과 로그아웃 상태 관리 | 인증 상태와 행동 함수 반환 | API 오류를 사용자 행동 중심 문구로 변환 |
| `readCurrentSession` | `src/authentication/api.ts` | 현재 HttpOnly 쿠키의 세션 확인 | `SessionResponse` 또는 `null` | 비정상 응답 및 서버 오류 |
| `createAuthenticatedSession` | `src/authentication/api.ts` | 접근 코드로 세션 생성 | 접근 코드, `SessionResponse` 반환 | 잘못된 코드, 시도 제한, 서비스 중단 |
| `deleteAuthenticatedSession` | `src/authentication/api.ts` | 현재 세션 쿠키 삭제 | 입력·반환 없음 | 연결 및 서버 오류 |
| `DiseaseSearchPanel` | `src/disease-search/DiseaseSearchPanel.tsx` | 질환 자동완성과 명시적 후보 확인 | 사용자 입력과 후보 선택 | 빈 결과, 세션 만료, 제공자 장애 |
| `useDiseaseSearch` | `src/disease-search/useDiseaseSearch.ts` | debounce와 검색 요청 상태 관리 | 검색어, 후보와 상태 반환 | 오래된 요청 취소 및 오류 문구 변환 |
| `searchDiseases` | `src/disease-search/api.ts` | 질환 검색 API 호출과 응답 검증 | 영어 검색어, 후보 목록 반환 | 인증 만료, 결과 없음, 검색 서비스 장애 |
| `AnalysisInputPanel` | `src/analysis-input/AnalysisInputPanel.tsx` | 타깃 방식과 SMILES 입력 및 확인 | 확정 질환, 검증된 입력 callback | 잘못된 SMILES, 인증 만료, 입력 조합 오류 |
| `validateAnalysisInput` | `src/analysis-input/api.ts` | 백엔드 분석 입력 검증 호출 | 질환·타깃 방식·SMILES, canonical SMILES 반환 | 비정상 응답, 세션 만료, 유효하지 않은 입력 |
| `AnalysisProgressPanel` | `src/analysis-progress/AnalysisProgressPanel.tsx` | orchestration 단계별 상태와 결과 목업 | 목업 시나리오, React 화면 반환 | 부분 실패와 전체 실패 표현 |
| `useMockAnalysis` | `src/analysis-progress/useMockAnalysis.ts` | 실행 상태 snapshot을 순차적으로 재생 | 선택 시나리오, 현재 frame과 조작 함수 | 실제 API를 호출하지 않음 |
| `AnalysisRunPanel` | `src/analysis-progress/AnalysisRunPanel.tsx` | 실제 분석 생성과 영속 단계 상태 표시 | 검증된 입력, React 화면 반환 | Agent 미구성, polling 실패 |
| `useAnalysisRun` | `src/analysis-progress/useAnalysisRun.ts` | 멱등 생성과 polling 수명주기 관리 | 검증된 입력, 실행 상태와 조작 함수 | 연속 조회 실패와 세션 만료 |

## 인증 데이터 흐름

1. 앱 진입 시 `GET /api/v1/auth/session`으로 기존 세션을 확인한다.
2. 미인증이어도 공개 랜딩은 유지하며 `분석 시작`을 누르면 접근 코드 입력을 표시한다.
3. `POST /api/v1/auth/session`이 성공하면 서버가 발급한 HttpOnly 쿠키로 작업공간에 진입한다.
4. 새로고침하면 1번부터 다시 확인하며 접근 코드를 브라우저 저장소에 보관하지 않는다.
5. 로그아웃은 `DELETE /api/v1/auth/session`으로 쿠키를 만료시킨다.

## 디자인 토큰

색상은 `src/index.css`의 의미 기반 CSS 변수에서 한 번만 정의한다. 화면과 컴포넌트는
`--color-brand-action`, `--color-surface-primary`, `--color-status-warning`처럼 용도를
나타내는 토큰을 사용하며 색상값을 직접 작성하지 않는다. 브랜드 색을 변경할 때는 이
토큰 값만 수정하고 상태 색은 성공, 경고, 오류 및 안내의 의미를 유지한다.
현재 브랜드 방향은 네이비(`--color-brand-deep: #1d3553`)와 청록
(`--color-brand-action: #126d77`)이다. 청록은 조작 가능한 요소와 선택 상태에 사용하고,
성공·경고·오류·안내는 별도의 상태 토큰과 텍스트/아이콘으로 구분한다. 결과 수치의
높고 낮음을 브랜드색만으로 암시하지 않는다. 바탕은 `#f5f8fa`, 카드 면은 흰색에 가깝게
유지해 긴 분석 보고서를 읽기 쉽게 한다.

## 로컬 실행

```sh
npm ci
npm run dev
```

기본 개발 서버는 `http://localhost:5173`에서 열리며 `/api` 요청을
`http://localhost:8000`으로 전달한다. Vercel에서는 `VITE_API_BASE_URL`을 비워 두어
동일 origin의 `/api`를 사용한다. 로컬 API 설정과 접근 코드는 변경하지 않는다.

## Vercel Preview 배포

Vercel 프로젝트는 모노레포의 `frontend`를 Root Directory로 사용한다. 빌드 설정은
[`vercel.json`](./vercel.json)에 기록하며 Node.js 24에서 `npm ci`와 `npm run build`를
실행한 뒤 `dist`를 배포한다.

`dev`는 공용 스테이징 Preview 브랜치로 사용하며 최신 화면은
[dev Preview](https://evidrug-frontend-git-dev-evi-drug.vercel.app)에서 확인한다.
`ignoreCommand`는 Root Directory에서 직전 커밋과 비교하여 프론트엔드 변경이 없는
백엔드 전용 커밋의 빌드를 건너뛴다.

## Render API 연결

`vercel.json`의 `/api/:path*` rewrite는 요청을
`https://evidrug-api.onrender.com/api/:path*`로 전달한다. 브라우저는 Vercel 주소로만
요청하므로 기존 Secure/HttpOnly/SameSite=Lax 쿠키 정책을 유지한다.
API 응답은 브라우저와 CDN에 저장하지 않도록 no-store 헤더를 설정한다.
외부 rewrite 캐싱을 별도로 활성화하지 않는다.

- Vercel의 `VITE_API_BASE_URL`은 삭제하거나 빈 값으로 둔 뒤 재배포한다.
- Render `EVIDRUG_CORS_ORIGINS`에는 `["https://evidrug-frontend.vercel.app"]`을 설정한다.
- Preview에서 로그인까지 검증하려면 해당 Preview origin을 명시적으로 추가해야 한다.
  모든 Preview를 와일드카드로 허용하지 않는다.
- 2026-09-17 Render 직접 health 요청에서 200/staging을 확인했다.
  이는 Redis 또는 프론트 로그인 성공 검증이 아니다.
- main 반영 후 `/api/v1/health`, 로그인, 새로고침 세션 복원, 로그아웃,
  질환 검색과 SMILES 검증을 실제 브라우저에서 확인한다.
- 개발자 도구에서 요청 URL이 프론트 origin인지, 세션 쿠키가 발급되는지,
  인증 응답에 no-store가 적용되는지 확인한다. 코드/쿠키를 로그나 캡처에 남기지 않는다.
- 프록시 경유 클라이언트 IP 식별과 시도 제한은 #42의 공개 전 검증 항목이다.
- 무료 API의 유휴 정지 후 첫 요청은 느리거나 실패할 수 있다. 서버가 깨어난 후 재시도한다.

프록시 대상 변경은 `vercel.json`에서 수행한다. 인증 후 `/`는 실제 분석,
`/preview`는 실행 목업, `/results`는 최근 저장 결과 목록, `/results/{analysis_id}`는
저장 결과 상세 화면이다. 화면 간 이동은
진행 중인 분석 상태를 유지하며, 새로고침·새 탭에서는 해당 화면을 직접 연다.
`Demo data` 실행 목업은 실제 API를 호출하지 않으며,
`Live workflow` 화면은 분석 생성·조회 API에 연결된다. [Target 결과 UI](../docs/features/target-prioritization-ui.md)는
저장된 공개 요약을 표시하며 결과를 열어보는 동작으로 provider/LLM을 추가 호출하지 않는다.

[ADMET·DTA·Decision 결과 카드](../docs/features/specialist-results-ui.md)는 실제 분석 종료 후
공개 결과 API를 GET으로 조회한다. 작업공간의 **저장 결과**에서 기존 분석 ID로도 조회할 수
있으며, 결과 조회와 재시도는 새 분석을 생성하지 않는다. 현재 세션 또는 유효한 동일
브라우저 방문자 쿠키로 소유권을 확인한다.

## 검증

### Decision v5.7 구조화 결과 (#299)

`analysis-progress/SpecialistResultsPanel.tsx`는 서버의 구조화 판정에 대해 영문 배지와
한국어 부제, 네 영역 평가, 대표 후보 행, 강점·위험·다음 단계 및 접힌 상세 근거를
표시한다. 서버가 계산한 대표 후보와 독성 확인시험 우선순위를 그대로 사용하며 화면에서
판정을 다시 계산하지 않는다. CToxPred2는 채널·분류·분류 확률을 원 관측과 함께 표시한다.

`specialistResults.ts`가 새 필드 계약을 검증하고 과거 결과의 읽기 경로를 유지한다.
`specialistResultsReview.ts`는 구조화 평가와 서버 메타데이터를 내보내기에 보존한다.
합성 데이터 기반 `DecisionV57.test.tsx`는 대표 후보·축 배지·과거 호환 및 잘못된 응답을
검증한다. 실제 서버 계약은 선행 백엔드 #296–#298에 의존하므로 이 PR을 먼저 배포하지
않는다. 모바일 overflow·화면 캡처 및 10초 스캔 검증은 아직 완료하지 않았다.

```sh
npm run lint
npm run typecheck
npm test
npm run build
```

인증 테스트는 미인증 공개 화면, 접근 코드 성공과 실패, 시도 제한, 기존 세션 복원,
로그아웃 및 연결 복구를 검증한다. 내부 함수 호출 순서보다 사용자가 화면에서 확인할 수
있는 동작을 기준으로 작성한다.
orchestration 목업 테스트는 전체 성공, DTA 건너뜀기, 전체 실패와 초기화를 화면 기준으로 검증한다.

## 수정과 확장 시 주의점

- 인증 API 필드나 오류 코드가 변경되면 먼저 `authentication/api.ts`의 타입과 검증을 수정한다.
- 사용자용 인증 문구는 `authentication/useAuthentication.ts`에서 관리한다.
- 접근 코드를 전역 상태, URL, 브라우저 저장소 또는 로그에 추가하지 않는다.
- 질환 입력 같은 후속 기능은 인증된 `Workspace` 영역에서 별도 기능 폴더로 연결한다.
- 색상과 간격을 바꿀 때는 의미 기반 토큰을 우선 수정하고 컴포넌트별 예외를 최소화한다.

관련 UX 기준은 [`docs/ux/current-experience.md`](../docs/ux/current-experience.md)를 따른다.
