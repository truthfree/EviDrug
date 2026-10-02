import copy

import pytest

from evidrug_api.ctoxpred2.adapter import (
    CtoxReportError,
    CtoxToolAdapter,
    normalize_ctox_report,
)
from evidrug_api.ctoxpred2.contracts import (
    CTOX_PACKAGES,
    CtoxChannel,
    CtoxPredictionLabel,
    CtoxToolArguments,
)

SMILES = "CCO"


def provider_report() -> dict[str, object]:
    return {
        "status": "passed",
        "canonical_smiles": SMILES,
        "upstream_repository": "issararab/CToxPred2",
        "upstream_commit": "2a31aa119e27b6b69a5588d18a01f2a27fef4524",
        "profile": "rf_ssl",
        "packages": CTOX_PACKAGES,
        "artifacts": {
            "decriptors_preprocessing/global_preprocessing_pipeline.sav": (
                "58ca80d195ca2f261aa50e68e88a82aebdccdc106149f89d5bb9d10d54e7f301"
            ),
            "random_forest/hERG/_ssl_herg_model.joblib": (
                "1060d8afee2df325410e11fe5b1a6457656f158f63dc4baab0dc1ccc9a0a98c6"
            ),
            "random_forest/Nav1.5/_ssl_nav_model.joblib": (
                "0b57bc75c80518c1ec01a597f0fde3cbeb5a6a55f8f48d7afc26ea5b8ff30b8b"
            ),
            "random_forest/Cav1.2/_ssl_cav_model.joblib": (
                "d7be3bcb59890058208a18167246c395d5ac4ad5b9d762ff07b0b0643ac375e9"
            ),
        },
        "predictions": {
            "hERG": {"label": 1, "class_probability": 0.82},
            "Nav1.5": {"label": 0, "class_probability": 0.73},
            "Cav1.2": {"label": 0, "class_probability": 0.68},
        },
    }


def test_normalize_ctox_report_preserves_channel_labels_and_probability() -> None:
    result = normalize_ctox_report(provider_report(), expected_smiles=SMILES)

    assert [item.channel for item in result.predictions] == [
        CtoxChannel.HERG,
        CtoxChannel.NAV1_5,
        CtoxChannel.CAV1_2,
    ]
    assert result.predictions[0].label is CtoxPredictionLabel.POSITIVE
    assert result.predictions[0].class_probability == 0.82
    assert result.model.profile == "rf_ssl"
    assert len(result.model.artifacts) == 4


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("canonical_smiles",), "CCC"),
        (("profile",), "dnn_mc_dropout"),
        (("predictions", "hERG", "label"), 2),
        (("predictions", "hERG", "class_probability"), 1.1),
    ),
)
def test_normalize_ctox_report_rejects_invalid_values(path: tuple[str, ...], value: object) -> None:
    report = copy.deepcopy(provider_report())
    target: dict[str, object] = report
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[path[-1]] = value

    with pytest.raises(CtoxReportError):
        normalize_ctox_report(report, expected_smiles=SMILES)


def test_normalize_ctox_report_rejects_missing_channel() -> None:
    report = provider_report()
    predictions = report["predictions"]
    assert isinstance(predictions, dict)
    del predictions["Cav1.2"]

    with pytest.raises(CtoxReportError):
        normalize_ctox_report(report)


@pytest.mark.asyncio
async def test_ctox_adapter_validates_provider_output() -> None:
    class FakeProvider:
        async def predict(self, canonical_smiles: str) -> dict[str, object]:
            report = provider_report()
            report["canonical_smiles"] = canonical_smiles
            return report

    result = await CtoxToolAdapter(FakeProvider()).execute(
        CtoxToolArguments(canonical_smiles=SMILES)
    )

    assert result.canonical_smiles == SMILES
