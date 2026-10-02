# Decision v5.7 개발용 fixture

2026-10-01 사용자가 제공한 Codex_첨부파일_1001.zip의 시험 입력/기대 출력 각 2개다.
Palbociclib 지정·Abemaciclib 탐색 사례이며 평가용 12건 또는 gold label이 아니다.
출력 파일명에 있는 1회는 첨부 이름이며 이번 PR에서 실제 모델을 호출했다는 뜻이 아니다.

decision_v57_support.py는 입력의 표 형태를 실제 AdmetContext columns/rows 계약으로,
모델 점수를 실제 DtaObservation/SCORE_UNITS JSON으로 재구성해 서버 검증에 사용한다.
프롬프트 hash는 test_decision_v57.py가 원본 LF 계약 d5bd73424fca로 검사한다.
실 모델의 문장 품질·거부율·비용·개발 사례 총 6회 기록은 #300에서 별도로 검증한다.
