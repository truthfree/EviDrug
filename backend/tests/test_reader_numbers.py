"""원시 수치와 분리된 독자용 숫자 표현 정책을 검증한다."""

import pytest

from evidrug_api.reader_numbers import (
    format_reader_number,
    reader_facing_numbers_are_concise,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.6306613832534401, "0.63"),
        (-3.132798821678443, "-3.13"),
        (447.54300000000035, "447.54"),
        (0.00036742, "3.7e-4"),
        (0.0, "0"),
    ],
)
def test_format_reader_number(value: float, expected: str) -> None:
    assert format_reader_number(value) == expected


def test_reader_precision_accepts_units_and_scientific_notation() -> None:
    assert reader_facing_numbers_are_concise(
        "수용해도는 -3.13 log(mol/L)이고 novelty는 3.7e-4입니다."
    )


def test_reader_precision_rejects_long_decimal() -> None:
    assert not reader_facing_numbers_are_concise("예측값은 0.8158712983131409입니다.")
