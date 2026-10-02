import json

import httpx
import pytest

from evidrug_api.dta.assay_providers import (
    AssayProviderError,
    AssayProviderErrorCode,
    PubchemBioassayProvider,
)
from evidrug_api.dta.evidence import (
    AssayEvidenceKind,
    DtaAssayArguments,
)

ARGUMENTS = DtaAssayArguments(canonical_smiles="CCO", uniprot_accession="P00533")


def response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload), headers={"content-type": "application/json"}
    )


@pytest.mark.asyncio
async def test_pubchem_filters_target_and_separates_quantitative_and_qualitative() -> None:
    columns = [
        "AID",
        "CID",
        "Activity Outcome",
        "Target Accession",
        "Activity Value [uM]",
        "Activity Name",
        "Assay Name",
        "PubMed ID",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return response({"IdentifierList": {"CID": [702]}})
        return response(
            {
                "Table": {
                    "Columns": {"Column": columns},
                    "Row": [
                        {"Cell": ["1", "702", "Active", "P00533", "0.05", "Ki", "exact", "12"]},
                        {"Cell": ["2", "702", "Inactive", "P00533", "", "", "qual", ""]},
                        {"Cell": ["3", "702", "Active", "OTHER", "0.01", "Kd", "wrong", ""]},
                    ],
                }
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PubchemBioassayProvider(client).query(ARGUMENTS)

    assert result.status == "succeeded"
    assert [item.kind for item in result.evidence] == [
        AssayEvidenceKind.QUANTITATIVE,
        AssayEvidenceKind.QUALITATIVE,
    ]
    assert result.external_requests == 2


@pytest.mark.asyncio
async def test_rate_limit_has_stable_error_code() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(429))
    ) as client:
        with pytest.raises(AssayProviderError) as captured:
            await PubchemBioassayProvider(client).query(ARGUMENTS)
    assert captured.value.code is AssayProviderErrorCode.RATE_LIMITED
    assert captured.value.external_requests == 1


@pytest.mark.asyncio
async def test_http_timeout_has_stable_error_code() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(AssayProviderError) as captured:
            await PubchemBioassayProvider(client).query(ARGUMENTS)
    assert captured.value.code is AssayProviderErrorCode.TIMEOUT
