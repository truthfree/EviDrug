# 저장 결과 기반 Agent 단독 실행

## 배경과 범위

프롬프트·Agent 변경 검증 시 전체 분석을 반복하지 않고, 기준 분석과 run을 지정해 한 Agent만
다시 실행한다. 이전 결과는 덮어쓰지 않으며 새로운 개발 분석 ID와 run ID를 만든다.
공개 API·프론트엔드 기능이 아닌 **DB 접근 권한이 있는 개발자용 CLI**다.

| 모드 | 이번에 하는 일 | 외부 호출 |
| :--- | :--- | :--- |
| `live_agent` | 선택한 Agent를 새로 실행하고 필요한 upstream 출력은 저장 결과로 제공 | 선택한 Agent 내부에서만 허용 |
| `reuse_output` | 완료된 출력을 그대로 복사하고 원본 참조·재사용 경고 기록 | provider·LLM 모두 0회 |
| `target_from_snapshot` | 당시 전체 후보 근거로 Target의 LLM만 재호출 | **아직 미구현**, snapshot 누락 오류로 거부 |
| `continue_dta_decision` | 완료 Target·ADMET 결과를 읽고 DTA→Decision만 새로 실행 | DTA 모델·DTA 해석·Decision만 허용 |

일반 단독 `live_agent`/`reuse_output` CLI는 **Target Hypothesis만** 허용한다.
#110에서 별도 `continue_dta_decision` 경로에 전문 typed parser와 실제 DTA/Decision handler를
연결했다. 나머지 Agent의 임의 단독 실행은 여전히 `replay_agent_not_configured`로 거부한다.
단독 개발 분석의 `completed`는 해당 Agent만 완료했다는
뜻이며, 최종 약물 판정 완료가 아니다. 일반 UI 세션의 분석 목록에는 섞지 않는다.

Target의 `live_agent` 모드는 Target을 새로 호출하므로 해당 단계의 토큰을 줄여주지는
않는다. 동일 출력을 보기만 할 때는 기존 DB를 조회하면 되고, 새 실행 흐름 검증은 `reuse_output`,
프롬프트 변경 검증은 `live_agent`를 사용한다. LLM 입력 근거를 고정하는 기능은 #106이다.

자동 cache, 최신 성공 run 자동 선택, provider 장애 fallback, 공개 replay API와 UI는 제외한다.

## Target·ADMET 재사용 후 DTA·Decision 이어 실행 (#110)

**화면의 새 분석은 여전히 전체 live 실행이다.** 이 경로는 개발 CLI의 명시적 계획에만 적용된다.
원본 Target/ADMET의 LLM 해석까지 그대로 참조하며 새 Target/ADMET run이나 도구 관측을
복제하지 않는다. 새로운 개발 분석에는 DTA와 Decision 두 단계만 생성한다.
Target/ADMET 추가 토큰·외부 API·모델 호출은 0이며 새 비용은 DTA 해석과 Decision에서 발생한다.
예측은 원래 계획의 tool/timeout/token 한도를 넘지 않는다. shortlist가 tool 예산을 넘으면
기존 DTA 계약대로 일부 후보가 skip될 수 있다.

원본 분석이 종료된 상태에서 완료된 Target·ADMET run 두 개와 비교 기준 Decision run을
명시한다. Decision은 실패한 run이어도 된다. 두 원본의 완료 상태·원본 분석 소속·동일 입력·
JSON/hash·metadata·typed schema를 prepare와 각 실행 단계에서 검증한다.
DB 변경은 새로운 개발 분석/계획/실행 기록뿐이며 원본 분석과 과거 비용은 변경하지 않는다.

1. 모델 이미지가 준비된 환경에서 worker만 새 코드로 교체한다. **다른 분석이 실행 중이지
   않을 때** 사용한다. CLI와 Celery는 별도 프로세스이므로 컨테이너 메모리를 공유한다.

```sh
docker compose -f compose.yaml -f compose.poc.yaml up -d --build --no-deps worker
```

2. 무료 준비. `BASE`, `DECISION_RUN`, `TARGET_RUN`, `ADMET_RUN`을 실제 UUID로 바꾼다.

```sh
docker compose exec worker python -m evidrug_api.orchestration.replay_cli prepare \
  --base-analysis-id BASE --source-run-id DECISION_RUN \
  --agent decision --mode continue_dta_decision \
  --upstream-run-id TARGET_RUN --upstream-run-id ADMET_RUN
```

3. 반환된 새 `analysis_id`로 실행/조회한다. **run은 유료 LLM 호출이 최대 두 번 발생하며**
   `--allow-live`가 없으면 client를 만들기 전에 거부한다. prepare/show는 무료다.

