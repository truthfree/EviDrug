"""API와 Celery broker 사이의 작은 작업 전달 경계."""

import asyncio
from typing import Protocol
from uuid import UUID

from celery import Celery


class AnalysisDispatchError(Exception):
    """broker가 분석 시작 메시지를 확인하지 못했음을 나타낸다."""


class AnalysisDispatcher(Protocol):
    """분석 ID만 비동기 실행 큐에 전달하는 계약."""

    async def dispatch(self, analysis_id: UUID) -> None:
        """전달 실패 시 예외를 발생시킨다."""
        ...


class CeleryAnalysisDispatcher:
    """Celery broker에 분석 시작 메시지를 보내는 production adapter."""

    def __init__(self, celery_app: Celery) -> None:
        self.celery_app = celery_app

    async def dispatch(self, analysis_id: UUID) -> None:
        """동기 Celery 전송이 API event loop를 막지 않게 별도 thread에서 실행한다."""

        try:
            await asyncio.to_thread(
                self.celery_app.send_task,
                "evidrug.analysis.run",
                args=[str(analysis_id)],
            )
        except Exception as error:  # Celery/Kombu transports expose different exception types
            raise AnalysisDispatchError from error
