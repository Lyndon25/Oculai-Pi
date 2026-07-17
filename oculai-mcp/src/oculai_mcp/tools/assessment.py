"""Candidate assessment — scoring, recording, shortlist generation.

Upgraded from naive AVG to confidence-weighted, role-type-aware scoring
with must-pass gate enforcement and full audit history.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from oculai_mcp.db.client import execute_with_retry, fetch_with_retry, fetchrow_with_retry
from oculai_mcp.tools.assessment_weights import ROLE_WEIGHTS, get_gates, get_weights
from oculai_mcp.tools.errors import ValidationError

logger = logging.getLogger(__name__)

# Evidence types relevant to each assessment dimension.
# Used to filter evidence when validating tier requirements per-dimension.
_DIMENSION_EVIDENCE_TYPES: dict[str, frozenset[str]] = {
    "academic": frozenset({"paper", "conference_talk", "patent", "award", "reference"}),
    "engineering": frozenset({"code", "patent", "paper", "certification", "blog_post"}),
    "leadership": frozenset({"reference", "interview", "award", "email"}),
    "communication": frozenset({"conference_talk", "blog_post", "social_media", "interview", "web_page"}),
    "culture_fit": frozenset({"reference", "interview", "social_media", "email"}),
    "skill_match": frozenset({"paper", "code", "certification", "patent", "profile", "web_page"}),
    "location": frozenset({"profile", "web_page"}),
    "career_stage": frozenset({"profile", "paper", "patent", "reference", "web_page"}),
    "mobility": frozenset({"profile", "web_page", "social_media", "interview"}),
    "overall": frozenset(),  # overall dimension accepts any evidence type
}


async def score_candidate(
    run_id: UUID,
    person_id: UUID,
    dimensions: dict[str, float],
    assessor_agent: str,
    evidence_ids: list[str] | None = None,
    confidence: float = 1.0,
    rationale: str = "",
    role_type: str = "default",
) -> dict[str, Any]:
    """Score a candidate across multiple dimensions. Each dimension is scored.

    Upgraded behavior:
    - Validates evidence tier requirements before storing
    - Tracks score history when scores change
    - Computes confidence-weighted overall with role-type weights
    - Enforces must-pass gates (failure caps overall at 5.0)
    """
    _validate_assessment_input(dimensions, confidence, role_type)
    try:
        parsed_evidence_ids = [UUID(e) for e in evidence_ids] if evidence_ids else []
    except (TypeError, ValueError) as exc:
        raise ValidationError("evidence_ids must contain valid UUIDs") from exc
    await _validate_evidence_references(run_id, person_id, parsed_evidence_ids)
    assessment_ids = []
    validation_issues: list[dict[str, Any]] = []

    for dim, score in dimensions.items():
        # Validate evidence requirements — filter evidence relevant to this dimension
        dim_evidence_ids = await _filter_evidence_for_dimension(
            parsed_evidence_ids, dim, run_id, person_id
        )
        validation = await validate_evidence_for_score(
            run_id=run_id,
            person_id=person_id,
            dimension=dim,
            score=score,
            evidence_ids=dim_evidence_ids,
        )
        if not validation["valid"]:
            validation_issues.append(validation)

    # Reject the complete batch before the first write. This avoids partially
    # persisted assessments and makes evidence requirements a hard constraint.
    if validation_issues:
        raise ValidationError(
            "assessment rejected because evidence requirements were not met",
            details={"validation_issues": validation_issues},
        )

    for dim, score in dimensions.items():
        # Check if this dimension already has a score (for history tracking)
        existing = await fetchrow_with_retry(
            """
            SELECT score, confidence FROM candidateassessment
            WHERE run_id = $1 AND person_id = $2 AND assessor_agent = $3 AND dimension = $4
            """,
            run_id, person_id, assessor_agent, dim,
        )

        row = await fetchrow_with_retry(
            """
            INSERT INTO candidateassessment
                (run_id, person_id, assessor_agent, dimension, score, confidence, rationale, evidence_ids)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (run_id, person_id, assessor_agent, dimension)
            DO UPDATE SET score = EXCLUDED.score, confidence = EXCLUDED.confidence,
                          rationale = EXCLUDED.rationale, evidence_ids = EXCLUDED.evidence_ids,
                          updated_at = now()
            RETURNING assessment_id
            """,
            run_id, person_id, assessor_agent, dim, score, confidence, rationale,
            parsed_evidence_ids,
        )
        if row:
            assessment_ids.append(str(row["assessment_id"]))

            # Track score history if changed
            if existing and (existing["score"] != score or existing["confidence"] != confidence):
                await execute_with_retry(
                    """
                    INSERT INTO assessmentscorehistory
                        (run_id, person_id, dimension, previous_score, new_score,
                         previous_confidence, new_confidence, assessor_agent, change_reason, evidence_snapshot)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    run_id, person_id, dim,
                    existing["score"], score,
                    existing["confidence"], confidence,
                    assessor_agent,
                    f"Score updated via score_candidate: {rationale[:200]}",
                    {"evidence_ids": [str(e) for e in parsed_evidence_ids]},
                )

    # Compute and update overall score on CandidateRecord
    result = await _compute_overall_score(person_id, run_id, role_type=role_type)

    return {
        "person_id": str(person_id),
        "assessment_ids": assessment_ids,
        "dimensions_scored": list(dimensions.keys()),
        "overall_score": result["overall_score"],
        "gate_status": result["gate_status"],
        "gate_failures": result["gate_failures"],
        "validation_issues": validation_issues,
    }