```sh
docker compose exec worker python -m evidrug_api.orchestration.replay_cli run \
  --analysis-id NEW_ANALYSIS_UUID --allow-live
docker compose exec worker python -m evidrug_api.orchestration.replay_cli show \
  --analysis-id NEW_ANALYSIS_UUID
```

`new_runs[].new_usage`만 이번 비용이며 `reused_runs[].metadata.usage`는 과거 비용이다.
Target/ADMET은 `uncalled_agents`에 표시된다. 오류로 사용량을 확보하지 못하면 null이지 0이 아니다.
DTA 전체 실패 시 Decision을 `skipped_due_to_failed_dta`로 생략하며 자동 재시도하지 않는다.
동일 ID의 run 재전달은 공통 claim/lease가 차단한다. 재시도는 새 prepare가 필요하다.
모델·prompt·endpoint·설정 버전이 바뀌면 실행 전에 거부하므로 새 prepare를 수행한다.

일반 분석의 교차 분석 참조는 계속 차단한다. 이 모드에서만 확정 manifest에 명시된
Target/ADMET run·hash를 읽고, 새 DTA는 동일 개발 분석에서 읽는다. 원본 Target을 참조하는
DTA lineage와 ADMET 원본 tool/run ID는 그대로 유지한다. 저장 입력을 추측하거나 live
조회로 보충하지 않는다. 기존 `agent_replays` JSON 계약을 확장하므로 새 migration은 없다.
새 경로는 `continuation.py` → `service.py:run_continuation` → 기존 runner/전문 Agent,
교차 분석 허용 경계는 `upstream.py`에서 확인한다.

2026-09-22 사용자 실실행: `b34cc7a2-a6bd-404b-ae22-f65207687217`은 completed.
Target/ADMET는 호출하지 않았고 새 DTA 558 + Decision 3,090 = 3,648토큰을 사용했다.
원본 Target 2,523 + ADMET 2,051토큰은 과거 사용량이며 이번 비용이 아니다.
이 결과는 분리 전 Decision v2의 연결 검증이다. 이후 사용자 실행
`2d7348ff-b059-41f2-b497-58a05bf4598c`에서는 Decision v3가 ADMET 완료와 선택 범위를
구분했다. DTA 예측은 성공했으나 해석 검증이 거부되어 전체 partial_failure였고,
Decision은 completed였다. 새 토큰은 DTA 620 + Decision 3,015 = 3,635이다.
후속 검증에서 `d721280c-fb79-40c0-bfba-94379739e159` continuation이 completed였고,
새 DTA 574 + Decision 3,346 = 3,920토큰을 사용했다. 실제 실행 버전은 DTA v1이었다.
이후 일반 UI 실행 `086220de-1891-41c8-b6d8-e03865f8175a`에서 DTA v2가 확인됐고
미허용 인용으로 해석이 거부됐다. 최신 상태는 [현황](../project-status.md)을 참고한다.
버전이 바뀐 이전 미실행 계획은 새 prepare가 필요하다.
분리 후 backend 전체 282 passed / 3 skipped, Ruff/format/mypy 통과.
#114·#109 병합 후 PR 준비 시 전체 286 passed / 3 skipped 및 동일 정적 검사 통과.
원본 불변, 두 단계만 호출, 동일 ID 재전달, DTA 실패 시 Decision 생략, 취소,
출처/해시/metadata 거부와 무료 prepare/show·live 동의 경계를 fake provider/client로 검증했다.

## 사용 순서

### 1. 이미지와 migration 갱신

프로젝트 루트에서 실행한다. 기존 DB volume을 삭제할 필요는 없다.

```sh
docker compose up -d --build
```

`migrate`가 `0007_agent_replay`까지 적용한 뒤 API와 worker가 시작된다. 실패하면 실행을 멈추고
migrate 로그부터 확인한다. CLI는 컨테이너에 이미 주입된 환경변수를 사용한다. 키나 DB 비밀번호를
명령에 적지 않고, CLI 자체는 `.env`를 읽지 않는다.

### 2. 원본 ID 확인

DBeaver의 `agent_runs`에서 `analysis_id`, `run_id`, `agent_name`, `status`를 확인한다.
`reuse_output`과 upstream 재사용은 `completed`만 허용한다. 전체 분석이 `failed`여도 선택한
Target run이 성공했고 분석이 종료됐다면 사용할 수 있다. `live_agent`는 실패한 선택 단계의
새 실행도 허용하지만, 입력 snapshot 검증은 생략하지 않는다.

### 3. 무료 재사용으로 먼저 확인

`BASE_ANALYSIS_UUID`와 `SOURCE_TARGET_RUN_UUID`를 원본 값으로 바꾼다.

