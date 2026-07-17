"""Outreach tools — draft generation, human approval gate.

All external-contact actions (email, LinkedIn message, etc.) must pass
through the human approval gate. These tools NEVER send messages
autonomously — they only prepare drafts and request approval.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from oculai_mcp.db.client import execute_with_retry, fetch_with_retry, fetchrow_with_retry
from oculai_mcp.tools.errors import AuthError, ConflictError, ValidationError

logger = logging.getLogger(__name__)

APPROVAL_ACTIONS = {
    "send_email": "Send email to candidate",
    "send_linkedin_message": "Send LinkedIn message",
    "send_linkedin_connection": "Send LinkedIn connection request",
    "send_wechat_message": "Send WeChat message",
    "create_calendar_invite": "Create calendar invitation",
    "export_shortlist": "Export candidate shortlist externally",
    "export_report": "Export a candidate sourcing report",
    "finalize_review": "Finalize automated candidate rankings",
}

# Decision authority is explicit and checked in domain code.  Agent/system
# identities are never accepted as reviewers.
_HUMAN_REVIEWER_ROLES = frozenset({
    # Generic role assigned only by the trusted desktop main-process IPC
    # boundary. It is deliberately not accepted from the renderer payload.
    "human_reviewer",
    "hiring_manager",
    "recruiting_manager",
    "hr_admin",
    "privacy_officer",
    "compliance_reviewer",
})
_ACTION_ALLOWED_ROLES: dict[str, frozenset[str]] = {
    "export_shortlist": frozenset({"human_reviewer", "hiring_manager", "recruiting_manager", "hr_admin", "privacy_officer"}),
    "export_report": frozenset({"human_reviewer", "hiring_manager", "recruiting_manager", "hr_admin", "privacy_officer"}),
    "finalize_review": frozenset({"human_reviewer", "hiring_manager", "recruiting_manager", "hr_admin", "compliance_reviewer"}),
}
_AUTOMATED_REVIEWER_IDS = frozenset({"system", "agent", "assistant", "assessment_engine"})
_REQUIRED_ACTION_CONTEXT: dict[str, frozenset[str]] = {
    "export_shortlist": frozenset({"scope"}),
    "export_report": frozenset({"format"}),
    "finalize_review": frozenset({"session_id"}),
}


async def create_outreach_draft(
    run_id: UUID,
    person_id: UUID,
    strategy: str,
    template: str = "standard",
    channel: str = "email",
    draft_content: str = "",
    subject: str = "",
    agent_id: str = "system",
) -> dict[str, Any]:
    """Create an outreach draft for a candidate.

    This only creates a draft — it does NOT send anything.
    The draft must be approved via oculai_request_approval before
    any message is sent.

    Args:
        run_id: The run UUID
        person_id: Target candidate Person UUID
        strategy: Outreach strategy (warm_intro, cold_email, linkedin_inmail, etc.)
        template: Template name to base the draft on
        channel: Contact channel (email, linkedin, wechat)
        draft_content: The draft message body
        subject: Email subject line (for email channel)
        agent_id: Agent creating the draft
    """
    # Verify candidate exists
    person = await fetchrow_with_retry(
        "SELECT canonical_name, latest_institution, latest_position FROM person WHERE person_id = $1",
        person_id,
    )
    if not person:
        return {"error": "person not found"}

    outreach_id = uuid4()
    content_preview = draft_content[:200] if draft_content else ""
    await execute_with_retry(
        """
        INSERT INTO outreachrecord
            (record_id, run_id, person_id, channel, strategy, subject,
             content_preview, content_full, template_id, status, created_by_agent, updated_by_agent)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'draft', $10, $10)
        """,
        outreach_id, run_id, person_id, channel, strategy,
        subject or "", content_preview, draft_content or "", template,
        agent_id,
    )

    logger.info(
        "Outreach draft %s created for person %s (channel=%s, strategy=%s)",
        outreach_id, person_id, channel, strategy,
    )

    return {
        "outreach_id": str(outreach_id),
        "person_id": str(person_id),
        "person_name": person["canonical_name"],
        "channel": channel,
        "strategy": strategy,
        "status": "draft",
        "requires_approval": True,
    }


async def request_human_approval(
    run_id: UUID,
    action_type: str,
    action_context: dict[str, Any],
    draft_content: str = "",
    agent_id: str = "system",
) -> dict[str, Any]:
    """Request human approval for an action that has external side effects.

    This is the GATE that all external actions must pass through:
    - Sending outreach messages
    - Exporting candidate data externally
    - Any write to external systems

    The action is blocked until a human approves via the database.

    Args:
        run_id: The run UUID
        action_type: Type of action needing approval (see APPROVAL_ACTIONS)
        action_context: Full context dict describing what, who, and why
        draft_content: The draft content to be approved
        agent_id: Agent requesting approval
    """
    if action_type not in APPROVAL_ACTIONS and not action_type.startswith("custom:"):
        return {
            "status": "error",
            "error": {
                "code": "unknown_action_type",
                "message": f"Unknown action type '{action_type}'. Valid types: {list(APPROVAL_ACTIONS.keys())} or 'custom:<name>'.",
            },
        }

    if not isinstance(action_context, dict):
        raise ValidationError("action_context must be an object")
    required_context = _REQUIRED_ACTION_CONTEXT.get(action_type, frozenset())
    missing_context = sorted(
        key for key in required_context if not action_context.get(key)
    )
    if missing_context:
        raise ValidationError(
            f"action_context is incomplete for {action_type}",
            details={"missing_context_fields": missing_context},
        )

    approval_id = uuid4()
    await execute_with_retry(
        """
        INSERT INTO humanapproval
            (approval_id, run_id, action_type, action_context, draft_content,
             status, requested_by, created_by_agent, updated_by_agent)
        VALUES ($1, $2, $3, $4, $5, 'pending', $6, $6, $6)
        """,
        approval_id, run_id, action_type, action_context, draft_content, agent_id,
    )

    logger.info(
        "Approval %s requested for action %s in run %s",
        approval_id, action_type, run_id,
    )

    return {
        "approval_id": str(approval_id),
        "run_id": str(run_id),
        "action_type": action_type,
        "action_label": APPROVAL_ACTIONS.get(action_type, action_type),
        "status": "pending",
        "message": "Human approval required before this action proceeds.",
    }


async def decide_human_approval(
    approval_id: UUID,
    decision: str,
    reviewer_id: str,
    reviewer_role: str,
    review_notes: str,
) -> dict[str, Any]:
    """Approve or deny a pending request with an auditable human identity.

    Only ``pending -> approved|denied`` is permitted. The reviewer must have
    an authorised human role and cannot be the same identity that requested
    the action, enforcing basic separation of duties.
    """
    normalized_decision = decision.strip().lower()
    if normalized_decision in {"approve", "approved"}:
        target_status = "approved"
    elif normalized_decision in {"deny", "denied"}:
        target_status = "denied"
    else:
        raise ValidationError("decision must be 'approve' or 'deny'")

    reviewer_id = reviewer_id.strip()
    reviewer_role = reviewer_role.strip().lower()
    if not reviewer_id or reviewer_id.lower() in _AUTOMATED_REVIEWER_IDS:
        raise AuthError("a named human reviewer_id is required")
    if reviewer_role not in _HUMAN_REVIEWER_ROLES:
        raise AuthError(
            "reviewer_role is not authorised to decide approvals",
            details={"allowed_roles": sorted(_HUMAN_REVIEWER_ROLES)},
        )
    if not review_notes or not review_notes.strip():
        raise ValidationError("review_notes are required for an auditable decision")

    current = await fetchrow_with_retry(
        """SELECT approval_id, status, action_type, requested_by
           FROM humanapproval WHERE approval_id = $1""",
        approval_id,
    )
    if not current:
        raise ValidationError("approval not found")
    if current["status"] != "pending":
        raise ConflictError(
            f"approval is already {current['status']}; only pending approvals can be decided"
        )
    if str(current["requested_by"]).strip().lower() == reviewer_id.lower():
        raise AuthError("approval requester cannot review their own request")

    allowed_roles = _ACTION_ALLOWED_ROLES.get(current["action_type"], _HUMAN_REVIEWER_ROLES)
    if reviewer_role not in allowed_roles:
        raise AuthError(
            f"role {reviewer_role!r} cannot decide action {current['action_type']!r}",
            details={"allowed_roles": sorted(allowed_roles)},
        )

    reviewed_at = datetime.now(timezone.utc)
    review_audit = {
        "approval_review": {
            "decision": target_status,
            "reviewer_id": reviewer_id,
            "reviewer_role": reviewer_role,
            "reviewed_at": reviewed_at.isoformat(),
        }
    }
    row = await fetchrow_with_retry(
        """
        UPDATE humanapproval
        SET status = $2,
            reviewed_by = $3,
            reviewed_at = $4,
            review_notes = $5,
            action_context = action_context || $6,
            updated_by_agent = $3
        WHERE approval_id = $1 AND status = 'pending'
        RETURNING approval_id, run_id, action_type, status, reviewed_by, reviewed_at
        """,
        approval_id,
        target_status,
        reviewer_id,
        reviewed_at,
        review_notes.strip(),
        review_audit,
    )
    if not row:
        raise ConflictError("approval state changed concurrently; decision was not applied")

    return {
        "approval_id": str(row["approval_id"]),
        "run_id": str(row["run_id"]),
        "action_type": row["action_type"],
        "status": row["status"],
        "reviewed_by": row["reviewed_by"],
        "reviewer_role": reviewer_role,
        "reviewed_at": str(row["reviewed_at"]),
    }


async def consume_approved_action(
    approval_id: UUID | None,
    run_id: UUID,
    action_type: str,
    expected_context: dict[str, Any],
    consumer: str,
) -> dict[str, Any]:
    """Atomically consume a scoped, approved action exactly once."""
    if approval_id is None:
        raise AuthError(
            f"human approval is required for {action_type}",
            details={"required_action_type": action_type, "expected_context": expected_context},
        )

    current = await fetchrow_with_retry(
        "SELECT * FROM humanapproval WHERE approval_id = $1",
        approval_id,
    )
    if not current:
        raise AuthError("approval not found")
    if current["run_id"] != run_id or current["action_type"] != action_type:
        raise AuthError("approval is not scoped to this run and action")
    if current["status"] != "approved" or not current.get("reviewed_by") or not current.get("reviewed_at"):
        raise AuthError("approval has not been approved by an authorised human")

    action_context = current.get("action_context") or {}
    context_mismatches = {
        key: {"expected": value, "actual": action_context.get(key)}
        for key, value in expected_context.items()
        if action_context.get(key) != value
    }
    if context_mismatches:
        raise AuthError(
            "approval context does not match the requested action",
            details={"context_mismatches": context_mismatches},
        )
    if action_context.get("consumed_at"):
        raise ConflictError("approval has already been consumed")

    consumed_at = datetime.now(timezone.utc)
    consumption_audit = {
        "consumed_at": consumed_at.isoformat(),
        "consumed_by": consumer,
        "consumed_for": action_type,
    }
    row = await fetchrow_with_retry(
        """
        UPDATE humanapproval
        SET action_context = action_context || $2,
            updated_by_agent = $3
        WHERE approval_id = $1
          AND status = 'approved'
          AND (action_context->>'consumed_at') IS NULL
        RETURNING approval_id, reviewed_by, reviewed_at
        """,
        approval_id,
        consumption_audit,
        consumer,
    )
    if not row:
        raise ConflictError("approval was consumed concurrently")
    return {
        "approval_id": str(row["approval_id"]),
        "reviewed_by": row["reviewed_by"],
        "reviewed_at": str(row["reviewed_at"]),
        "consumed_at": consumed_at.isoformat(),
    }


async def check_approval_status(approval_id: UUID) -> dict[str, Any]:
    """Check the status of a human approval request."""
    row = await fetchrow_with_retry(
        "SELECT * FROM humanapproval WHERE approval_id = $1", approval_id,
    )
    if not row:
        return {"error": "approval not found"}

    d = dict(row)
    for k in ("created_at", "updated_at", "reviewed_at"):
        if d.get(k):
            d[k] = str(d[k])
    for k in ("approval_id", "run_id"):
        if d.get(k):
            d[k] = str(d[k])
    context = d.get("action_context") or {}

    return {
        "approval_id": str(approval_id),
        "status": d["status"],
        "action_type": d["action_type"],
        "approved": d["status"] == "approved",
        "consumed": bool(context.get("consumed_at")),
        "details": d,
    }


async def list_pending_approvals(run_id: UUID | None = None) -> dict[str, Any]:
    """List all pending human approval requests.

    Args:
        run_id: Optional run UUID to filter by
    """
    if run_id:
        rows = await fetch_with_retry(
            "SELECT * FROM humanapproval WHERE status = 'pending' AND run_id = $1 ORDER BY created_at DESC",
            run_id,
        )
    else:
        rows = await fetch_with_retry(
            "SELECT * FROM humanapproval WHERE status = 'pending' ORDER BY created_at DESC",
        )

    approvals = []
    for r in rows:
        d = dict(r)
        for k in ("created_at", "updated_at", "reviewed_at"):
            if d.get(k):
                d[k] = str(d[k])
        approvals.append({
            "approval_id": str(d["approval_id"]),
            "run_id": str(d["run_id"]),
            "action_type": d["action_type"],
            "action_label": APPROVAL_ACTIONS.get(d["action_type"], d["action_type"]),
            "action_context": d.get("action_context") or {},
            "requested_by": d["requested_by"],
            "created_at": d["created_at"],
        })

    return {"pending_approvals": approvals, "count": len(approvals)}


async def get_outreach_history(
    person_id: UUID,
    limit: int = 50,
) -> dict[str, Any]:
    """Get outreach history for a candidate."""
    rows = await fetch_with_retry(
        """
        SELECT * FROM outreachrecord
        WHERE person_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        person_id, limit,
    )

    history = []
    for r in rows:
        d = dict(r)
        for k in ("created_at", "updated_at", "sent_at"):
            if d.get(k):
                d[k] = str(d[k])
        history.append(d)

    return {
        "person_id": str(person_id),
        "outreach_history": history,
        "count": len(history),
    }
