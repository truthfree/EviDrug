FROM ghcr.io/astral-sh/uv:0.11.24 AS uv
FROM python:3.10.18-slim-bookworm

COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libexpat1 libgomp1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 MPLCONFIGDIR=/tmp/matplotlib HOME=/tmp

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/tmp/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY LICENSES /licenses

ADD --checksum=sha256:a912300c004954d3b8a32d8ca7cec1ebfe021bfa11dbb7f1e653ebe00c124094 \
    https://files.pythonhosted.org/packages/fb/45/cb1eaa6cf8bb7f9595372fc66232e10b70c5dfc3191a3effc35c3e00d947/DeepPurpose-0.1.5.tar.gz \
    /tmp/DeepPurpose-0.1.5.tar.gz
RUN --mount=type=cache,target=/tmp/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-deps --no-build-isolation \
    /tmp/DeepPurpose-0.1.5.tar.gz \
    && rm -f /tmp/DeepPurpose-0.1.5.tar.gz

ADD --checksum=sha256:1f5c62863303d5057566b3b29be24e31cc138b361dfb8b085b53c8169d8c6829 \
    https://dataverse.harvard.edu/api/access/datafile/4159715 \
    /tmp/model_cnn_cnn_bindingdb.zip
COPY prepare_model.py /tmp/prepare_model.py
RUN /app/.venv/bin/python /tmp/prepare_model.py \
    /tmp/model_cnn_cnn_bindingdb.zip /opt/deeppurpose model_cnn_cnn_bindingdb \
    && rm -f /tmp/model_cnn_cnn_bindingdb.zip \
    && mkdir -p /app/result

ADD --checksum=sha256:655119c3896a773a8ccf0e711ad263a3bfbcd0bab7ea5085cccb12e13908bc0c \
    https://dataverse.harvard.edu/api/access/datafile/4204178?format=original \
    /tmp/model_mpnn_cnn_bindingdb.zip
RUN /app/.venv/bin/python /tmp/prepare_model.py \
    /tmp/model_mpnn_cnn_bindingdb.zip /opt/deeppurpose-mpnn model_MPNN_CNN \
    && mv /opt/deeppurpose-mpnn/model_MPNN_CNN /opt/deeppurpose/model_mpnn_cnn_bindingdb \
    && rmdir /opt/deeppurpose-mpnn \
    && rm -f /tmp/model_mpnn_cnn_bindingdb.zip /tmp/prepare_model.py

COPY smoke.py runtime.py wget.py ./
COPY lifelines ./lifelines
USER 65532:65532
ENTRYPOINT ["/app/.venv/bin/python", "/app/smoke.py"]
