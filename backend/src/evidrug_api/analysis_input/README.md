# 분석 입력 검증

확정된 질환, 타깃 입력 방식과 화합물의 SMILES를 분석 실행 전에 검증하는 모듈이다.
이 모듈은 입력을 정리할 뿐 타깃 검색, 에이전트 호출이나 분석 작업 생성은 수행하지 않는다.

## API

`POST /api/v1/analysis-inputs/validate`

인증된 사용자가 다음 값을 보낸다.

- `disease_id`, `disease_name`: 앞 단계에서 사용자가 확인한 Open Targets 질환
- `target_mode`: `discover` 또는 `specified`
- `target_name`: `specified`에서만 필요한 사용자의 타깃 입력
- `smiles`: 분석할 화합물의 SMILES

`discover`는 타깃을 지정하지 않고 질환을 기반으로 후보를 탐색한다. `specified`는 사용자가
입력한 타깃을 후속 단계에서 검색하고 확인한다. 아직 표준 타깃 ID를 확정하지 않으므로
`target_name`은 사용자가 입력한 문자열 그대로 보존한다.

SMILES는 RDKit으로 실제 분자 파싱을 수행한다. 유효한 입력은 원본과 canonical SMILES를
함께 반환하고, 파싱할 수 없는 입력은 `invalid_smiles` 오류로 거부한다.

## 주요 코드

- `models.py`: 요청·응답 구조와 타깃 모드 조합 규칙
- `smiles.py`: 교체 가능한 파서 계약과 RDKit 구현
- `dependencies.py`: FastAPI가 애플리케이션의 파서를 가져오는 경계
- `router.py`: 인증, SMILES 오류 변환과 응답 구성

## 후속 범위

- Open Targets 또는 UniProt 기반 타깃 후보 검색과 사용자 확인
- Hypothesis Agent 입력 구성
- 분석 작업 생성과 결과 저장
