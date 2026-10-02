"""현재 도구의 SQL service를 등록한다. 등록만으로 Agent 권한을 부여하지 않는다."""

from datetime import UTC, datetime
from time import perf_counter

from pydantic import BaseModel

from evidrug_api.admet.adapter import AdmetToolAdapter
from evidrug_api.admet.contracts import AdmetToolArguments, AdmetToolResult
from evidrug_api.admet.repository import AdmetRepository
from evidrug_api.admet.service import AdmetExecutionService
from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.ctoxpred2.adapter import CtoxToolAdapter
from evidrug_api.ctoxpred2.contracts import (
    CTOX_TOOL_VERSION,
    CtoxToolArguments,
    CtoxToolResult,
)
from evidrug_api.ctoxpred2.repository import CtoxRepository
from evidrug_api.ctoxpred2.service import CtoxExecutionService
from evidrug_api.dta.adapter import DtaToolAdapter
from evidrug_api.dta.assay_providers import AssayProviderError, DtaAssayProvider
from evidrug_api.dta.assay_repository import DtaAssayRepository
from evidrug_api.dta.contracts import DtaArguments, DtaResult
from evidrug_api.dta.evidence import DtaAssayArguments, DtaAssayResult
from evidrug_api.dta.repository import DtaRepository
from evidrug_api.dta.service import DtaExecutionService
from evidrug_api.tool_admission.executor import ToolOutcomeError
from evidrug_api.tool_admission.registry import Invocation, ToolBinding
from evidrug_api.tool_execution.runner import input_fingerprint


def admet_binding(adapter: AdmetToolAdapter) -> ToolBinding:
    """ADMET-AI 고정 버전의 service를 연결한다. 전체 manifest는 관측에 복사하지 않는다."""

    async def invoke(call: Invocation, arguments: BaseModel) -> BaseModel:
        output = await AdmetExecutionService(
            adapter,
            AdmetRepository(call.session),
            timeout_seconds=call.timeout_seconds,
        ).execute(
            analysis_id=call.context.analysis_id,
            run_id=call.context.run_id,
            request_id=call.request_id,
            tool_call_id=call.tool_call_id,
            arguments=AdmetToolArguments.model_validate(arguments.model_dump()),
        )
        return output.result

    return ToolBinding(
        "admet_ai",
        "1.4.0",
        "ADMET endpoint와 DrugBank approved percentile을 예측한다.",
        (AnalysisStageName.ADMET,),
        AdmetToolArguments,
        AdmetToolResult,
        invoke,
        300,
    )


def dta_binding(adapter: DtaToolAdapter, *, tool_id: str = "dta") -> ToolBinding:
    """등록 시 provider identity를 고정하고 실행 직전 변경을 거부한다."""
    model = adapter.provider.model

    async def invoke(call: Invocation, arguments: BaseModel) -> BaseModel:
        if adapter.provider.model != model:
            raise ToolOutcomeError("provider_unavailable")
        result = await DtaExecutionService(
            adapter,
            DtaRepository(call.session),
            tool_id=tool_id,
            timeout_seconds=call.timeout_seconds,
        ).execute(
            analysis_id=call.context.analysis_id,
            run_id=call.context.run_id,
            request_id=call.request_id,
            tool_call_id=call.tool_call_id,
            arguments=DtaArguments.model_validate(arguments.model_dump()),
        )
        if result.error_code:
            raise ToolOutcomeError(result.error_code)
        return result

    # checkpoint가 다르면 동일 package version이어도 다른 등록 버전이다.
    version = "model-" + input_fingerprint(model.model_dump(mode="json"))
    return ToolBinding(
        tool_id,
        version,
        "분자와 단백질 서열의 모델별 결합 점수를 예측한다.",
        (AnalysisStageName.DTA,),
        DtaArguments,
        DtaResult,
        invoke,
        300,
    )


def dta_assay_binding(provider: DtaAssayProvider, *, timeout_seconds: int = 30) -> ToolBinding:
    """공식 assay API provider를 DTA 전용 typed binding으로 등록한다."""

    tool_id = {
        "pubchem": "pubchem_bioassay",
    }[provider.source.value]

    async def invoke(call: Invocation, arguments: BaseModel) -> BaseModel:
        parsed = DtaAssayArguments.model_validate(arguments.model_dump())
        repository = DtaAssayRepository(call.session)
        stored = await repository.load(
            tool_call_id=call.tool_call_id,
            request_id=call.request_id,
            analysis_id=call.context.analysis_id,
            run_id=call.context.run_id,
            provider=provider.source,
            provider_version=provider.version,
            arguments=parsed,
        )
        if stored is not None:
            if stored.result is not None:
                return stored.result
            raise ToolOutcomeError(
                stored.error_code or "provider_unavailable",
                external_requests=stored.external_requests,
            )
        await repository.validate_input(call.context.analysis_id, parsed.canonical_smiles)
        started_at = datetime.now(UTC)
        started = perf_counter()
        try:
            result = await provider.query(parsed)
        except AssayProviderError as error:
            finished_at = datetime.now(UTC)
            await repository.save(
                tool_call_id=call.tool_call_id,
                request_id=call.request_id,
                analysis_id=call.context.analysis_id,
                run_id=call.context.run_id,
                provider=provider.source,
                provider_version=provider.version,
                arguments=parsed,
                status="failed",
                error_code=error.code.value,
                external_requests=error.external_requests,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=int((perf_counter() - started) * 1000),
                result=None,
            )
            raise ToolOutcomeError(
                error.code.value, external_requests=error.external_requests
            ) from error
        finished_at = datetime.now(UTC)
        await repository.save(
            tool_call_id=call.tool_call_id,
            request_id=call.request_id,
            analysis_id=call.context.analysis_id,
            run_id=call.context.run_id,
            provider=provider.source,
            provider_version=provider.version,
            arguments=parsed,
            status=result.status,
            error_code=None,
            external_requests=result.external_requests,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=int((perf_counter() - started) * 1000),
            result=result,
        )
        return result

    return ToolBinding(
        tool_id,
        provider.version,
        "동일 compound-target의 공개 실험 assay 근거를 조회한다.",
        (AnalysisStageName.DTA,),
        DtaAssayArguments,
        DtaAssayResult,
        invoke,
        timeout_seconds,
    )


def ctoxpred2_binding(adapter: CtoxToolAdapter) -> ToolBinding:
    """Decision recall을 수행하는 ADMET run에만 CToxPred2를 공개한다."""

    async def invoke(call: Invocation, arguments: BaseModel) -> BaseModel:
        return await CtoxExecutionService(
            adapter,
            CtoxRepository(call.session),
            timeout_seconds=call.timeout_seconds,
        ).execute(
            analysis_id=call.context.analysis_id,
            run_id=call.context.run_id,
            request_id=call.request_id,
            tool_call_id=call.tool_call_id,
            arguments=CtoxToolArguments.model_validate(arguments.model_dump()),
        )

    return ToolBinding(
        "ctoxpred2",
        CTOX_TOOL_VERSION,
        "hERG, Nav1.5, Cav1.2 심장 이온통로 liability를 추가 관측한다.",
        (AnalysisStageName.ADMET,),
        CtoxToolArguments,
        CtoxToolResult,
        invoke,
        300,
    )
