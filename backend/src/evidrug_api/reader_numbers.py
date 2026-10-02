"""독자용 서술의 숫자 표현과 원시 과학 관측값을 분리한다."""

import math
import re

READER_NUMBER_INSTRUCTIONS = (
    "In reader-facing prose, write ordinary decimal values with at most two decimal places. "
    "If a non-zero value would round to 0.00, use scientific notation with two significant "
    "digits or an accurate unit conversion. Keep structured numeric fields, identifiers, enums, "
    "evidence IDs, and units unchanged. "
)

_LONG_DECIMAL = re.compile(r"(?<![\d.])[-+]?\d+\.(\d{3,})(?![\d.])")


def format_reader_number(value: float) -> str:
    """원시 값은 바꾸지 않고 독자용 문자열만 간결하게 만든다."""
    if not math.isfinite(value):
        raise ValueError("reader-facing number must be finite")
    if value != 0 and abs(value) < 0.005:
        mantissa, exponent = f"{value:.1e}".split("e")
        return f"{mantissa}e{int(exponent):+d}"
    rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if rendered == "-0" else rendered


def reader_facing_numbers_are_concise(*texts: str) -> bool:
    """일반 소수점 셋째 자리 이상이 독자용 문장에 남았는지 검사한다."""
    return not any(_LONG_DECIMAL.search(text) for text in texts)
