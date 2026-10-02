"""분석 입력 라우터의 교체 가능한 의존성."""

from typing import cast

from fastapi import Request

from evidrug_api.analysis_input.smiles import SmilesParser


def get_smiles_parser(request: Request) -> SmilesParser:
    """애플리케이션 시작 시 구성한 SMILES 파서를 반환한다."""

    return cast(SmilesParser, request.app.state.smiles_parser)
