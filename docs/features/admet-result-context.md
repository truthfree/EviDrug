# ADMET 저장 결과 조회와 compact context

## 목표와 범위

SQL에 저장된 예측을 재계산하지 않고 필요한 endpoint만 선택해 Agent 입력의 반복 metadata를
줄인다. 이 단계의 요약은 LLM 설명 생성이나 위험 판정이 아니라 결정적인 열·행 선택이다.
원 예측값, percentile, 단위와 출처를 유지하며 다른 점수로 변환하거나 반올림하지 않는다.

구현 범위는 인증된 서버가 사용하는 읽기 전용 `AdmetContextBuilder`다.
Agent가 직접 호출하는 조회 tool 등록·승인·실행 기록, 공개 HTTP API, ArtifactReference
등록, DTA projection 및 LLM Agent 루프는 후속 범위다. 이번 계층을 Agent가 직접 호출하여
ToolRegistry/admission을 우회하게 해서는 안 된다.

## 입력과 정상 흐름

호출자는 소유권을 확인한 `analysis_id`를 서버 context에서 전달한다. 조회 인수는
`source_tool_call_id`와 중복 없는 1~20개 `endpoint_ids`다. analysis_id는 LLM 인수에서 받지 않는다.
이전 run 결과를 쓸 수 있지만 같은 분석 안의 정확한 원 실행을 지정해야 한다.
SMILES 기반 전역 검색이나 최신 실행 자동 선택은 하지 않는다.

1. 분석 ID와 원 실행 ID를 함께 조건으로 성공 결과를 조회한다.
2. 요청 endpoint의 prediction과 필요한 metadata column만 SQL에서 join한다.
3. 요청 순서대로 `columns + rows` 배열을 만들고 원 실행/run·manifest·tool version·
   endpoint metadata hash·참조 집단/hash·해석 제한을 한 번 덧붙인다.
4. catalog endpoint 수와 부분 선택 여부를 명시하고 최대 32 KiB UTF-8 JSON인지 검사한다.

열 순서는 endpoint_id, name, category, task_type, value, units,
drugbank_approved_percentile, species, source_url이다. key를 매 행 반복하지 않는다.
모델 artifact 상세·dataset 크기·benchmark 지표·SMILES는 이 projection에서 제외하며 원 SQL에 남는다.
manifest hash는 원본 모델 metadata를 찾는 식별자이지 context 내용 자체의 hash가 아니다.

## 실패와 해석 경계

- 없는 결과와 다른 분석의 결과는 동일한 `result_not_found`로 반환한다.
- 요청 endpoint가 catalog에 없거나 예측 행이 없으면 `endpoint_unavailable`로 전체 조회를 거부한다.
- 미지원 manifest는 `unsupported_manifest`, 크기 초과는 `context_too_large`다.
- 빈/중복/과다 선택, 비정상 수치·분류 범위는 Pydantic 검증에서 거부한다.
- 실패 시 자동 재추론하거나 다른 모델로 fallback하지 않는다.
- 단위가 없으면 null로 유지하고 dimensionless로 추정하지 않는다.
- percentile은 참조 순위이며 confidence도, 모든 endpoint에 공통인 안전성 방향도 아니다.
- 선택하지 않은 endpoint가 안전하거나 정상이란 뜻이 아니다. 해석 제한을 크기 때문에
  조용히 삭제하지 않고 응답 자체를 거부한다.

새 SQL table/migration은 없다. builder는 SELECT만 실행하고 autoflush·commit·rollback을
수행하지 않는다. 전용 읽기 session을 권장하며 거래 종료는 호출자의 책임이다.
서버가 analysis 소유권 및 upstream provenance를 먼저 검증해야 하며 UUID만으로 인증하지 않는다.
기존 성공 도메인 결과를 사용하므로 과거 ledger 도입 이전 결과도 읽을 수 있다.

## 검증

합성 SQLite 데이터로 선택·순서·원 값·단위·출처·JSON round-trip, 타 분석 접근 차단,
누락 endpoint, 잘못된 입력, 크기 상한, SELECT-only/no-autoflush를 검증한다.
전체 정규화 report보다 JSON byte 수가 줄어드는 것을 검사하며, tokenizer별 토큰 절감률이나
실제 LLM 비용·판단 품질을 검증한 것으로 해석하지 않는다. 실제 모델/API 호출은 하지 않는다.
백엔드 [Issue #81](https://github.com/truthfree/EviDrug-Dacon2026/issues/81), 프론트엔드 변경 없음.
