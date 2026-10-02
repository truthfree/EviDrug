# Decision Agent

## v5.7 출력·검증·저장 (#298)

현재 생성은 `decision-gated-synthesis-v5.7`과 엄격한 12필드 `DecisionAssessment`를 쓴다.
첨부 전문 `prompt_v5_7.txt`의 마지막 개행만 제거하며 LF SHA-256 앞 12자리는
`d5bd73424fca`다. 입력 한도 64 KiB(+512 포함), 출력 8,192 tokens를 적용한다.
최초 호출과 교정 호출 모두 recall default를 제거해 모든 속성을 required로 선언한 생성 전용
strict JSON schema를 사용한다. JSON 파싱 실패, 미완료 응답과 근거 수치 범위·지표 라벨 위반은
같은 입력으로 교정 재호출을 최대 1회 수행하고 응답은 다시 기존 `DecisionAssessment`로 검증한다.
수치 범위·라벨 교정은 네 assessment 영역의 `key_values`를 비워 같은 오류의 반복을 막는다.
두 호출의 token·외부 요청 사용량과 교정 사유를 보존한다. schema·인용·정책 오류와
네트워크 실패는 교정 대상이 아니며 자동 재호출하지 않는다. 이 경로는 #301의
ADMET strict JSON 전송 모드와 별개이며 Decision은 서버가 생성 응답을 검증한다.

필수 필드는 decision_phase/verdict/headline/assessment/key_strengths/key_concerns/
rationale/used_evidence_ids/conflicts/gaps/next_actions/recall_request다. recall 요청에도
12필드를 요구한다. `LegacyDecisionAssessment`는 과거 저장 읽기 전용이며 새 생성의
검증을 완화하지 않는다. 새 실행 결과는 `decision-context-v5.7`과 `server_metadata`를
저장하고 공개 API의 `decision.result`에도 그대로 투영한다. 버전 없는 과거 결과는 v4로
읽고 새 headline/assessment/strengths/concerns/metadata는 None이다.

`validation.py`는 실제 `DtaScoreType.PREDICTED_PKD` 계약을 사용한다. pKd→단위별 Kd와
원 관측/기준선 값을 소수 0/1/2자리 또는 유효숫자 2자리 표시와 대조한다. 과거 로컬 오류의
4.811026573181152/6.590145587921143 관측도 typed DTA JSON 경계 회귀로 검증한다.
생성 문장의 인용 마커 제거·긴 소수 표시 정규화는 새 문장 필드에도 적용한다.

| C-3 | 검증 위치·고정 코드 | 처리 |
| --- | --- | --- |
| 1 | DecisionAssessment / decision_schema_invalid | 거부 |
| 2 | validate_assessment / decision_go_restricted | 거부 |
| 3 | validate_policy / decision_go_gate_unresolved | 거부 |
| 4 | validate_policy / decision_no_go_experimental_evidence_missing | 거부 |
| 5–7 | validate_policy / decision_area_status_mismatch | 거부 |
| 8 | allowed_values / decision_key_value_out_of_range | 거부 |
| 9 | LABEL_RULES / decision_key_value_label_mismatch | 거부 |
| 10 | admet_evidence / decision_toxicity_axes_mismatch | 거부 |
| 11 | validate_policy / decision_ctox_herg_value_ambiguous | 경고 |
| 12 | validate_policy / decision_lead_candidate_summary_missing | 경고 |
| 13 | INTERNAL_TERMS / decision_internal_terms_in_prose | 경고 |
| 14 | validate_policy / decision_non_lead_value_unlabelled | 경고 |

기존 decision_response_incomplete/json_invalid/citation_unknown/specialist_citation_missing/
recall_not_allowed/next_actions_missing과 후처리 prose_empty_after_citation_cleanup 거부도
유지한다(각 코드 앞 decision_). 실행 오류는 decision_input_budget_exceeded,
decision_model_unavailable, decision_invalid_output이며 표시 정규화는
decision_inline_citations_removed/decision_numeric_display_normalized 경고로 기록한다.
no_go의 직접 반박 여부 등 문장 의미 전체는 서버가 보장하지 않으며 C-3 범위와 프롬프트
역할을 구분한다. 수치·판정 거부를 문체 경고로 낮추지 않는다.

회귀 진입점은 test_decision_v57.py, test_decision_recall.py, test_admet_decision_recall.py와
실제 fake/SQL full DAG의 test_poc_agents.py다. 첨부 2개 출력은 개발용 고정 fixture이며
실제 재실행 결과가 아니다. #299 실제 화면/export 및 #300 개발 사례 각 3회·모바일
검증은 아직 별도 완료 조건이다. 이 PR만으로 전체 통합/배포 완료를 선언하지 않는다.

