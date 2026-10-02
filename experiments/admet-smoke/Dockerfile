FROM ghcr.io/astral-sh/uv:0.11.24 AS uv
FROM python:3.12.10-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv
# chemfunc가 초기화 중 RDKit SVG 모듈을 import하므로 headless 추론에서도 X11 공유
# 라이브러리가 필요하다. GUI 패키지 전체가 아닌 동적 링크 의존성만 설치한다.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libexpat1 libgomp1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# uv의 다운로드·압축 해제 캐시는 수백 MB의 PyTorch 복사본을 만들 수 있다. 잠금 파일로
# 재현성을 보장하고 캐시는 이미지 레이어에 남기지 않는다.
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 HOME=/tmp
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/tmp/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY LICENSES /licenses
COPY smoke.py runtime.py ./
RUN /app/.venv/bin/python /app/runtime.py --prepare-assets
USER 65532:65532
ENTRYPOINT ["/app/.venv/bin/python", "/app/smoke.py"]
