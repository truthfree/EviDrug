# ADMET baseline 선행 검증

## 배경과 사용자 목표

추가 도구와 자율 선택의 효과를 비교하기 전에 실행 가능한 고정 ADMET baseline이 필요하다.
현재 단계는 ADMET-AI 1.4.0의 실행 가능성을 확인하는 backend 실험이며 사용자 분석 API는
변경하지 않는다.

## 범위와 흐름

사용자가 독립 검증 명령을 검토하고 실행 → CPU 모델 로딩 → 공개 aspirin 분자 추론 →
원본 예측과 실행 버전, 모델·참조 데이터 SHA-256 보고서 확인.
비밀 설정, 서비스 연동, 화면, LLM 해석, adaptive 도구 선택과 Decision recall은 제외한다.

## 입력·출력과 오류

입력은 스크립트에 고정된 공개 SMILES다. 출력은 예측 원본 및 endpoint 메타데이터를
포함하는 JSON이다. 누락 endpoint, NaN/무한대, 비수치 응답 또는 실행 실패는 실패로
처리하며 부분 결과를 성공으로 표시하거나 다른 모델로 대체하지 않는다.
API 계약과 화면 상태에는 변경이 없다.

## 보안과 비용

운영 `.env`나 호스트 경로를 컨테이너에 전달하지 않는다. Docker 권한을 Codex에
추가하지 않고 사용자가 검토한 고정 작업을 직접 실행한다. 빌드 다운로드·CPU·메모리·
로컬 이미지 저장 비용만 발생하며 외부 유료 API는 사용하지 않는다.

## 완료 조건

- 독립 잠금 명세 및 검증 로직 unit test 통과
- 2026-09-18 Docker Desktop의 Linux/amd64 CPU 환경에서 ADMET-AI 1.4.0 설치,
  모델 로딩과 aspirin 단일 추론 성공 확인
- 모델·참조 데이터 hash와 전체 예측 출력 확인. 긴 터미널 출력의 복사 누락을 막기 위해
  이후 실행부터 전체 JSON을 Git 제외된 로컬 보고서로 보존
- 실패 시 원인을 기록하고 실제 검증 전 API/worker 연동을 완료로 보고하지 않음

Backend: [#55](https://github.com/truthfree/Dacon2026/issues/55).
Frontend Issue는 이번 범위에 없으며 실행 설명은
[검증 README](../../experiments/admet-smoke/README.md)를 따른다.
검증된 report의 production 정규화 계약은
[ADMET tool adapter](../../backend/src/evidrug_api/admet/README.md)를 따른다.
