"""Training/evaluation metrics are unavailable in this inference-only image."""


def concordance_index(*_args: object, **_kwargs: object) -> float:
    raise RuntimeError("lifelines metrics are outside the DTA smoke-test scope")