```sh
docker compose exec api python -m evidrug_api.orchestration.replay_cli prepare \
  --base-analysis-id BASE_ANALYSIS_UUID \
  --source-run-id SOURCE_TARGET_RUN_UUID \
  --agent target_hypothesis \
  --mode reuse_output
```

출력의 새로운 `analysis_id`를 다음 명령에 사용한다.

```sh
docker compose exec api python -m evidrug_api.orchestration.replay_cli run \
  --analysis-id NEW_ANALYSIS_UUID

docker compose exec api python -m evidrug_api.orchestration.replay_cli show \
  --analysis-id NEW_ANALYSIS_UUID
```

정상 결과는 `status=completed`, `new_usage.token_usage.total_tokens=0`,
`new_usage.external_requests=0`이다. `original_source_usage`는 원본의 과거 비용이다.
`new_run_id`로 `agent_runs`를 보면 `parent_run_id`가 원본을 가리키며 원본 JSON은 그대로다.
복사된 출력에는 `stored_output_reused` 경고와 새 producer run ID가 기록된다.

### 4. 프롬프트 변경을 실제로 확인

`prepare`의 모드를 `live_agent`로 바꾸어 **별도의** 새 실행 계획을 만든다.
`prepare`는 유료 호출을 하지 않는다. 실행 시 명시적으로 `--allow-live`를 넣는다.

```sh
docker compose exec api python -m evidrug_api.orchestration.replay_cli run \
  --analysis-id NEW_LIVE_ANALYSIS_UUID \
  --allow-live
```

이 경우 Target의 Open Targets·UniProt·LLM은 실제로 호출하고 ADMET·DTA·Decision은 호출하지
않는다. 이번 실행 토큰은 `new_usage`, 기준 실행 토큰은 `original_source_usage`에서 별도로 본다.
실패·timeout·취소로 usage를 얻지 못했다면 `new_usage=null`이며 **무료였다는 뜻이 아니다**.
원격 요청의 취소가 보장되지 않으므로 응답을 받지 못해도 과금될 수 있다.

`run`을 같은 ID로 반복해도 완료된 실행은 다시 호출하지 않는다. 실행 중에는
`replay_in_progress`로 거부한다. 재시도하려면 새 계획 ID를 만든다. `prepare` 자체의 중복 제출을
막으려면 `--analysis-id NEW_UUID`를 지정한다. 같은 ID와 같은 계획만 멱등 처리하며, 다른 ID의
동일 입력은 자동 cache hit가 아니다.

CLI 종료 코드는 성공/조회 `0`, Agent 실행 실패 `1`, 검증·설정·DB·중복 실행 오류 `2`다.
준비된 계획의 prompt/model/limit/provider endpoint hash가 실행 설정과 달라지면 거부하므로,
빌드나 설정을 바꿨다면 새 `prepare`부터 진행한다.

## 저장 및 검증 계약

- `agent_replays`: 새 analysis ID, 원본 analysis/run ID, 입력·upstream hash, 원래 metadata/usage,
  실행 설정 버전과 manifest JSON/hash. 원본 분석은 변경하지 않는다.
- `agent_runs.input_json`: 이번 버전부터 공통 AgentInput을 hash와 함께 보존한다. 키는 포함하지
  않지만 연구 입력이므로 DB 접근 정책의 보호 대상이다.
- `agent_runs.parent_run_id`: 새 실행의 비교 기준 run. upstream은 `input_json`과 manifest에서
  원래 ID와 output hash로 참조하며 새 실행처럼 복제하지 않는다.
- 새 실행의 실제 모델·prompt·policy·provider 데이터 버전과 사용량은 기존
  `output_json.execution_metadata`와 `execution_metadata_json`에 저장한다.
- live provider 데이터 릴리스는 준비 시 고정할 수 없다. 실제 조회한 릴리스는 새 output에 남고,
  원본과 같다고 주장하지 않는다. endpoint 원문 대신 hash를 저장한다.

원본 분석이 종료됐는지, run의 원본 분석 소속, 입력 snapshot/hash, output의 성공 상태·hash,
등록된 typed schema, output ID, metadata를 검증한다. upstream은 같은 기준 분석에서만 선택한다.
DTA 단독 replay에는 Target 1개, Decision 단독 replay에는 Target·ADMET·DTA 3개를 요구하며 DTA가 참조한 Target도
선택된 Target과 같아야 한다. 부분 실패 출력을 허용하는 일반 DAG와 달리 개발 replay는 엄격하게
완료된 근거만 재사용한다.

이전 run에는 `input_json`이 없다. upstream이 없는 Target/ADMET은 원래 분석 입력·실행 profile로
AgentInput을 재구성하고 **기존 input hash와 정확히 일치할 때만** 승인한다. 이전 DTA/Decision의
누락된 upstream 참조는 추측하지 않고 `replay_input_snapshot_missing`으로 거부한다.

