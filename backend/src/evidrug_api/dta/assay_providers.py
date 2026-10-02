"""공식 공개 API에서 exact compound-target assay 근거를 읽는 provider."""

import asyncio
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Protocol

import httpx
from rdkit import Chem, rdBase

from evidrug_api.analysis_input.models import PotencyEndpoint
from evidrug_api.dta.evidence import (
    AssayEvidence,
    AssayEvidenceKind,
    AssaySource,
    DtaAssayArguments,
    DtaAssayResult,
)


class AssayProviderErrorCode(StrEnum):
    TIMEOUT = "provider_timeout"
    UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "provider_rate_limited"
    INVALID_RESPONSE = "provider_invalid_response"


class AssayProviderError(RuntimeError):
    """외부 오류 원문 없이 고정 코드를 실행 경계로 전달한다."""

    def __init__(self, code: AssayProviderErrorCode, *, external_requests: int = 1) -> None:
        self.code = code
        self.external_requests = external_requests
        super().__init__(code.value)


class DtaAssayProvider(Protocol):
    source: AssaySource
    version: str

    async def query(self, arguments: DtaAssayArguments) -> DtaAssayResult: ...


def _canonical_smiles(value: str) -> str | None:
    # RDKit parser는 실패한 원문 SMILES를 stderr에 쓸 수 있어 분석 입력 로그 노출을 막는다.
    with rdBase.BlockLogs():
        molecule = Chem.MolFromSmiles(value)
    return (
        None
        if molecule is None
        else Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)
    )


def _same_structure(left: str, right: str) -> bool:
    canonical_left = _canonical_smiles(left)
    return canonical_left is not None and canonical_left == _canonical_smiles(right)


def _positive_number(value: object) -> float | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return float(parsed)


def _limited_text(value: object, limit: int = 1000) -> str | None:
    text = str(value or "").strip()
    return text[:limit] or None


class HttpAssayProvider:
    """한 요청에 숨은 retry 없이 timeout·HTTP·JSON 오류를 정규화한다."""

    source: AssaySource
    version: str

    def __init__(self, client: httpx.AsyncClient, *, timeout_seconds: float = 20) -> None:
        self.client = client
        self.timeout_seconds = timeout_seconds

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        data: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await self.client.request(
                    method, url, params=params, data=data, headers=headers
                )
        except (TimeoutError, httpx.TimeoutException) as error:
            raise AssayProviderError(AssayProviderErrorCode.TIMEOUT) from error
        except httpx.HTTPError as error:
            raise AssayProviderError(AssayProviderErrorCode.UNAVAILABLE) from error
        if response.status_code == 429:
            raise AssayProviderError(AssayProviderErrorCode.RATE_LIMITED)
        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            raise AssayProviderError(AssayProviderErrorCode.UNAVAILABLE)
        if response.status_code >= 400:
            raise AssayProviderError(AssayProviderErrorCode.INVALID_RESPONSE)
        try:
            return response.json()
        except ValueError as error:
            raise AssayProviderError(AssayProviderErrorCode.INVALID_RESPONSE) from error

    async def _counted_request(
        self,
        counter: list[int],
        method: str,
        url: str,
        **kwargs: object,
    ) -> object:
        counter[0] += 1
        try:
            return await self._request(method, url, **kwargs)  # type: ignore[arg-type]
        except AssayProviderError as error:
            error.external_requests = counter[0]
            raise


class PubchemBioassayProvider(HttpAssayProvider):
    """CID assay summary에서 target accession이 정확히 일치하는 행만 채택한다."""

    source = AssaySource.PUBCHEM
    version = "pubchem-pug-rest-2026-09-30"
    base_url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

    async def query(self, arguments: DtaAssayArguments) -> DtaAssayResult:
        requests = [0]
        resolved = await self._counted_request(
            requests,
            "POST",
            f"{self.base_url}/compound/smiles/cids/JSON",
            data={"smiles": arguments.canonical_smiles},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        identifiers = (
            resolved.get("IdentifierList", {}).get("CID", []) if isinstance(resolved, dict) else []
        )
        if not isinstance(identifiers, list) or len(identifiers) != 1:
            return _result(self.source, [], external_requests=requests[0])
        payload = await self._counted_request(
            requests, "GET", f"{self.base_url}/compound/cid/{identifiers[0]}/assaysummary/JSON"
        )
        table = payload.get("Table") if isinstance(payload, dict) else None
        columns = table.get("Columns", {}).get("Column") if isinstance(table, dict) else None
        rows = table.get("Row") if isinstance(table, dict) else None
        if not isinstance(columns, list) or not isinstance(rows, list):
            return _result(self.source, [], external_requests=requests[0])
        evidence = []
        for row in rows:
            cells = row.get("Cell") if isinstance(row, dict) else None
            if not isinstance(cells, list) or len(cells) != len(columns):
                continue
            item = dict(zip((str(column) for column in columns), cells, strict=True))
            if str(item.get("Target Accession", "")) != arguments.uniprot_accession:
                continue
            aid = str(item.get("AID", "")).strip()
            sid = str(item.get("SID", "")).strip() or "unknown"
            activity_name = str(item.get("Activity Name", "")).strip()
            value = _positive_number(item.get("Activity Value [uM]"))
            if aid and value is not None and activity_name in {"Kd", "Ki", "IC50"}:
                evidence.append(
                    AssayEvidence(
                        source=self.source,
                        source_record_id=f"aid:{aid}:sid:{sid}:{activity_name}",
                        kind=AssayEvidenceKind.QUANTITATIVE,
                        endpoint=PotencyEndpoint(activity_name),
                        value=value,
                        unit="uM",
                        assay_description=_limited_text(item.get("Assay Name")),
                        pmid=str(item.get("PubMed ID") or "").strip() or None,
                    )
                )
            elif aid and str(item.get("Activity Outcome", "")).strip():
                evidence.append(
                    AssayEvidence(
                        source=self.source,
                        source_record_id=(
                            f"aid:{aid}:sid:{sid}:{str(item.get('Activity Outcome', '')).strip()}"
                        ),
                        kind=AssayEvidenceKind.QUALITATIVE,
                        qualitative_outcome=str(item["Activity Outcome"]),
                        assay_description=_limited_text(item.get("Assay Name")),
                        pmid=str(item.get("PubMed ID") or "").strip() or None,
                    )
                )
        return _result(self.source, evidence, external_requests=requests[0])


def _result(
    source: AssaySource,
    evidence: list[AssayEvidence],
    *,
    external_requests: int,
) -> DtaAssayResult:
    unique = {item.source_record_id: item for item in evidence}
    values = tuple(unique[key] for key in sorted(unique))
    return DtaAssayResult(
        source=source,
        status="succeeded" if values else "no_records",
        evidence=values,
        external_requests=external_requests,
    )
