# Open Targets 질환 검색

사용자가 입력한 영어 질환명 또는 표현형을 Open Targets의 표준 엔티티 후보로
변환하는 백엔드 경계다. 이 단계에서는 OpenAI 모델을 호출하지 않는다.

## 흐름

1. `POST /api/v1/diseases/search`가 인증된 요청과 영어 검색어를 받는다.
2. `OpenTargetsDiseaseSearcher`가 GraphQL search를 `disease` 엔티티로 제한해 호출한다.
3. Open Targets의 relevance score 순서로 최대 설정 개수의 후보를 반환한다.
4. 후보가 하나여도 `requires_confirmation=true`를 반환해 자동 확정을 막는다.

## 주요 구성요소

- `DiseaseSearchRequest`: 영어 검색어 길이와 문자 범위를 검증한다.
- `DiseaseCandidate`: Open Targets ID, 표준명, 설명, relevance score를 표현한다.
- `DiseaseSearcher`: 테스트와 실제 제공자를 교체할 수 있는 검색 계약이다.
- `OpenTargetsDiseaseSearcher`: GraphQL 요청과 응답 파싱만 담당한다.
- `search_diseases`: 인증, 후보 없음, 제공자 장애를 HTTP 응답으로 구분한다.

질환 후보 선택 UI와 선택 결과 저장, 확정된 질환의 타깃 근거 수집은 후속 기능의
책임이다.
