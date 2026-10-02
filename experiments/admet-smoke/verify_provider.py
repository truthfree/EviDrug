"""사용자 Docker에서 ADMET 반복 추론과 메모리 SQLite 저장을 검증한다."""

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
from uuid import uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.contracts import AdmetToolArguments
from evidrug_api.admet.provider import AdmetSubprocessProvider
from evidrug_api.admet.repository import AdmetRepository
from evidrug_api.admet.service import AdmetExecutionService
from evidrug_api.analysis_input.models import TargetMode
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.database import Base


class MetricsCollector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.metrics = []

    def emit(self, record):
        self.metrics.append(record.admet_runtime)


async def verify(docker, container_name, profile):
    memory, cpus = ("512m", "0.1") if profile == "free" else ("2g", "1")
    source = AdmetSubprocessProvider(
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
            "evidrug-admet-smoke:1.4.0",
            "/app/runtime.py",
        )
    )
    collector = MetricsCollector()
    logger = logging.getLogger("evidrug_api.admet.provider")
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
            repository = AdmetRepository(session)
            service = AdmetExecutionService(AdmetToolAdapter(source), repository)
            summaries = []
            manifest_hashes = set()
            for _ in range(2):
                ids = dict(
                    tool_call_id=uuid4(),
                    request_id=uuid4(),
                    analysis_id=analysis.id,
                    run_id=uuid4(),
                )
                arguments = AdmetToolArguments(canonical_smiles="CCO")
                output = await service.execute(**ids, arguments=arguments)
                assert await repository.load_output(ids["tool_call_id"]) == output
                assert await service.execute(**ids, arguments=arguments) == output
                manifest_hashes.add(output.manifest.manifest_sha256)
                summaries.append(
                    {
                        "manifest_sha256": output.manifest.manifest_sha256,
                        "endpoint_count": len(output.result.predictions),
                    }
                )
            assert len(manifest_hashes) == 1
            assert len(collector.metrics) == 2
            assert collector.metrics[0]["cold_start"] is True
            assert collector.metrics[1]["cold_start"] is False
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "profile": profile,
                        "sql_roundtrip": True,
                        "results": summaries,
                        "runtime": collector.metrics,
                    },
                    indent=2,
                    allow_nan=False,
                )
            )
    finally:
        await source.aclose()
        await engine.dispose()
        logger.removeHandler(collector)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("free", "1c-2g"), default="1c-2g")
    args = parser.parse_args()
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker CLI is required")
    container_name = "evidrug-admet-provider-check-" + uuid4().hex
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
        subprocess.run(
            [docker, "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


if __name__ == "__main__":
    main()
