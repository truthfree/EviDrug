"""DeepPurpose의 런타임 다운로드 경로를 명시적으로 비활성화한다."""


def download(*_args: object, **_kwargs: object) -> str:
    raise RuntimeError("Runtime downloads are disabled; use the pinned baked checkpoint")