계획 저장 시와 실행 직전에 다시 검사한다. source 변경, 다른 입력, 누락 snapshot, 알 수 없는
schema를 live 조회로 보충하지 않는다. `ReplayExecutor`가 호출을 제한하고
`AnalysisOrchestrator.run_single`이 기존 claim·timeout·중복 방지·취소·lease·원자 저장을 재사용한다.
단독 분석을 일반 fixed DAG runner에 잘못 전달하면 실행 전에 거부한다.

### 주요 오류

| 코드 | 대응 |
| :--- | :--- |
| `replay_output_not_completed` | 완료된 원본/upstream run 선택 |
| `replay_origin_mismatch`, `replay_input_mismatch` | 같은 원본 분석의 run과 입력인지 확인 |
| `replay_upstream_set_mismatch`, `replay_upstream_lineage_mismatch` | 필요한 모든 upstream과 파생 관계 확인 |
| `replay_schema_mismatch`, `replay_schema_not_registered` | 해당 schema의 검증 adapter가 필요 |
| `replay_*_hash_mismatch`, `replay_source_changed` | 원본/계획 변경 여부 조사, 자동 재조회 금지 |
| `replay_input_snapshot_missing` | 재구성 불가능한 과거 run, 새 정상 실행에서 snapshot 생성 필요 |
| `replay_evidence_snapshot_missing` | Target LLM-only 모드는 #106 이후 지원 |
| `replay_runtime_version_mismatch` | 현재 빌드/설정으로 새 계획 준비 |
| `replay_agent_not_configured` | 실제 Agent 및 typed parser 연결 후 사용 |
| `replay_development_only` | production에서는 사용 금지 |

## Target 근거 snapshot 후속 설계

`AgentInput` 저장과 **외부 근거 snapshot**은 다르다. 현재 입력에는 질환·타깃·SMILES·upstream
참조만 있고, LLM이 본 전체 후보 자료는 없다. shortlist 결과에서 후보 집합을 역으로 생성하면
원래 판단 조건을 바꾸게 된다.

#106에서 다음을 구현한다.

1. 후보 조회 직후 전체 `TargetCandidateBatch`를 별도 typed snapshot으로 저장: 탈락 후보,
   function description, tractability, 인과 근거, source version/time, 조회 limit.
2. case·candidate fingerprint, snapshot schema/hash, 원본 run과 연결.
3. UniProt 자료의 보존 범위 결정. 순위가 달라져 이전 shortlist 밖 후보를 선택할 때 단백질 자료가
   없으면 명시적 replay miss로 처리하거나, 별도로 승인된 snapshot 생성 단계에서 미리 수집.
4. 저장 provider와 live reasoner를 조합하되 Open Targets·UniProt 호출은 0회로 검증.
5. snapshot은 원본 artifact이며 LLM prompt에 그대로 붙이지 않는다. 기존 압축 projection을 사용하고
   sequence는 reasoner 입력에 추가하지 않는다.

## 코드 읽는 순서와 검증

`replay_cli.py` → `replay_contracts.py` → `replay_store.py` → `replay.py` →
`service.py:run_single` → 기존 `repository.py` 순서로 읽는다. 새 Agent를 연결할 때는 CLI의
handler/parser 등록과 도메인 입력 검증을 확장한다. 공통 orchestration에 과학 판단을 넣지 않는다.

```sh
cd backend
uv run --no-env-file pytest tests/test_agent_replay.py tests/test_agent_replay_migration.py
uv run --no-env-file pytest tests/test_orchestration.py
```

외부 호출 없는 fake 테스트로 단일 호출·0회 재사용·원본 보존·입력/출처/schema 거부·동시 전달·취소·
lease 만료와 늦은 저장 차단을 검증한다. SQLite migration 테스트는 0006의 원본 보존, 0007 적용,
downgrade 후 재적용을 확인한다. PostgreSQL/컨테이너 통합 검증은 별도로 수행한다.

2026-09-21 개발 PostgreSQL 검증에서는 `0007_agent_replay` 적용을 확인한 뒤 기존 Target의
전체 결과(LLM 판단 포함)를 재사용했다. 원본 보존·결과 일치·새 토큰 0·외부 요청 0·같은 ID의
재전달 시 run 1개 유지를 확인했다. 원본의 8,904토큰은 과거 사용량으로만 표시됐다.
검증은 로컬 코드에서 DB에 직접 연결해 수행했으며, Docker 내부 CLI 실행과 유료 live 호출은
수행하지 않았다.

관련: #104,
#106,
후속 개선 목록.
