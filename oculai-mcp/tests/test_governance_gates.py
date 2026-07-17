"""Unit tests for deterministic assessment and human-approval gates."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from oculai_mcp.tools import assessment, outreach, report, review_orchestrator
from oculai_mcp.tools.errors import AuthError, ConflictError, ValidationError
from oculai_mcp.tools.evidence_tier import get_tier

pytestmark = pytest.mark.unit


def test_social_profiles_are_secondary_evidence() -> None:
    assert get_tier("zhihu", "profile") == 2
    assert get_tier("Juejin", "profile") == 2
    assert get_tier("csdn", "profile") == 2
    assert get_tier("github", "code") == 1


@pytest.mark.asyncio
async def test_cross_run_evidence_reference_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid4()
    person_id = uuid4()
    evidence_id = uuid4()
    monkeypatch.setattr(
        assessment,
        "fetch_with_retry",
        AsyncMock(
            return_value=[
                {
                    "evidence_id": evidence_id,
                    "run_id": uuid4(),
                    "person_id": person_id,
                }
            ]
        ),
    )

    with pytest.raises(ValidationError, match="belong to the assessed run/person"):
        await assessment._validate_evidence_references(run_id, person_id, [evidence_id])


@pytest.mark.asyncio
async def test_high_score_without_primary_evidence_never_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    person_id = uuid4()
    evidence_id = uuid4()
    monkeypatch.setattr(assessment, "_validate_evidence_references", AsyncMock())
    monkeypatch.setattr(
        assessment,
        "_filter_evidence_for_dimension",
        AsyncMock(return_value=[evidence_id]),
    )
    monkeypatch.setattr(
        assessment,
        "fetch_with_retry",
        AsyncMock(return_value=[{"evidence_id": evidence_id, "tier": 2}]),
    )
    fetchrow = AsyncMock()
    execute = AsyncMock()
    monkeypatch.setattr(assessment, "fetchrow_with_retry", fetchrow)
    monkeypatch.setattr(assessment, "execute_with_retry", execute)

    with pytest.raises(ValidationError, match="evidence requirements"):
        await assessment.score_candidate(
            run_id=run_id,
            person_id=person_id,
            dimensions={"skill_match": 8.0},
            assessor_agent="fit_evaluator",
            evidence_ids=[str(evidence_id)],
        )

    fetchrow.assert_not_awaited()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_dimensions_use_full_profile_and_missing_gate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    person_id = uuid4()
    monkeypatch.setattr(
        assessment,
        "fetch_with_retry",
        AsyncMock(return_value=[{"dimension": "engineering", "score": 10.0, "confidence": 1.0}]),
    )
    execute = AsyncMock()
    monkeypatch.setattr(assessment, "execute_with_retry", execute)

    result = await assessment._compute_overall_score(person_id, run_id, "default")

    assert result["overall_score"] == 1.6
    assert result["gate_status"] == "failed"
    assert result["gate_failures"] == [
        {
            "dimension": "skill_match",
            "required": 4.0,
            "actual": None,
            "reason": "required_dimension_missing",
        }
    ]
    breakdown = execute.await_args.args[4]
    assert breakdown["normalization"] == "full_profile_weight"
    assert breakdown["assessment_coverage"] == 0.16


@pytest.mark.asyncio
async def test_approval_decision_records_authority_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_id = uuid4()
    run_id = uuid4()
    now = datetime.now(timezone.utc)
    fetchrow = AsyncMock(
        side_effect=[
            {
                "approval_id": approval_id,
                "status": "pending",
                "action_type": "export_report",
                "requested_by": "report_agent",
            },
            {
                "approval_id": approval_id,
                "run_id": run_id,
                "action_type": "export_report",
                "status": "approved",
                "reviewed_by": "alice@example.com",
                "reviewed_at": now,
            },
        ]
    )
    monkeypatch.setattr(outreach, "fetchrow_with_retry", fetchrow)

    result = await outreach.decide_human_approval(
        approval_id=approval_id,
        decision="approve",
        reviewer_id="alice@example.com",
        reviewer_role="privacy_officer",
        review_notes="Approved for the named internal recipient.",
    )

    assert result["status"] == "approved"
    assert result["reviewer_role"] == "privacy_officer"
    update_args = fetchrow.await_args_list[1].args
    assert update_args[2] == "approved"
    assert update_args[3] == "alice@example.com"
    assert update_args[6]["approval_review"]["reviewer_role"] == "privacy_officer"


@pytest.mark.asyncio
@pytest.mark.parametrize("action_type", ["export_report", "finalize_review"])
async def test_trusted_desktop_human_reviewer_can_approve_sensitive_actions(
    monkeypatch: pytest.MonkeyPatch,
    action_type: str,
) -> None:
    """Desktop's fixed generic role can approve export and shortlist finalization."""
    approval_id = uuid4()
    run_id = uuid4()
    now = datetime.now(timezone.utc)
    fetchrow = AsyncMock(
        side_effect=[
            {
                "approval_id": approval_id,
                "status": "pending",
                "action_type": action_type,
                "requested_by": "review_agent",
            },
            {
                "approval_id": approval_id,
                "run_id": run_id,
                "action_type": action_type,
                "status": "approved",
                "reviewed_by": "oculai-desktop-user",
                "reviewed_at": now,
            },
        ]
    )
    monkeypatch.setattr(outreach, "fetchrow_with_retry", fetchrow)

    result = await outreach.decide_human_approval(
        approval_id=approval_id,
        decision="approved",
        reviewer_id="oculai-desktop-user",
        reviewer_role="human_reviewer",
        review_notes="Approved from the trusted desktop review UI.",
    )

    assert result["status"] == "approved"
    assert result["reviewer_role"] == "human_reviewer"
    assert fetchrow.await_args_list[1].args[6]["approval_review"]["reviewer_role"] == "human_reviewer"


