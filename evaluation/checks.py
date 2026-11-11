"""Small reference checks, with semantic judgment explicitly left for human review."""

from typing import Literal

from pydantic import BaseModel

from backend.app.models import AnalysisResult
from backend.app.quality import draft_action_claims, draft_closure_claims, draft_has_service_offer, draft_temporal_claims, label_consistency_fields, pr_only_recommendations, reproduction_claim_fields
from evaluation.snapshots import EvaluationCase


class Check(BaseModel):
    name: str
    status: Literal["passed", "failed", "not_scored"]
    detail: str


def check_result(case: EvaluationCase, result: AnalysisResult) -> list[Check]:
    checks: list[Check] = []

    def record(name: str, passed: bool | None, detail: str) -> None:
        status = "not_scored" if passed is None else "passed" if passed else "failed"
        checks.append(Check(name=name, status=status, detail=detail))

    content = result.content
    record("final_answer_accepted", content is not None, "A missing final answer is not a correct abstention.")
    for name, checker in (
        ("no_known_unperformed_maintainer_action", draft_action_claims),
        ("no_known_closure_resolution_inference", draft_closure_claims),
    ):
        problems = checker(content.draft_reply) if content else []
        record(name, not problems if content else None,
               f"Known draft action/status patterns: {problems}. A narrow check, not semantic or release verification.")
    temporal_claims = draft_temporal_claims(content.draft_reply) if content else []
    record(
        "draft_time_claims_attributed",
        not temporal_claims if content else None,
        f"Known unqualified fix/support assertions: {temporal_claims}. Attribution truth and answered questions still need source review.",
    )
    inconsistent_labels = label_consistency_fields(content) if content else []
    record(
        "label_decision_consistent",
        not inconsistent_labels if content else None,
        f"Known zero-recommendation claims conflict with a nonempty label list in: {inconsistent_labels}. This is not general semantic consistency validation.",
    )
    record(
        "maintainer_draft_voice",
        not draft_has_service_offer(content.draft_reply) if content else None,
        "Known assistant-style service offers are not suitable for a ready-to-post maintainer draft. This is a narrow pattern check.",
    )
    unsupported = reproduction_claim_fields(content) if content else []
    record(
        "no_first_person_reproduction_claim",
        not unsupported if content else None,
        f"Known English first-person reproduction forms found in: {unsupported}. This is a narrow pattern check, not semantic verification.",
    )
    record("similar_search_completed", bool(result.searches), "At least one successful keyword search is required.")
    record("label_catalog_loaded", result.labels_source_id is not None, "A skipped catalog is not an empty recommendation decision.")
    incompatible = pr_only_recommendations(content, result.repo_labels) if content else []
    record(
        "no_known_pr_only_label_recommendations",
        not incompatible if content and result.labels_source_id else None,
        f"Known PR-only label descriptions conflict with this issue target: {incompatible}. Other scope wording and relevance still need review.",
    )
    for expected in case.expected.comparisons:
        candidate = next((item for item in result.candidates if item.number == expected.issue_number), None)
        record(f"candidate_{expected.issue_number}_found", candidate is not None, "The reference candidate must be discovered by a model-selected query.")
        record(f"candidate_{expected.issue_number}_read", bool(candidate and candidate.source_id), "A search hit alone cannot establish the comparison.")
        comparison = next((item for item in content.comparisons if item.issue_number == expected.issue_number), None) if content else None
        observed = comparison.relationship if comparison else "not_compared"
        record(
            f"candidate_{expected.issue_number}_relationship",
            observed == expected.relationship if content else None,
            f"Expected {expected.relationship}; observed {observed}. Reason quality still needs review.",
        )
    if case.expected.no_candidates_expected:
        record("no_eligible_candidates", not result.candidates if result.searches else None, "This applies only to the fixed snapshot and recorded keyword queries.")
        record("no_comparisons", not content.comparisons if content else None, "The empty-search case should not invent a comparison.")
    names = {item.name.casefold() for item in content.label_recommendations} if content else set()
    required = {name.casefold() for name in case.expected.required_labels}
    allowed = {name.casefold() for name in case.expected.allowed_labels}
    record("required_labels_present", required <= names if content else None, f"Required additional labels: {sorted(required)}; observed: {sorted(names)}.")
    record("labels_within_reference", names <= allowed if content else None, f"Allowed additional labels: {sorted(allowed)}. This reference is pending human review.")
    if case.expected.missing_information_required:
        record("missing_questions_present", bool(content.missing_information) if content else None, "Question relevance and whether the details were already supplied need human review.")
    if content:
        source_ids = {source.id for source in result.sources}
        findings = [content.summary, *content.known_facts, *content.comparisons, *content.label_recommendations]
        record(
            "retrieved_citation_ids",
            all(item.source_ids and set(item.source_ids) <= source_ids for item in findings),
            "Retrieved source IDs do not prove that the cited text supports a claim.",
        )
    else:
        record("retrieved_citation_ids", None, "No accepted final answer to inspect.")
    return checks


def summarize_cases(cases: list[list[Check]]) -> dict[str, object]:
    """Count checklists without presenting a blended accuracy percentage."""
    counts = {"passed": 0, "failed": 0, "not_scored": 0}
    matched = 0
    for checks in cases:
        for check in checks:
            counts[check.status] += 1
        matched += bool(checks) and all(check.status == "passed" for check in checks)
    return {
        "cases_attempted": len(cases), "cases_meeting_automated_expectations": matched,
        "check_counts": counts, "human_review_status": "pending",
        "interpretation": "A development checklist on five fictional cases, not a production accuracy estimate.",
    }