## v5.7 입력 전환 준비 (#297)

`policy.py`는 후보별 표적·결합 관문, 대표 후보, 실행 제한 사유와 안전성 기대 상태를
계산한다. 표적과 결합 지지 개수가 가장 많은 후보를 선택하고 동점은 입력 순서를
유지한다. 서로 다른 후보의 지지를 합치지 않으며 미확정 대안만으로 전체 Go를 제한하지
않는다. 표적 방향 stabilize는 미해결이고 후보 없으면 대표 후보는 None이다.

실제 모델 입력의 새 경로는 `target_mode`, `dta_reference_boundary`, `lead_candidate`,
`evidence["dta:<ensembl_id>"]`의 `region_status`, `experimental_binding_support`,
`pubchem_requested`, `pubchem_executed`, `pubchem_execution_issue` 및
`evidence["admet:context"].toxicity_axes`다. 원 ADMET context의 columns/rows와
drugbank_approved_percentile/reference_population은 유지한다. ADMET 자연어 해석과
과거 DTA evidence_assessment/status/recall_trigger는 모델 입력에서 제외한다.
저장된 독성 축을 원 관측으로 검증해 불일치는 decision_toxicity_axes_mismatch로 거부한다.

typed `DecisionMetadata`는 영역 기대값과 stage/cause 제한, 원 오류 코드, baseline/final
안전성 및 positive confirm_channels를 공유한다. positive 채널은 확인 대상을 추가하지만
negative로 기존 축을 해제하지 않는다. recall 실패는 baseline을 유지하고 제한을 추가한다.
부분 해석 실패는 실행 제한과 원 관측의 영역 상태를 분리해 보존한다.

#297에서 준비한 입력/공통 계산을 #298 출력·서버 검증·공개 저장에 연결했다.
아래 v4 이하의 출력 설명은 과거 계약 기록이며 현재 v5.7 생성 명세가 아니다.

저장된 전문 출력의 해석을 종합하며 queue, 도구 호출, 예산 증액과 상태 전이는 소유하지 않는다.

| 진입점 | 역할·입출력 | 오류 |
| :--- | :--- | :--- |
| `DecisionAgent.execute` / agent.py | AgentInput → AgentOutput[DecisionResult], 단일 LLM 요청 | 입력 초과, 모델 장애, 잘못된 JSON/인용 |
| `DecisionAgent._context` / agent.py | 검증된 저장 참조 → 작은 근거 projection | hash/소유권/입력/Target-DTA lineage 불일치 |
| `projection.py` | 검증된 결과의 Target·ADMET·DTA·cardiac 필드 축약과 Decision 입력 payload | live·replay 간 projection drift |
| `dta_lineage.py` | DTA assay snapshot → 저장된 SQL query/evidence 검증 | 조회·근거·입력 hash 불일치 |
| `DecisionAssessment` / agent.py | provisional recall 요청 또는 final verdict, 인용 key, conflicts/gaps | 폐쇄형 schema 검증 실패 |

실행 진입점 → context → 모델 호출 → 인용/누락 정책 검증 → 공통 출력 순서로 읽는다.
인용 key는 source_run_ids로 연결되는 저장 결과의 projection key이며 원 claim UUID를 대체하는
외부 API가 아니다. 공개 결과 API의 Decision DTO에 새 필드와 서버 메타데이터를 확장한다.

go는 추가 연구 우선순위일 뿐 치료 권고가 아니다. 미구성/실패 단계를 누락 근거로 명시하고
불완전한 근거에서 무조건적 go를 거부한다. No-Go를 단순 API 장애나 데이터 없음으로 만들지
않도록 prompt를 제한한다. 의미적 grounding 전체를 기계적 검증으로 보장하는 것은 아니다.

## 과거 출력 계약 (v4 이하)

