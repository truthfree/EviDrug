#!/usr/bin/env bash
# 모델 이미지만 준비한다. LLM 호출, DB 변경, 컨테이너 재시작은 하지 않는다.
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
docker build --platform linux/amd64 -t evidrug-admet-smoke:1.4.0 \
  "$task_root/experiments/admet-smoke"
docker build --platform linux/amd64 -t evidrug-deeppurpose-dta-smoke:0.1.5-bindingdb-dual \
  "$task_root/experiments/deeppurpose-dta-smoke"
docker build --platform linux/amd64 -t evidrug-ctoxpred2-smoke:0.1.0-rf-ssl \
  "$task_root/experiments/ctoxpred2-smoke"
docker build --platform linux/amd64 -f "$task_root/Dockerfile.poc" \
  -t evidrug-worker "$task_root"
