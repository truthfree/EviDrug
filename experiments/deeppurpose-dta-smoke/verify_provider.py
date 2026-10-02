"""사용자가 실행하는 실제 Docker provider → SQLite 통합 검증. 운영 DB는 사용하지 않는다."""

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.database import Base
from evidrug_api.dta.adapter import DtaToolAdapter
from evidrug_api.dta.contracts import DtaArguments
from evidrug_api.dta.deeppurpose import MPNN_CNN_BINDINGDB_MODEL, DeepPurposeProvider
from evidrug_api.dta.repository import DtaRepository
from evidrug_api.dta.service import DtaExecutionService


class MetricsCollector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.metrics = []

    def emit(self, record):
        self.metrics.append(record.dta_runtime)


async def verify(docker, container_name, profile):
    memory, cpus = ("512m", "0.1") if profile == "free" else ("2g", "1")
    provider = DeepPurposeProvider(
        (
            docker,
            "run",
            "-i",
            "--name",
            container_name,
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--memory",
            memory,
            "--cpus",
            cpus,
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",
            "--entrypoint",
            "/app/.venv/bin/python",
            "evidrug-deeppurpose-dta-smoke:0.1.5-bindingdb-dual",
            "/app/runtime.py",
            "--model",
            MPNN_CNN_BINDINGDB_MODEL.model_id,
        ),
        MPNN_CNN_BINDINGDB_MODEL,
    )
    collector = MetricsCollector()
    logger = logging.getLogger("evidrug_api.dta.deeppurpose")
    logger.addHandler(collector)
    logger.setLevel(logging.INFO)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            analysis = AnalysisRecord(
                session_fingerprint="0" * 64,
                idempotency_key=str(uuid4()),
                disease_id="synthetic",
                disease_name="Protocol validation",
                target_mode=TargetMode.DISCOVER,
                original_smiles="CCO",
                canonical_smiles="CCO",
            )
            session.add(analysis)
            await session.commit()
            repository = DtaRepository(session)
            service = DtaExecutionService(
                DtaToolAdapter(provider, timeout_seconds=300),
                repository,
                tool_id="dta_mpnn_cnn_bindingdb",
            )
            # 합성 입력: 분자·서열 전달 경로 확인용이며 결합 성능 평가가 아니다.
            arguments = DtaArguments(
                canonical_smiles="CCO", target_sequence="ACDEFGHIKLMNPQRSTVWY"
            )
            results = []
            for _ in range(2):
                ids = dict(
                    tool_call_id=uuid4(),
                    request_id=uuid4(),
                    analysis_id=analysis.id,
                    run_id=uuid4(),
                )
                result = await service.execute(**ids, arguments=arguments)
                if result.status != "succeeded":
                    raise RuntimeError(
                        f"Provider failed: {result.error_code}; see container state"
                    )
                assert await repository.load(ids["tool_call_id"]) == result
                assert await service.execute(**ids, arguments=arguments) == result
                results.append(result.model_dump(mode="json"))
            assert len(collector.metrics) == 2
            assert collector.metrics[0]["cold_start"] is True
            assert collector.metrics[1]["cold_start"] is False
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "profile": profile,
                        "sql_roundtrip": True,
                        "results": results,
                        "runtime": collector.metrics,
                    },
                    indent=2,
                    allow_nan=False,
                )
            )
    finally:
        await provider.aclose()
        await engine.dispose()
        logger.removeHandler(collector)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("free", "1c-2g"), default="1c-2g")
    args = parser.parse_args()
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker CLI is required")
    container_name = "evidrug-dta-provider-check-" + uuid4().hex
    try:
        asyncio.run(verify(docker, container_name, args.profile))
    finally:
        subprocess.run(
            [
                docker,
                "inspect",
                "--format",
                "oom_killed={{.State.OOMKilled}} exit_code={{.State.ExitCode}}",
                container_name,
            ],
            stdout=sys.stderr,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        # client 프로세스 종료만으로 컨테이너 종료를 보장하지 않으므로 생성한 이름만 정리한다.
        subprocess.run(
            [docker, "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


if __name__ == "__main__":
    main()
