# 저장소 안전 검사

## DTA 단독 진단

분석이 끝나 worker가 유휴 상태일 때 저장소 루트에서 실행한다. 실행 중인 PoC worker의
기존 DTA 입력이 정확히 하나인 분석만 지원한다.

```sh
bash scripts/diagnose-dta.sh ANALYSIS_ID
```

재빌드 없이 저장된 입력을 읽고 같은 worker 환경에서 DeepPurpose 추론 한 번만 실행한다.
LLM·외부 API 호출, SQL 쓰기, 분석 생성·재시작은 없다. 모델 자식 프로세스에는 API key를
전달하지 않는다. 최대 300초 후 종료하며 오류 원문 대신 예외 종류·파일명·행 번호만 출력한다.
실행 중인 분석과 동시에 사용하면 worker의 2GB 메모리 제한을 공유하므로 피해야 한다.
성공은 모델의 단독 실행 가능성만 뜻하며 provider protocol·전체 분석 성공이나 정확도 검증은 아니다.

## PoC 분석 기록 확인

이슈 #126의 통합 검증에서는 분석이 끝난 뒤 작업자가 다음 명령을 직접 실행한다.

```sh
bash scripts/report-poc-analysis.sh ANALYSIS_ID
```

기존 worker와 DB에 연결해 분석 한 건의 단계 상태, Agent 버전·토큰·안전한 진단 수치,
원 ADMET 예측 수, DTA 관측값·단위, 도구 실행과 trace를 `SELECT`로만 조회한다.
Docker 서비스를 시작·교체하거나 모델·LLM을 호출하지 않는다. 출력에 접근 코드·DB 연결 문자열,
SMILES·단백질 서열, 프롬프트·해석 원문과 잘못된 인용 ID는 포함하지 않는다.
같은 분석 ID의 화면 상태·저장 결과 카드와 대조하고, 실행 기록을 완료로 판단하기 전에
프롬프트 버전 `poc-specialist-v2`를 확인한다. 이 스크립트는 인증된 API·실브라우저 검증을
대체하지 않는다.
이 스크립트는 운영 결과를 저장하지 않는 수동 진단이며, 정상 실행은 공통 runner를 사용한다.

## 빌드와 비밀정보 검사

`build-poc.sh`는 잠긴 ADMET/DeepPurpose 이미지와 PoC worker 이미지를 빌드한다.
API key나 DB를 읽거나 LLM을 호출하지 않는다. 이후 Compose 실행은
[PoC 전체 실행 안내](../docs/features/poc-full-execution.md)를 따른다.

`check_secrets.py`는 Git index/이력의 비밀 설정 파일 경로를 먼저 확인하고 Gitleaks로
내용을 검사한다. 실행 중인 애플리케이션이나 로컬 `.env`를 읽지 않는다.

| 항목 | 내용 |
| :--- | :--- |
| 이름 | `check(mode)` |
| 위치 | `check_secrets.py` |
| 역할 | 커밋 전 및 CI 비밀정보 유출 방지 |
| 입력·출력 | `staged` 또는 `history` / 통과 0, 탐지 1, 실행 오류 2 |
| 오류 | scanner 누락/실행 실패/Git 오류 시 실패 종료 |

진입점은 `.githooks/pre-commit`과 `.github/workflows/secret-scan.yml`이다.
경로 정책 → Gitleaks → 비밀값 없는 결과 메시지 순으로 실행한다.
`test_check_secrets.py`는 임시 저장소와 합성 키로 동작을 확인한다.
검사 범위는 `.gitleaks.toml`, 경로 정책은 `forbidden_path()`에서 수정한다.

설치·테스트·오류 대응은 [기여 안내](../CONTRIBUTING.md)를 따른다.
