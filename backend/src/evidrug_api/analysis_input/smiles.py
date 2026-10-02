"""RDKit을 이용한 SMILES 파싱과 정규화."""

from typing import Protocol

from rdkit import Chem, rdBase


class InvalidSmiles(ValueError):
    """입력 문자열을 유효한 분자로 해석할 수 없을 때 발생한다."""


class SmilesParser(Protocol):
    """분석 입력 API가 사용하는 교체 가능한 SMILES 파서 계약."""

    def canonicalize(self, smiles: str) -> str:
        """유효한 SMILES를 canonical SMILES로 변환한다."""

        ...


class RdkitSmilesParser:
    """RDKit의 분자 파서를 사용해 SMILES를 검증하고 정규화한다."""

    def canonicalize(self, smiles: str) -> str:
        with rdBase.BlockLogs():
            molecule = Chem.MolFromSmiles(smiles, sanitize=True)

        if molecule is None or molecule.GetNumAtoms() == 0:
            raise InvalidSmiles("SMILES could not be parsed as a molecule")

        return str(Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True))