@pytest.mark.asyncio
async def test_requester_cannot_self_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        outreach,
        "fetchrow_with_retry",
        AsyncMock(
            return_value={
                "approval_id": uuid4(),
                "status": "pending",
                "action_type": "finalize_review",
                "requested_by": "alice@example.com",
            }
        ),
    )
    with pytest.raises(AuthError, match="cannot review their own"):
        await outreach.decide_human_approval(
            approval_id=uuid4(),
            decision="approve",
            reviewer_id="alice@example.com",
            reviewer_role="hiring_manager",
            review_notes="I approve my own request.",
        )


@pytest.mark.asyncio
async def test_human_can_deny_pending_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    approval_id = uuid4()
    run_id = uuid4()
    now = datetime.now(timezone.utc)
    fetchrow = AsyncMock(
        side_effect=[
            {
                "approval_id": approval_id,
                "status": "pending",
                "action_type": "finalize_review",
                "requested_by": "review_agent",
            },
            {
                "approval_id": approval_id,
                "run_id": run_id,
                "action_type": "finalize_review",
                "status": "denied",
                "reviewed_by": "manager@example.com",
                "reviewed_at": now,
            },
        ]
    )
    monkeypatch.setattr(outreach, "fetchrow_with_retry", fetchrow)

    result = await outreach.decide_human_approval(
        approval_id=approval_id,
        decision="deny",
        reviewer_id="manager@example.com",
        reviewer_role="hiring_manager",
        review_notes="Candidate ranking requires another evidence pass.",
    )

    assert result["status"] == "denied"
    assert fetchrow.await_args_list[1].args[2] == "denied"


@pytest.mark.asyncio
async def test_approval_is_scoped_and_consumed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    approval_id = uuid4()
    run_id = uuid4()
    reviewed_at = datetime.now(timezone.utc)
    approved = {
        "approval_id": approval_id,
        "run_id": run_id,
        "action_type": "export_report",
        "status": "approved",
        "reviewed_by": "alice@example.com",
        "reviewed_at": reviewed_at,
        "action_context": {"format": "html"},
    }
    monkeypatch.setattr(
        outreach,
        "fetchrow_with_retry",
        AsyncMock(
            side_effect=[
                approved,
                {
                    "approval_id": approval_id,
                    "reviewed_by": "alice@example.com",
                    "reviewed_at": reviewed_at,
                },
            ]
        ),
    )
    result = await outreach.consume_approved_action(
        approval_id,
        run_id,
        "export_report",
        {"format": "html"},
        "report_export",
    )
    assert result["reviewed_by"] == "alice@example.com"

    monkeypatch.setattr(
        outreach,
        "fetchrow_with_retry",
        AsyncMock(
            return_value={
                **approved,
                "action_context": {
                    "format": "html",
                    "consumed_at": result["consumed_at"],
                },
            }
        ),
    )
    with pytest.raises(ConflictError, match="already been consumed"):
        await outreach.consume_approved_action(
            approval_id,
            run_id,
            "export_report",
            {"format": "html"},
            "report_export",
        )