async def record_assessment(
    run_id: UUID,
    person_id: UUID,
    assessor_agent: str,
    dimension: str,
    score: float,
    confidence: float = 1.0,
    rationale: str = "",
    evidence_ids: list[str] | None = None,
    role_type: str = "default",
) -> dict[str, Any]:
    """Record a single dimension assessment.

    Upgraded behavior:
    - Tracks score history when scores change
    - Re-computes overall with role-type weights
    """
    _validate_assessment_input({dimension: score}, confidence, role_type)
    try:
        parsed_evidence_ids = [UUID(e) for e in evidence_ids] if evidence_ids else []
    except (TypeError, ValueError) as exc:
        raise ValidationError("evidence_ids must contain valid UUIDs") from exc
    await _validate_evidence_references(run_id, person_id, parsed_evidence_ids)
    relevant_evidence_ids = await _filter_evidence_for_dimension(
        parsed_evidence_ids, dimension, run_id, person_id
    )
    validation = await validate_evidence_for_score(
        run_id=run_id,
        person_id=person_id,
        dimension=dimension,
        score=score,
        evidence_ids=relevant_evidence_ids,
    )
    if not validation["valid"]:
        raise ValidationError(
            "assessment rejected because evidence requirements were not met",
            details={"validation_issues": [validation]},
        )

    # Check existing for history tracking
    existing = await fetchrow_with_retry(
        """
        SELECT score, confidence FROM candidateassessment
        WHERE run_id = $1 AND person_id = $2 AND assessor_agent = $3 AND dimension = $4
        """,
        run_id, person_id, assessor_agent, dimension,
    )

    row = await fetchrow_with_retry(
        """
        INSERT INTO candidateassessment (run_id, person_id, assessor_agent, dimension, score, confidence, rationale, evidence_ids)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (run_id, person_id, assessor_agent, dimension)
        DO UPDATE SET score = EXCLUDED.score, confidence = EXCLUDED.confidence,
                      rationale = EXCLUDED.rationale, evidence_ids = EXCLUDED.evidence_ids,
                      updated_at = now()
        RETURNING assessment_id
        """,
        run_id, person_id, assessor_agent, dimension, score, confidence, rationale,
        parsed_evidence_ids,
    )
    assessment_id = str(row["assessment_id"]) if row else None

    # Track history if changed
    if existing and (existing["score"] != score or existing["confidence"] != confidence):
        await execute_with_retry(
            """
            INSERT INTO assessmentscorehistory
                (run_id, person_id, dimension, previous_score, new_score,
                 previous_confidence, new_confidence, assessor_agent, change_reason, evidence_snapshot)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            run_id, person_id, dimension,
            existing["score"], score,
            existing["confidence"], confidence,
            assessor_agent,
            f"Score updated via record_assessment: {rationale[:200]}",
            {"evidence_ids": [str(e) for e in parsed_evidence_ids]},
        )

    # Recompute overall
    result = await _compute_overall_score(person_id, run_id, role_type=role_type)

    return {
        "assessment_id": assessment_id,
        "person_id": str(person_id),
        "dimension": dimension,
        "score": score,
        "overall_score": result["overall_score"],
        "gate_status": result["gate_status"],
    }


async def get_shortlist(
    run_id: UUID,
    min_score: float = 0,
    limit: int = 20,
) -> dict[str, Any]:
    """Get shortlisted candidates ranked by overall quality score.

    Returns dimension scores with confidence from the assessment engine.
    """
    rows = await fetch_with_retry(
        """
        SELECT cr.record_id, cr.person_id, cr.status, cr.quality_score, cr.match_scores,
               p.canonical_name, p.latest_institution, p.h_index, p.total_citations, p.total_papers
        FROM candidaterecord cr
        JOIN person p ON p.person_id = cr.person_id
        WHERE cr.run_id = $1
          AND (cr.quality_score >= $2 OR $2 = 0)
          AND cr.match_scores->>'gate_status' = 'passed'
        ORDER BY cr.quality_score DESC
        LIMIT $3
        """,
        run_id, int(min_score * 10), limit,
    )

    shortlist = []
    for r in rows:
        # Get dimension scores
        scores = await fetch_with_retry(
            "SELECT dimension, score, confidence FROM candidateassessment WHERE run_id = $1 AND person_id = $2",
            run_id, r["person_id"],
        )
        # Get gate status from match_scores if available
        gate_status = "unknown"
        match_scores = r.get("match_scores") or {}
        if isinstance(match_scores, dict):
            gate_status = match_scores.get("gate_status", "unknown")

        shortlist.append({
            "record_id": str(r["record_id"]),
            "person_id": str(r["person_id"]),
            "name": r["canonical_name"],
            "institution": r["latest_institution"],
            "h_index": r["h_index"],
            "total_citations": r["total_citations"],
            "total_papers": r["total_papers"],
            "overall_score": r["quality_score"] / 10.0 if r["quality_score"] else 0.0,
            "gate_status": gate_status,
            "dimensions": {s["dimension"]: {"score": s["score"], "confidence": s["confidence"]} for s in scores},
        })

    return {"run_id": str(run_id), "shortlist": shortlist, "count": len(shortlist)}


async def get_score_history(
    run_id: UUID,
    person_id: UUID | None = None,
    dimension: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Get score history for auditing."""
    query = "SELECT * FROM assessmentscorehistory WHERE run_id = $1"
    args: list[Any] = [run_id]
    if person_id:
        query += " AND person_id = $2"
        args.append(person_id)
    if dimension:
        query += f" AND dimension = ${len(args) + 1}"
        args.append(dimension)
    query += " ORDER BY changed_at DESC LIMIT $"
    query += str(len(args) + 1)
    args.append(limit)

    rows = await fetch_with_retry(query, *args)
    return {
        "run_id": str(run_id),
        "history": [dict(r) for r in rows],
        "count": len(rows),
    }


async def _compute_overall_score(person_id: UUID, run_id: UUID, role_type: str = "default") -> dict[str, Any]:
    """Compute confidence-weighted overall score from dimension assessments.

    Uses role-type-specific weights and enforces must-pass gates.
    Gate failures cap overall score at 5.0 regardless of weighted average.
    """
    if role_type not in ROLE_WEIGHTS:
        logger.warning("Unknown role_type '%s' — falling back to 'default' weights", role_type)

    weights = get_weights(role_type)
    gates = get_gates(role_type)

    rows = await fetch_with_retry(
        "SELECT dimension, score, confidence, evidence_ids FROM candidateassessment WHERE run_id=$1 AND person_id=$2",
        run_id, person_id,
    )

    weighted_sum = 0.0
    gate_failures: list[dict[str, Any]] = []
    dim_scores: dict[str, dict[str, float]] = {}
    by_dimension: dict[str, list[tuple[float, float]]] = {}

    for r in rows:
        dim = r["dimension"]
        if dim not in weights:
            # Unknown dimension — skip from weighted average to avoid inflating the total
            logger.warning("Dimension '%s' has no weight in role_type '%s' — skipping", dim, role_type)
            continue

        score = float(r["score"])
        conf = float(r["confidence"] if r["confidence"] is not None else 0.5)
        by_dimension.setdefault(dim, []).append((score, conf))

    # Aggregate multiple assessors once per dimension. Missing dimensions and
    # low confidence reduce the score instead of being normalised away.
    for dim, assessments in by_dimension.items():
        total_confidence = sum(conf for _, conf in assessments)
        if total_confidence > 0:
            score = sum(score * conf for score, conf in assessments) / total_confidence
            conf = min(1.0, total_confidence / len(assessments))
        else:
            score = sum(score for score, _ in assessments) / len(assessments)
            conf = 0.0
        weighted_sum += score * weights[dim] * conf
        dim_scores[dim] = {"score": round(score, 2), "confidence": round(conf, 3)}

    # Missing required dimensions are hard gate failures.
    for dim, required in gates.items():
        aggregate = dim_scores.get(dim)
        if aggregate is None:
            gate_failures.append({
                "dimension": dim,
                "required": required,
                "actual": None,
                "reason": "required_dimension_missing",
            })
        elif aggregate["score"] < required:
            gate_failures.append({
                "dimension": dim,
                "required": required,
                "actual": aggregate["score"],
                "reason": "below_required_score",
            })

    # Role weights sum to one, so this is the full-profile weighted score.
    overall = round(weighted_sum, 1)
    assessed_weight = sum(weights[dim] for dim in by_dimension)

    # Gate failure caps score at 5.0
    if gate_failures:
        overall = min(overall, 5.0)
        gate_status = "failed"
    else:
        gate_status = "passed"

    # Store computed breakdown in CandidateRecord.match_scores JSONB
    breakdown = {
        "overall_score": overall,
        "role_type": role_type,
        "dimension_scores": dim_scores,
        "gate_status": gate_status,
        "gate_failures": gate_failures,
        "confidence_adjusted": True,
        "normalization": "full_profile_weight",
        "assessment_coverage": round(assessed_weight, 3),
        "missing_dimensions": sorted(set(weights) - set(by_dimension)),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    await execute_with_retry(
        """UPDATE candidaterecord
           SET quality_score = $3,
               match_scores = $4,
               updated_at = now(),
               updated_by_agent = 'assessment_engine'
           WHERE run_id = $1 AND person_id = $2""",
        run_id, person_id, int(overall * 10), breakdown,
    )

    return {
        "overall_score": overall,
        "gate_status": gate_status,
        "gate_failures": gate_failures,
    }


async def _filter_evidence_for_dimension(
    evidence_ids: list[UUID],
    dimension: str,
    run_id: UUID,
    person_id: UUID,
) -> list[UUID]:
    """Filter evidence IDs to only those relevant to the given assessment dimension.

    Evidence types that don't match the dimension are excluded from tier validation.
    The 'overall' dimension accepts all evidence types.
    """
    if not evidence_ids:
        return []

    relevant_types = _DIMENSION_EVIDENCE_TYPES.get(dimension)
    if relevant_types is None or len(relevant_types) == 0:
        # Unknown dimension or 'overall' — accept all evidence
        return evidence_ids

    # Query evidence types for the given IDs
    rows = await fetch_with_retry(
        """SELECT evidence_id, evidence_type FROM evidence
           WHERE evidence_id = ANY($1) AND run_id = $2 AND person_id = $3""",
        evidence_ids, run_id, person_id,
    )
    type_map = {r["evidence_id"]: r["evidence_type"] for r in rows}

    filtered = [eid for eid in evidence_ids
                if type_map.get(eid) in relevant_types]
    return filtered


async def validate_evidence_for_score(
    run_id: UUID,
    person_id: UUID,
    dimension: str,
    score: float,
    evidence_ids: list[UUID],
) -> dict[str, Any]:
    """Validate that scores meet evidence tier requirements.

    Rules (from Fit Evaluator agent prompt):
    - score >= 7: at least 1 Tier 1 evidence item
    - score >= 5: at least 1 Tier 1 or Tier 2 evidence item
    - confidence < 0.5: must be flagged
    """
    if score >= 7.0 and not evidence_ids:
        return {"valid": False, "reason": "Score >= 7 requires at least 1 Tier 1 evidence item", "required_tier": 1}

    if score >= 5.0 and not evidence_ids:
        return {"valid": False, "reason": "Score >= 5 requires at least 1 Tier 1 or Tier 2 evidence item", "required_tier": 2}

    if evidence_ids:
        rows = await fetch_with_retry(
            """SELECT evidence_id, tier FROM evidence
               WHERE evidence_id = ANY($1) AND run_id = $2 AND person_id = $3""",
            evidence_ids, run_id, person_id,
        )
        tiers = {r["evidence_id"]: r["tier"] for r in rows}
        tier_1_count = sum(1 for t in tiers.values() if t == 1)
        tier_2_count = sum(1 for t in tiers.values() if t == 2)

        if score >= 7.0 and tier_1_count == 0:
            return {
                "valid": False,
                "reason": f"Score >= 7 requires Tier 1 evidence, found {tier_1_count} Tier 1",
                "required_tier": 1,
                "found_tiers": {str(k): v for k, v in tiers.items()},
            }
        if score >= 5.0 and tier_1_count + tier_2_count == 0:
            return {
                "valid": False,
                "reason": "Score >= 5 requires Tier 1/2 evidence, found none",
                "required_tier": 2,
                "found_tiers": {str(k): v for k, v in tiers.items()},
            }

    return {"valid": True}


def _validate_assessment_input(
    dimensions: dict[str, float], confidence: float, role_type: str
) -> None:
    """Validate assessment values before any database interaction."""
    if role_type not in ROLE_WEIGHTS:
        raise ValidationError(
            f"unknown role_type {role_type!r}; must be one of {sorted(ROLE_WEIGHTS)}"
        )
    if not dimensions:
        raise ValidationError("at least one assessment dimension is required")
    unknown = sorted(set(dimensions) - set(ROLE_WEIGHTS[role_type]))
    if unknown:
        raise ValidationError(
            "unknown assessment dimensions",
            details={"unknown_dimensions": unknown},
        )
    invalid_scores = {
        dim: score
        for dim, score in dimensions.items()
        if not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not 0 <= score <= 10
    }
    if invalid_scores:
        raise ValidationError(
            "assessment scores must be numeric values between 0 and 10",
            details={"invalid_scores": invalid_scores},
        )
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise ValidationError("confidence must be a numeric value between 0 and 1")


async def _validate_evidence_references(
    run_id: UUID, person_id: UUID, evidence_ids: list[UUID]
) -> None:
    """Reject missing or cross-run/person evidence references."""
    if not evidence_ids:
        return
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValidationError("evidence_ids must not contain duplicates")

    rows = await fetch_with_retry(
        """SELECT evidence_id, run_id, person_id FROM evidence
           WHERE evidence_id = ANY($1)""",
        evidence_ids,
    )
    by_id = {row["evidence_id"]: row for row in rows}
    missing = [str(evidence_id) for evidence_id in evidence_ids if evidence_id not in by_id]
    mismatched = [
        {
            "evidence_id": str(evidence_id),
            "actual_run_id": str(by_id[evidence_id]["run_id"]),
            "actual_person_id": str(by_id[evidence_id]["person_id"]),
        }
        for evidence_id in evidence_ids
        if evidence_id in by_id
        and (
            by_id[evidence_id]["run_id"] != run_id
            or by_id[evidence_id]["person_id"] != person_id
        )
    ]
    if missing or mismatched:
        raise ValidationError(
            "evidence_ids must exist and belong to the assessed run/person",
            details={"missing_evidence_ids": missing, "mismatched_evidence": mismatched},
        )
