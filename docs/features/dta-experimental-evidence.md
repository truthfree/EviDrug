# DTA 실험근거와 조건부 PubChem BioAssay

## 목표

두 DTA 모델의 결합 예측을 실험값으로 오해하지 않고, 동일 compound-target의 비교 가능한
assay 근거·충돌·공백과 함께 Decision Agent에 전달한다.

## 흐름

```text
canonical SMILES + Target의 UniProt accession/서열
→ CNN-CNN·MPNN-CNN 독립 예측
→ 사전 지정 potency criterion으로 모델 판단 영역 비교
→ 판단 영역 차이·중요 공백일 때 후보별 PubChem BioAssay 최대 1회
→ DTA evidence package를 Decision upstream 결과로 저장
```

## 계약

- `potency_criterion`은 분석 시작 요청의 선택 필드이며 endpoint, 최대값, 단위를 가진다.
- 지원 endpoint는 Kd, Ki, IC50이고 단위는 pM, nM, uM, mM, M이다.
- 현재 모델의 `predicted_pkd`는 Kd criterion과만 직접 비교한다.
- criterion 부재, endpoint 불일치와 정성 assay는 정량 충돌 판정에서 제외한다.
- 원 레코드 ID, assay 설명, DOI/PMID를 보존하고 같은 원 실험의 중복 제거는 provider가
  안정적인 출처 식별자를 제공한 뒤 적용한다.
- PubChem 조회 요청·실행·공백 해소는 서로 다른 상태로 저장한다.

## 실패와 비용

provider timeout, rate limit, 잘못된 응답과 결과 없음은 서로 구분해야 한다. 한 조회가 실패해도
이미 확보한 모델 및 다른 assay 관측은 보존한다. 숨은 retry와 다른 provider로의 자동 fallback은
허용하지 않는다. 외부 provider 활성화 전 rate limit, 이용 조건, 전송 입력과 실제 응답 schema를
공식 자료 및 승인 환경 smoke로 확인한다.

## 현재 구현 경계

typed 입력·결과, 관계 판정, 분석별 criterion 저장과 DTA Agent의 조건부 실행이 구현돼 있다.
#249에서 PubChem HTTP provider와 공통 admission binding을 연결했다. 활성화는
`EVIDRUG_DTA_ASSAY_PROVIDERS_ENABLED=true`인 PoC worker로 제한하며 기본값은 false다.
프론트엔드 입력·표시와 구조·서열 유사도는 별도 Issue다.

조회 결과는 `dta_assay_queries`와 `dta_assay_evidence`에 정규화해 저장한다. 전자는 provider
version·입력 식별자·성공/무결과/실패·외부 요청 수·시간을, 후자는 endpoint와 단위가 보존된
개별 근거를 가진다. SMILES는 분석 테이블을 원본으로 삼고 조회에는 hash만 저장하며 단백질 서열과
원본 HTTP payload는 저장하지 않는다. 동일 tool call은 저장 결과를 재사용한다.

공식 API 확인일은 2026-09-30이다.

- PubChem: PUG REST SMILES→CID와 compound assay summary. target accession이 정확히 같은
  행만 사용한다.

각 서비스의 공개 데이터와 endpoint는 변경될 수 있다. 운영 활성화 전 승인 환경에서 응답
schema, 지연, rate limit과 이용 조건을 다시 확인하며 replay miss를 live 호출로 바꾸지 않는다.
