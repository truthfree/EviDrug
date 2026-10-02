"""adaptive episode가 #155 세 정책과 같은 snapshot을 사용하고 live 호출을 피한다."""

import hashlib
from uuid import UUID, uuid5

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_policy_comparison import database as database_fixture
from test_policy_comparison import manifest, snapshots
from test_tool_capabilities import binding

from evidrug_api.analysis_jobs.models import AnalysisStageName
from evidrug_api.analysis_jobs.tables import AnalysisRecord
from evidrug_api.tool_admission.capabilities import (
    EvidenceGap,
    EvidenceGapKind,
    EvidenceKind,
    ResourceClass,
    capabilities_for_bindings,
)
from evidrug_api.tool_admission.contracts import ToolPin
from evidrug_api.tool_admission.registry import ToolRegistry
from evidrug_api.trajectory.adaptive_selector import AdaptiveSelectorInput
from evidrug_api.trajectory.admet_snapshot import AdmetSnapshotRepository
from evidrug_api.trajectory.contracts import TrajectoryStep
from evidrug_api.trajectory.ctox_snapshot import CtoxSnapshotRepository
from evidrug_api.trajectory.policy_comparison import PolicyComparisonError, PolicyComparisonRunner
from evidrug_api.trajectory.tables import TrajectoryStepRecord
from evidrug_api.trajectory.validator import validate_stored_episode

Database = tuple[async_sessionmaker[AsyncSession], UUID]
database = database_fixture


async def selector_config(
    session: AsyncSession,
    analysis_id: UUID,
    admet_id: UUID,
    ctox_id: UUID,
    *,
    max_latency: ResourceClass = ResourceClass.HIGH,
    allowed: bool = True,
) -> tuple[AdaptiveSelectorInput, ToolRegistry]:
    admet = await AdmetSnapshotRepository(session).load(admet_id)
    ctox = await CtoxSnapshotRepository(session).load(ctox_id)
    assert admet is not None and ctox is not None
    bindings = (
        binding("admet_ai", admet.entries[0].tool_version, AnalysisStageName.ADMET),
        binding("ctoxpred2", ctox.entries[0].tool_version, AnalysisStageName.ADMET),
    )
    return (
        AdaptiveSelectorInput(
            policy_version="adaptive-cardiac-v1",
            gap=EvidenceGap(
                gap_id=uuid5(analysis_id, "comparison:cardiac_ion_channel_evidence"),
                kind=EvidenceGapKind.CARDIAC_ION_CHANNEL,
                objective="심장 이온통로 근거 공백 보완",
                target_context="candidate molecule",
                required_evidence_kind=EvidenceKind.MODEL_PREDICTION,
                endpoint_id="herg",
                expected_field="class_probability",
                priority=2,
            ),
            agent=AnalysisStageName.ADMET,
            capabilities=capabilities_for_bindings(bindings),
            allowed_tools=(ToolPin(tool_id="ctoxpred2", version=ctox.entries[0].tool_version),)
            if allowed
            else (),
            available_inputs=("canonical_smiles",),
            remaining_tool_calls=1,
            max_cost_class=ResourceClass.HIGH,
            max_latency_class=max_latency,
        ),
        ToolRegistry(bindings),
    )


@pytest.mark.asyncio
async def test_adaptive_replays_same_snapshots_and_validates_saved_choice(
    database: Database,
) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, admet_provider, ctox_provider = await snapshots(factory, analysis_id)
    async with factory() as session:
        config, registry = await selector_config(session, analysis_id, admet_id, ctox_id)
        report = await PolicyComparisonRunner(session).run_with_adaptive(
            manifest(analysis_id, admet_id, ctox_id), selector_input=config, registry=registry
        )
        rows = tuple(
            await session.scalars(
                select(TrajectoryStepRecord).where(
                    TrajectoryStepRecord.episode_id == report.adaptive.episode_id
                )
            )
        )
    assert report.selection.selected_action is not None
    assert report.selection.selected_action.tool_id == "ctoxpred2"
    assert [item.strategy for item in report.fixed.outcomes] == [
        "baseline",
        "all_tools",
        "decision_recall",
    ]
    assert report.adaptive.strategy == "adaptive"
    assert report.adaptive.selected_tool_ids == ("admet_ai", "ctoxpred2")
    assert report.adaptive.covered_gap_count == 1
    assert report.adaptive.validation_verdict == "accepted"
    assert admet_provider.calls == 1 and ctox_provider.calls == 1
    steps = [TrajectoryStep.model_validate_json(row.step_json) for row in rows]
    selector_steps = [step for step in steps if step.selector_input_json is not None]
    assert len(selector_steps) == 1
    assert selector_steps[0].selected_action_id == report.selection.selected_action.action_id


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_by", ["latency", "allowlist"])
async def test_adaptive_no_match_stops_with_gap_and_no_live_fallback(
    database: Database, blocked_by: str
) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, admet_provider, ctox_provider = await snapshots(factory, analysis_id)
    async with factory() as session:
        config, registry = await selector_config(
            session,
            analysis_id,
            admet_id,
            ctox_id,
            max_latency=ResourceClass.LOW if blocked_by == "latency" else ResourceClass.HIGH,
            allowed=blocked_by != "allowlist",
        )
        report = await PolicyComparisonRunner(session).run_with_adaptive(
            manifest(analysis_id, admet_id, ctox_id), selector_input=config, registry=registry
        )
    assert report.selection.selected_action is None
    assert report.adaptive.selected_tool_ids == ("admet_ai",)
    assert report.adaptive.unresolved_gap_count == 1
    assert report.adaptive.validation_verdict == "accepted"
    assert admet_provider.calls == 1 and ctox_provider.calls == 1