`decision-report-clarity-v3` 프롬프트는 근거가 충분하면 최종 rationale을 두 문단,
대략 6~8문장의 한국어 연구 판단으로 유도한다. 질환 표적으로서의 적합성과 이
후보물질의 예측 결합 친화도·안전성을 순서대로 구분하고, 이미 산출된 pKd를 미평가
항목으로 되돌리지 않는다. pKd를 Kd로 환산할 때는 `Kd [M] = 10^(-pKd)`를 사용한다.
판단을 바꾸는 신호의 값과 의미를 설명한 뒤 현재 분자의 연구 우선순위로 마무리한다.
후속 확인은 rationale 끝에 나열하지 않고 구조화된 `next_actions`로 분리한다. 모델 간 같은
방향과 신호 강도 차이를 구별하고, 불확실성은
모델 결과의 해석·적용 범위·비교 근거에 집중한다. 동일 endpoint/model/단위/기준
집단이 없는 약물 간 우열과 입력에 없는 임상 아형·효능은
만들지 않는다. 규제·임상 주의사항은 판단 제약으로 적용하되 본문에서 반복하지 않는다.
`conditional_go`에는 결론을 바꿀 구체적 gap이 필요하고, `go`/`no_go`에는 불필요한
gap을 강제하지 않는다. 저장된 context projection은 그대로다.

`decision-adme-toxicity-synthesis-v4`는 새 생성 결과에 `next_actions` 1~3개를 요구한다.
각 항목은 `status=proposed`, 수행하거나 확인할 내용, 사례별 필요 이유, 어떤 관측이 현재
판정을 어느 방향으로 바꾸는지를 분리한다. 기본적으로 Conditional Go는 시스템이 만든 핵심
불확실성과 gap을 해소하는 행동을, Go는 현재 workflow를 넘어 가설을 진전시킬 외부 실험이나
조사를 우선 제안한다. 이는 고정 task taxonomy나 서버 거부 규칙이 아니다. 사례 근거상 더
유용하면 내부 재검토와 외부 결합·세포 기능·노출·독성 검증, 문헌 확인을 함께 제안하거나
범위를 확장할 수 있다. 다만 현재 workflow가 할 수 있는 일과 외부에서 수행할 일을 문장에서
구분하고, 실제 수행 결과나 임상 효능·안전성을 만들지 않는다. No-Go에는 현재 가설의 우선순위
하향, 대안 또는 구체적인 구제 조건을 고려하되 의미 없는 추가 실험을 강제하지 않는다.

독자용 `rationale`, `conflicts`, `gaps`, `next_actions`의 긴 소수는 원 저장 근거와 분리해
소수 둘째 자리까지 표시한다. 0이 아닌 작은 값이 `0.00`이 되는 경우 유효숫자 2자리
과학적 표기로 바꾼다. 예를 들어 `4.811026`은 `4.81`, `0.00004811026`은 `4.8e-5`로
표시한다. 정규화 시 `decision_numeric_display_normalized` 경고를 남기며 evidence projection과
원 전문 결과의 수치는 변경하지 않는다.

공개 Decision DTO에도 `next_actions`를 추가한다. v3 이하의 저장 결과에는 이 필드가 없으므로
읽기 모델의 기본값인 빈 목록으로 계속 조회할 수 있다. 다만 v4 모델이 새로 생성한 결과는
1개 이상이 없으면 `decision_next_actions_missing`으로 거부한다. 프론트 표시 작업은 별도다.

`decision-adme-toxicity-synthesis-v3`에서 도입한 다음 정책도 v4에서 유지한다. 모든 분자에
공통인 예측의 한계(결합
예측만으로 억제·선택성을 증명할 수 없음, 실측 부재)를 판정 사유나 `gaps`에 반복해
넣지 않고 필요할 때 `rationale` 끝에서 한 번만 설명하도록 한다. `gaps`는 확인
결과가 이번 분자의 연구 우선순위를 실제로 바꿀 수 있는 사례별 항목이며,
`conflicts`는 제공된 관측 간 실제 불일치다. 단일 독성 축 우려만으로 독성 기반
`no_go`를 주지 않고, 추가 연구 근거가 남아 있으면 해당 축의 구체적인 확인을
조건으로 제시한다.

