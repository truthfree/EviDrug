# 타깃 방식 및 SMILES 입력

질환 확인 다음 단계에서 사용자가 타깃 지정 여부와 화합물의 SMILES를 입력하고 백엔드의
분석 입력 계약으로 검증하는 기능이다.

## 사용자 흐름

1. 앞 단계에서 Open Targets 질환 후보를 확정한다.
2. `타깃 없이 탐색` 또는 `특정 타깃 평가`를 선택한다.
3. 특정 타깃 평가에서는 타깃명을 입력한다.
4. 두 방식 모두 화합물의 SMILES를 입력한다.
5. 백엔드 검증이 성공하면 canonical SMILES를 확인한다.

입력값이 바뀌면 이전 검증 결과를 즉시 초기화한다. 현재 타깃명은 사용자가 입력한 문자열로
보존하며 표준 타깃 ID 검색은 후속 기능에서 진행한다.

## 주요 코드

- `AnalysisInputPanel.tsx`: 입력 상태, 조건부 타깃 필드와 사용자용 오류
- `api.ts`: `/api/v1/analysis-inputs/validate` 요청·응답 계약과 런타임 검증
- `AnalysisInputPanel.css`: 타깃 선택 카드, SMILES 입력과 검증 결과 표현

## 제외 범위

- 타깃 후보 검색과 UniProt ID 확정
- 분자 구조 이미지
- PubChem 또는 ChEMBL 등록 확인
- 실제 Agent 및 분석 작업 실행