@pytest.mark.asyncio
async def test_adaptive_rejects_unregistered_selector_pin_before_replay(database: Database) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, _, _ = await snapshots(factory, analysis_id)
    async with factory() as session:
        config, registry = await selector_config(session, analysis_id, admet_id, ctox_id)
        bad = config.model_copy(
            update={"allowed_tools": (ToolPin(tool_id="ctoxpred2", version="other"),)}
        )
        with pytest.raises(PolicyComparisonError, match="adaptive_tool_not_registered"):
            await PolicyComparisonRunner(session).run_with_adaptive(
                manifest(analysis_id, admet_id, ctox_id), selector_input=bad, registry=registry
            )


@pytest.mark.asyncio
async def test_adaptive_rejects_modified_capability_cost_metadata(database: Database) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, _, _ = await snapshots(factory, analysis_id)
    async with factory() as session:
        config, registry = await selector_config(session, analysis_id, admet_id, ctox_id)
        changed = tuple(
            item.model_copy(update={"cost_class": ResourceClass.LOW})
            if item.tool_id == "ctoxpred2"
            else item
            for item in config.capabilities
        )
        bad = config.model_copy(update={"capabilities": changed})
        with pytest.raises(PolicyComparisonError, match="adaptive_capability_catalog_mismatch"):
            await PolicyComparisonRunner(session).run_with_adaptive(
                manifest(analysis_id, admet_id, ctox_id), selector_input=bad, registry=registry
            )


@pytest.mark.asyncio
async def test_adaptive_validator_detects_tampered_selected_action(database: Database) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, _, _ = await snapshots(factory, analysis_id)
    async with factory() as session:
        config, registry = await selector_config(session, analysis_id, admet_id, ctox_id)
        report = await PolicyComparisonRunner(session).run_with_adaptive(
            manifest(analysis_id, admet_id, ctox_id), selector_input=config, registry=registry
        )
        rows = tuple(
            await session.scalars(
                select(TrajectoryStepRecord).where(
                    TrajectoryStepRecord.episode_id == report.adaptive.episode_id
                )
            )
        )
        row, step = next(
            (item, saved)
            for item in rows
            if (saved := TrajectoryStep.model_validate_json(item.step_json)).selector_input_json
            is not None
        )
        alternate = step.available_actions[0].model_copy(
            update={"action_id": "tool:ctoxpred2-alternate"}
        )
        altered = step.model_copy(
            update={
                "available_actions": (*step.available_actions, alternate),
                "selected_action_id": alternate.action_id,
            }
        )
        row.step_json = altered.model_dump_json()
        row.step_sha256 = hashlib.sha256(row.step_json.encode()).hexdigest()
        await session.commit()
        validation = await validate_stored_episode(session, report.adaptive.episode_id)
    assert "selector_choice_mismatch" in {issue.reason_code for issue in validation.issues}


@pytest.mark.asyncio
async def test_adaptive_replay_miss_never_invokes_live_provider(database: Database) -> None:
    factory, analysis_id = database
    admet_id, ctox_id, admet_provider, ctox_provider = await snapshots(factory, analysis_id)
    async with factory() as session:
        analysis = await session.get(AnalysisRecord, analysis_id)
        assert analysis is not None
        analysis.canonical_smiles = "CCC"
        await session.commit()
        config, registry = await selector_config(session, analysis_id, admet_id, ctox_id)
        report = await PolicyComparisonRunner(session).run_with_adaptive(
            manifest(analysis_id, admet_id, ctox_id), selector_input=config, registry=registry
        )
    assert report.adaptive.replay_misses == 2
    assert report.adaptive.unresolved_gap_count == 1
    assert report.adaptive.validation_verdict == "accepted"
    assert admet_provider.calls == 1 and ctox_provider.calls == 1
