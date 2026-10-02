FROM python:3.9.23-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 HOME=/tmp
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libarchive-tools libgomp1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ctoxpred2
RUN python -m venv .venv
COPY requirements.txt ./requirements.txt
RUN .venv/bin/pip install --no-cache-dir -r requirements.txt

# Source and archive bytes are pinned before extraction; no model file is kept
# in this Git repository or accepted from an Agent-controlled path.
ADD --checksum=sha256:f91388b055bfbb21c74437829a534d412b86a323942c278d4e9981be8cfc3209 \
    https://codeload.github.com/gadsbyfly/PyBioMed/tar.gz/45440d8a70b2aa2818762ceadb499dd3a1df90bc \
    /tmp/pybiomed.tar.gz
ADD --checksum=sha256:07cbbf6fa29de3c89629f3d6f9c1b8e1cfc04656e3ce4f82c3233a852293758d \
    https://raw.githubusercontent.com/issararab/CToxPred2/2a31aa119e27b6b69a5588d18a01f2a27fef4524/CToxPred2/models/decriptors_preprocessing.rar \
    /tmp/decriptors_preprocessing.rar
ADD --checksum=sha256:0e2a88c8997d04c674e3b58109af8bdd2f797c1c62c25f180ff0bfd3805b7de7 \
    https://raw.githubusercontent.com/issararab/CToxPred2/2a31aa119e27b6b69a5588d18a01f2a27fef4524/CToxPred2/models/random_forest.rar \
    /tmp/random_forest.rar
ADD --checksum=sha256:eaa7573d764405c1983c7211f281c86c48674eeaf9dc4a721d1b223e24db5297 \
    https://raw.githubusercontent.com/issararab/CToxPred2/2a31aa119e27b6b69a5588d18a01f2a27fef4524/LICENSE \
    /opt/ctoxpred2/licenses/CToxPred2-LICENSE

RUN mkdir -p /tmp/pybiomed /tmp/extracted /opt/ctoxpred2/artifacts \
    && tar -xzf /tmp/pybiomed.tar.gz -C /tmp/pybiomed --strip-components=1 \
    && cp -a /tmp/pybiomed/PyBioMed .venv/lib/python3.9/site-packages/PyBioMed \
    && cp /tmp/pybiomed/LICENSE.txt licenses/PyBioMed-LICENSE.txt \
    && bsdtar -xf /tmp/decriptors_preprocessing.rar -C /tmp/extracted \
    && bsdtar -xf /tmp/random_forest.rar -C /tmp/extracted \
    && for model_path in \
       decriptors_preprocessing/global_preprocessing_pipeline.sav \
       random_forest/hERG/_ssl_herg_model.joblib \
       random_forest/Nav1.5/_ssl_nav_model.joblib \
       random_forest/Cav1.2/_ssl_cav_model.joblib; do \
         install -D -m 0444 "/tmp/extracted/$model_path" "/opt/ctoxpred2/artifacts/$model_path"; \
       done \
    && rm -rf /tmp/pybiomed /tmp/extracted /tmp/pybiomed.tar.gz \
              /tmp/decriptors_preprocessing.rar /tmp/random_forest.rar

COPY runtime.py benchmark.py ./
RUN .venv/bin/python runtime.py --artifact-root /opt/ctoxpred2/artifacts --verify-only
USER 65532:65532
ENTRYPOINT ["/opt/ctoxpred2/.venv/bin/python", "/opt/ctoxpred2/runtime.py", "--artifact-root", "/opt/ctoxpred2/artifacts"]
