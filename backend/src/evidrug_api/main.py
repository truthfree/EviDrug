from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from evidrug_api.analysis_input.router import router as analysis_input_router
from evidrug_api.analysis_input.smiles import RdkitSmilesParser, SmilesParser
from evidrug_api.analysis_jobs.dispatcher import AnalysisDispatcher, CeleryAnalysisDispatcher
from evidrug_api.analysis_jobs.router import router as analysis_jobs_router
from evidrug_api.api.abuse_guard import ApiAbuseGuard
from evidrug_api.api.health import router as health_router
from evidrug_api.auth.rate_limit import AuthAttemptLimiter, RedisAuthAttemptLimiter
from evidrug_api.auth.router import router as auth_router
from evidrug_api.config import Settings, get_settings
from evidrug_api.database import (
    DatabaseSessionFactory,
    create_database_engine,
    create_database_session_factory,
)
from evidrug_api.disease_search.client import DiseaseSearcher, OpenTargetsDiseaseSearcher
from evidrug_api.disease_search.router import router as disease_search_router
from evidrug_api.worker import celery_app


def create_app(
    settings: Settings | None = None,
    auth_attempt_limiter: AuthAttemptLimiter | None = None,
    disease_searcher: DiseaseSearcher | None = None,
    smiles_parser: SmilesParser | None = None,
    database_session_factory: DatabaseSessionFactory | None = None,
    analysis_dispatcher: AnalysisDispatcher | None = None,
) -> FastAPI:
    """설정과 교체 가능한 외부 의존성을 사용해 FastAPI 앱을 구성한다."""

    runtime_settings = settings or get_settings()
    redis_client: Redis | None = None
    open_targets_http_client: AsyncClient | None = None
    database_engine: AsyncEngine | None = None
    if auth_attempt_limiter is None:
        redis_client = Redis.from_url(runtime_settings.redis_url, decode_responses=True)
        auth_attempt_limiter = RedisAuthAttemptLimiter(
            redis=redis_client,
            attempt_limit=runtime_settings.auth_attempt_limit,
            window_seconds=runtime_settings.auth_attempt_window_seconds,
        )
    if disease_searcher is None:
        open_targets_http_client = AsyncClient(
            timeout=runtime_settings.open_targets_timeout_seconds
        )
        disease_searcher = OpenTargetsDiseaseSearcher(
            http_client=open_targets_http_client,
            graphql_url=runtime_settings.open_targets_graphql_url,
        )
    if smiles_parser is None:
        smiles_parser = RdkitSmilesParser()
    if database_session_factory is None:
        database_engine = create_database_engine(runtime_settings.database_url)
        database_session_factory = create_database_session_factory(database_engine)
    if analysis_dispatcher is None:
        analysis_dispatcher = CeleryAnalysisDispatcher(celery_app)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if redis_client is not None:
                await redis_client.aclose()
            if open_targets_http_client is not None:
                await open_targets_http_client.aclose()
            if database_engine is not None:
                await database_engine.dispose()

    public_deployment = runtime_settings.environment in {"staging", "production"}
    application = FastAPI(
        title=runtime_settings.app_name,
        version="1.0.0",
        docs_url=None if public_deployment else f"{runtime_settings.api_v1_prefix}/docs",
        redoc_url=None if public_deployment else "/redoc",
        openapi_url=None if public_deployment else f"{runtime_settings.api_v1_prefix}/openapi.json",
        lifespan=lifespan,
    )
    application.state.settings = runtime_settings
    application.state.auth_attempt_limiter = auth_attempt_limiter
    application.state.disease_searcher = disease_searcher
    application.state.smiles_parser = smiles_parser
    application.state.database_session_factory = database_session_factory
    application.state.analysis_dispatcher = analysis_dispatcher
    application.add_middleware(ApiAbuseGuard, settings=runtime_settings)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(health_router, prefix=runtime_settings.api_v1_prefix)
    application.include_router(auth_router, prefix=runtime_settings.api_v1_prefix)
    application.include_router(disease_search_router, prefix=runtime_settings.api_v1_prefix)
    application.include_router(analysis_input_router, prefix=runtime_settings.api_v1_prefix)
    application.include_router(analysis_jobs_router, prefix=runtime_settings.api_v1_prefix)
    return application


app = create_app()


def run() -> None:
    uvicorn.run("evidrug_api.main:app", host="0.0.0.0", port=8000)