@pytest.mark.asyncio
async def test_report_export_calls_mandatory_approval_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    run = {
        "run_id": run_id,
        "title": "Test",
        "status": "running",
        "created_at": datetime.now(timezone.utc),
        "target_profile": {},
        "active_plan_id": None,
    }
    monkeypatch.setattr(report, "fetchrow_with_retry", AsyncMock(return_value=run))
    approval_gate = AsyncMock(side_effect=AuthError("human approval is required"))
    monkeypatch.setattr(report, "consume_approved_action", approval_gate)

    with pytest.raises(AuthError, match="human approval"):
        await report.export_report(run_id, "html")
    approval_gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_approved_report_export_contains_approval_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    approval_id = uuid4()
    run = {
        "run_id": run_id,
        "title": "Test",
        "status": "running",
        "created_at": datetime.now(timezone.utc),
        "target_profile": {},
        "active_plan_id": None,
    }
    monkeypatch.setattr(report, "fetchrow_with_retry", AsyncMock(return_value=run))
    monkeypatch.setattr(report, "fetch_with_retry", AsyncMock(return_value=[]))
    approval_audit = {
        "approval_id": str(approval_id),
        "reviewed_by": "alice@example.com",
    }
    approval_gate = AsyncMock(return_value=approval_audit)
    monkeypatch.setattr(report, "consume_approved_action", approval_gate)

    result = await report.export_report(run_id, "markdown", approval_id)

    assert result["approval_audit"] == approval_audit
    assert "markdown" in result
    approval_gate.assert_awaited_once_with(
        approval_id=approval_id,
        run_id=run_id,
        action_type="export_report",
        expected_context={"format": "markdown"},
        consumer="report_export",
    )


@pytest.mark.asyncio
async def test_finalize_rejects_failed_candidate_before_approval_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    run_id = uuid4()
    person_id = uuid4()
    monkeypatch.setattr(
        review_orchestrator,
        "fetchrow_with_retry",
        AsyncMock(
            return_value={
                "run_id": run_id,
                "role_type": "default",
                "status": "active",
                "current_pass": "complete",
                "target_candidate_ids": [person_id],
            }
        ),
    )
    monkeypatch.setattr(
        review_orchestrator,
        "fetch_with_retry",
        AsyncMock(
            return_value=[
                {
                    "person_id": person_id,
                    "gate_status": "failed",
                    "gate_failures": [{"dimension": "skill_match"}],
                }
            ]
        ),
    )
    consume = AsyncMock()
    monkeypatch.setattr(review_orchestrator, "consume_approved_action", consume)

    result = await review_orchestrator.finalize_review_session(session_id, uuid4())

    assert result["status"] == "error"
    assert "assessment gates" in result["reason"]
    consume.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_requires_and_audits_human_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    approval_id = uuid4()
    run_id = uuid4()
    person_id = uuid4()
    fetchrow = AsyncMock(
        side_effect=[
            {
                "run_id": run_id,
                "role_type": "default",
                "status": "active",
                "current_pass": "complete",
                "target_candidate_ids": [person_id],
            },
            {
                "total_candidates": 1,
                "avg_score": 60,
                "max_score": 60,
                "min_score": 60,
                "excellent_count": 0,
                "good_count": 1,
                "poor_count": 0,
            },
        ]
    )
    fetch = AsyncMock(
        side_effect=[
            [{"person_id": person_id, "gate_status": "passed", "gate_failures": []}],
            [{"dimension": "skill_match", "avg_dim_score": 6.0, "count": 1}],
        ]
    )
    monkeypatch.setattr(review_orchestrator, "fetchrow_with_retry", fetchrow)
    monkeypatch.setattr(review_orchestrator, "fetch_with_retry", fetch)
    execute = AsyncMock()
    monkeypatch.setattr(review_orchestrator, "execute_with_retry", execute)
    approval_audit = {
        "approval_id": str(approval_id),
        "reviewed_by": "manager@example.com",
    }
    consume = AsyncMock(return_value=approval_audit)
    monkeypatch.setattr(review_orchestrator, "consume_approved_action", consume)

    result = await review_orchestrator.finalize_review_session(session_id, approval_id)

    assert result["status"] == "completed"
    assert result["human_approval"] == approval_audit
    consume.assert_awaited_once_with(
        approval_id=approval_id,
        run_id=run_id,
        action_type="finalize_review",
        expected_context={"session_id": str(session_id)},
        consumer="review_finalizer",
    )
    assert execute.await_args.args[2] == {"human_approval": approval_audit}


@pytest.mark.asyncio
async def test_sensitive_approval_request_requires_scoped_context() -> None:
    with pytest.raises(ValidationError, match="action_context is incomplete"):
        await outreach.request_human_approval(
            run_id=uuid4(),
            action_type="export_report",
            action_context={},
            agent_id="report_agent",
        )