독성 기반 `no_go`는 간손상·심장 이온통로·돌연변이원성 중 **서로 다른 두 축
이상**에서 유해한 양성 방향을 지지하고, 각 축의 해당 승인약 참조 집단 내
백분위도 높은 쪽이며, 표적·결합 근거를 함께 보아도 현재 연구 우선순위를
유지하기 어렵다고 해석될 때만 고려한다. 같은 축의 모델 둘을 두 축으로 세지
않으며, 숫자 합산이나 새 임계값을 만들지 않는다. 백분위만으로 양성 방향을
판정하거나 예측을 임상 독성 확정으로 표현하지 않는다. 이는 **LLM 지시문**이며
두 축·양성 방향·백분위·근거 충분성을 서버가 결정적으로 검증한다는 뜻은 아니다.
기존 서버 검증은 JSON 계약, 인용 ID, 누락 전문 단계의 Go 제한 등으로 그대로다.
평가용 [4분류 기준](../../../../docs/evaluation/decision-criteria-v1.md)은 직접적인
부정 근거를 요구하므로, 운영 3분류의 예측 기반 연구 우선순위와 같은 gold
label로 혼합하지 않는다. ADMET-AI의 [공식 설명](https://admet.ai.greenstonebio.com/)에
따르면 분류값은 해당 속성의 모델 예측 확률이고 DrugBank 백분위는 참조 집단 내
위치다. 질환·치료 영역에 맞는 참조 집단과 관측치 해석은 별도 검토가 필요하다.

새 프롬프트의 유료 재실행은 아직 하지 않았다. 대표 사례의 **검토 질문**은
DA-03(결합 모델 간 차이와 독성 축의 구분), DA-12(심장 독성 축의 확인 필요성),
DA-23(표적 방향 근거의 약함/상충), DA-30(상대적으로 낮다고 제시된 독성 신호와
원약물·활성체 불일치)이다. 이들은 예상 판정이나 정답 라벨이 아니다. 입력
패킷상 DA-12는 **Fulvestrant**이며 Camizestrant가 아니다. DA-30은 **Abiraterone
acetate 구조**를 CYP17A1에 입력해 활성체 전환을 모델링하지 않았다. 재실행 전
실제 원 관측의 endpoint 방향·백분위·모델 버전과 비교 가능성을 확인해야 한다.
네 건의 결과가 갈려도 32건의 판정 성능 또는 정책의 타당성을 입증하지 않는다.

근거 ID는 `used_evidence_ids`에만 남긴다. 모델이 독자용 rationale/conflicts/gaps에
`[target:...]` 같은 대괄호 내부 ID를 출력해도 해당 **표기만** 제거하고 구조화된 인용은
유지한다. 정규화가 발생하면 `decision_inline_citations_removed` 경고 코드를 저장한다.
수치·판단 문장이나 일반 대괄호 표현은 바꾸지 않는다. 프롬프트 준수만으로 내부 필드명과
상투적 문장을 기계적으로 모두 탐지할 수는 없으므로 실제 모델 출력은 별도 평가한다.

2026-09-27 사용자 제공 로컬 결과에서는 CDK4 관련 표적 근거, 결합 예측 pKd 4.8246,
hERG 예측 0.98470과 승인 약물 기준 백분위 98.84가 제시됐지만, 본문에 내부 인용
표기와 반복적 비입증 문구가 섞였다. 추가 심장 이온채널 호출은 저장 실패였다. 다음
실행에서는 (1) 핵심 병목의 우선순위 해석, (2) 내부 ID/원시 필드명 없는 문장,
(3) 비교 자료가 없는 임상 우열 주장 배제, (4) 실패한 후속 호출을 한 번만 간결하게
설명하는지를 사람이 확인한다. 이 결과는 프롬프트 변경 전 관측이며 v2의 live 검증이 아니다.

2026-09-28 재실행에서는 pKd 4.81을 보고도 분자의 결합 근거를 사실상 미평가로
취급하고, 결론을 실험 목록으로 끝내는 문제가 확인됐다. v3는 예측 결합값을 이미
완료된 분자 평가로 해석하도록 수정했다. 자동 테스트는 프롬프트 계약을 확인하지만,
실제 생성 문장의 과학적 해석과 길이는 새 분석으로 확인해야 한다.

`decision-adme-toxicity-synthesis-v1`에서는 새 ADMET 결과의 ADME·독성 해석을
별도 필드로 받아 노출·유리형 농도에 대한 조건부 가설과 위해 신호를 구분한다.
지용성·조직 재분포·배설·실제 혈중농도는 제공되지 않으면 주장하지 않는다.
기존 ADMET 결과는 두 필드가 없으므로 저장된 통합 해석을 그대로 사용할 수 있다.

LLM은 서열이나 전체 Target evidence를 받지 않는다. 원 도구 결과와 source run은 SQL에 유지한다.
DTA assay도 전체 행과 긴 assay 설명을 전달하지 않는다. 전체 근거로 중복 제거 전후 건수,
출처·endpoint별 건수, potency criterion 지지/반대 수, 최솟값·중앙값·최댓값과 출처별 방향을
결정적으로 계산한다. 대표 근거는 출처 포함, criterion 양쪽 경계, 출처 간 충돌, 모델-실험 충돌,
분포 경계 순으로 선정하고 선정 이유를 함께 전달한다. 후보 전체에서 최대 15건이며 여러 후보에는
한도를 배분하되 후보별 출처 수만큼은 확보한다. 핵심 레코드는
`source_tool_call_id`와 `source_record_id`로 `dta_assay_queries`/`dta_assay_evidence`에 연결된다.
새 DTA snapshot의 assay 실행 참조는 SQL의 분석/run/SMILES hash/UniProt/provider/outcome 및 근거와
일치해야 한다. 불일치하면 모델을 호출하지 않고 거부한다. #258 이전 snapshot처럼 `assay_runs`가
없는 결과는 replay 호환을 위해 기존 lineage 검증 경로를 유지하되 새 SQL 참조를 만들지는 않는다.
파싱 실패도 응답 사용량을 보존하고 자동 retry하지 않는다. API 장애로 사용량을 모르면 null이다.
Decision의 단일 Responses 호출은 출력(보이지 않는 추론 토큰 포함)을 최대 8,192토큰으로
제한한다. 4,096토큰에 정확히 도달한 미완료 응답은 같은 strict schema로 교정 재호출을 최대
1회 수행한다. 비용이 늘 수 있으므로 배포 후 새 분석 1건의 완료 여부와 실제 사용량을 확인한다.
두 번째 응답도 한도에 도달하면 결과를 성공으로 보정하거나 추가 재호출하지 않는다.

profile의 recall depth가 1이고 baseline ADMET 근거가 있으면 첫 Decision은
`cardiac_ion_channel_evidence`를 요청할 수 있다. 요청에는 조사 목적과 reason만 들어간다.
Orchestrator가 ADMET attempt 2를 실행한 뒤 Decision attempt 2에 성공 관측 또는 고정 실패
사유를 전달한다. 두 번째 Decision에는 recall 권한이 없으며 `final` 판단만 허용한다.
CToxPred2 관측은 저장된 Agent output과 원 tool call의 분석/run 관계를 재검증한다.
결과 API는 ADMET 1번·2번 호출을 `calls`의 순서형 기록으로 투영한다. 기존 화면용
`admet` 필드는 1번 기본 검사만 표시한다.
첫 Decision의 `request_recall`은 최종 verdict로 공개하지 않고, 두 번째 `final` 판단만
결과로 표시한다.

v3는 ADMET 실행 상태와 endpoint 선택 범위를 분리한다. 저장 원본의 `is_partial`은
선택된 요약 범위일 뿐 실행 실패가 아니다. Decision projection에서는 해당 필드를 제외하고
`execution_status`, `selection_coverage`, `missing_endpoints`를 사용한다. 원본 SQL은 보존한다.
허용 인용 ID·필수 전문 단계 prefix·허용 verdict와 필드 길이 제한을 prompt에 명시한다.
ADMET 내부 endpoint/해석의 인용 ID는 Decision의 최상위 projection ID와 다르므로 직접
인용하지 않는다. 서버의 검증 조건은 완화하지 않는다. 오류의 `decision_invalid_output`은
유지하고 `warnings[].code`로 다음 고정 진단을 보존한다(응답 원문/예외 원문은 저장하지 않음).

- `decision_response_incomplete`: 응답이 completed가 아님
- `decision_json_invalid`: JSON 구문 오류
- `decision_schema_invalid`: 필수 필드·자료형·길이·enum 등 schema 위반
- `decision_citation_unknown`: 허용 목록 밖의 인용
- `decision_specialist_citation_missing`: 사용 가능한 전문 단계 중 인용 누락
- `decision_go_restricted`: 근거 불완전 상태에서 무조건적 go
- `decision_recall_not_allowed`: recall depth가 없거나 재판단에서 다시 요청함
- `decision_next_actions_missing`: 새 생성 결과에 구조화된 후속 행동이 없음
- `decision_prose_empty_after_citation_cleanup`: 내부 인용만 제거하면 필수 문장이 비게 됨

기존 v1 실패 기록에는 이 정보가 없어 당시 원인을 소급 확정할 수 없다.
공식 [Structured Outputs 문서](https://developers.openai.com/api/docs/guides/structured-outputs)의
schema 준수와 내용 검증 구분을 참고했다. 현재 gateway의 일반 텍스트 호출은 유지하며,
API-level Structured Outputs 도입이나 자동 재요청은 이번 변경에 포함하지 않는다.

테스트: `uv run --no-env-file pytest tests/test_poc_agents.py tests/test_poc_reasoning.py`.
정책/prompt/projection은 agent.py, 공통 lifecycle은 orchestration에서 수정한다.
관련: [PoC 명세](../../../../docs/features/poc-full-execution.md).
