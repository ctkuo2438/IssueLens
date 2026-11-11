"""Validate analysis evidence, comparisons, labels and maintainer drafts."""

from backend.app.github import GitHubTools
from backend.app.models import AnalysisContent
from backend.app.quality import (
    comment_claim_fields, draft_action_claims, draft_closure_claims, draft_has_service_offer,
    draft_temporal_claims, is_pr_only_label, label_consistency_fields, reproduction_claim_fields,
)


def _validate_comparisons(content: AnalysisContent, tools: GitHubTools) -> None:
    """Accept comparisons only when both issues were actually retrieved."""
    target_source = next(source.id for source in tools.sources.values() if source.kind == "issue")
    compared: set[int] = set()
    for comparison in content.comparisons:
        candidate = tools.candidates.get(comparison.issue_number)
        if candidate is None or candidate.source_id is None:
            raise ValueError("Read each compared candidate with get_related_issue first.")
        if comparison.issue_number in compared:
            raise ValueError("Return only one comparison per candidate.")
        if not comparison.reason.strip():
            raise ValueError("Each comparison needs a concrete reason.")
        if target_source not in comparison.source_ids or candidate.source_id not in comparison.source_ids:
            raise ValueError(
                f"Comparison #{comparison.issue_number} must cite both the target and full candidate sources. "
                f"Required IDs: {[target_source, candidate.source_id]}; received: {comparison.source_ids}."
            )
        if any(source_id not in tools.sources for source_id in comparison.source_ids):
            raise ValueError("The comparison cited a source that was not retrieved.")
        if comparison.relationship == "possible_duplicate" and not any(item.strip() for item in comparison.similarities):
            raise ValueError("A possible duplicate needs concrete similarities.")
        if comparison.relationship == "related_but_different" and not any(item.strip() for item in comparison.differences):
            raise ValueError("Related but different issues need concrete differences.")
        compared.add(comparison.issue_number)
    missing = {candidate.number for candidate in tools.candidates.values() if candidate.source_id} - compared
    if missing:
        raise ValueError(
            f"Include a grounded comparison for each retrieved candidate body; missing: {sorted(missing)}. "
            "Use insufficient_evidence when the retrieved bodies do not establish a relationship."
        )


def _validate_label_recommendations(content: AnalysisContent, tools: GitHubTools) -> None:
    """Restrict suggestions to retrieved, unapplied catalog labels and grounded citations."""
    assert tools.issue is not None
    if len(content.label_recommendations) > 3:
        raise ValueError("Recommend at most three additional labels.")
    target_source = next(source.id for source in tools.sources.values() if source.kind == "issue")
    existing = {name.casefold() for name in tools.issue.labels}
    seen: set[str] = set()
    for recommendation in content.label_recommendations:
        name = recommendation.name.casefold()
        label = tools.repo_labels.get(name)
        if tools.labels_source_id is None or label is None:
            raise ValueError("Recommend only active names actually retrieved with get_repo_labels.")
        if name in existing:
            raise ValueError("Do not recommend labels already applied to the target issue.")
        if name in seen:
            raise ValueError("Do not repeat a label recommendation.")
        if not recommendation.reason.strip():
            raise ValueError("Each label recommendation needs a concrete reason.")
        if target_source not in recommendation.source_ids or tools.labels_source_id not in recommendation.source_ids:
            raise ValueError(
                f"Label {label.name!r} must cite the target issue and label catalog sources. "
                f"Required IDs: {[target_source, tools.labels_source_id]}; received: {recommendation.source_ids}."
            )
        if any(source_id not in tools.sources for source_id in recommendation.source_ids):
            raise ValueError("The label recommendation cited a source that was not retrieved.")
        if is_pr_only_label(label.description):
            raise ValueError(
                f"Label {label.name!r} is scoped to pull requests by its catalog description; "
                "do not recommend it for an issue. Remove it and keep only supported "
                "issue-applicable labels, or return an empty label_recommendations list."
            )
        recommendation.name = label.name
        seen.add(name)


def validate_analysis(content: AnalysisContent, tools: GitHubTools, visible_source_ids: set[str]) -> None:
    """Reject unsupported output before the agent accepts a final answer."""
    findings = [content.summary, *content.known_facts]
    if any(not finding.text.strip() or not finding.source_ids for finding in findings):
        raise ValueError("A factual claim had no supporting citation.")
    if any(source_id not in tools.sources for finding in findings for source_id in finding.source_ids):
        raise ValueError("The model cited a source that was not retrieved.")
    cited = [source_id for finding in findings for source_id in finding.source_ids]
    cited.extend(source_id for item in content.comparisons for source_id in item.source_ids)
    cited.extend(source_id for item in content.label_recommendations for source_id in item.source_ids)
    if len(content.missing_information) > 3:
        raise ValueError("The model requested more than three missing details.")
    _validate_comparisons(content, tools)
    _validate_label_recommendations(content, tools)
    inconsistent_labels = label_consistency_fields(content)
    if inconsistent_labels:
        raise ValueError(
            f"Label-decision text conflicts with nonempty label_recommendations in {', '.join(inconsistent_labels)}. "
            f"Current recommended labels: {[item.name for item in content.label_recommendations]}. "
            "Reassess the evidence and make all prose agree with the list. Keep supported "
            "recommendations and state actual limitations; return an empty list only if none is justified."
        )
    if any(source_id not in visible_source_ids for source_id in cited):
        raise ValueError("Cite only evidence included in model input; an omitted excerpt cannot support factual claims.")
    comment_claims = comment_claim_fields(
        content, tools.comment_check, visible_source_ids=visible_source_ids,
    )
    if comment_claims:
        raise ValueError(
            f"Unsupported comment absence or complete-read claim in {', '.join(comment_claims)}. "
            f"Current comment status is {tools.comment_check.status}. "
            "Distinguish unread, failed, partial and successful empty checks."
        )
    unsupported = reproduction_claim_fields(content)
    if unsupported:
        raise ValueError(
            f"Unsupported first-person reproduction claim in {', '.join(unsupported)}. "
            "Only read-only tools were used. Attribute reported reproduction to the reporter "
            "in third person, including when paraphrasing a quote. No reproduction was executed."
        )
    if draft_has_service_offer(content.draft_reply):
        raise ValueError(
            "The draft contains an assistant-style service offer. Rewrite it as a "
            "maintainer comment addressed to the issue author. State the concrete next "
            "step directly, without offering to generate another plan, answer or checklist."
        )
    draft_claims = [*draft_action_claims(content.draft_reply), *draft_closure_claims(content.draft_reply)]
    if draft_claims:
        raise ValueError(
            f"The draft has known unsupported action/status claims: {draft_claims}. "
            "Tools only read; no tracker action was performed. Attribute historical actions "
            "to their source. Closed does not establish a fix, implementation or release; "
            "leave those unknown without retrieved evidence."
        )
    temporal_claims = draft_temporal_claims(content.draft_reply)
    if temporal_claims:
        raise ValueError(
            f"The draft has known unqualified time/capability assertions: {temporal_claims}. "
            "Attribute historical plans or reported capability to the retrieved source, "
            "use past tense, and disclose that current implementation/release status was not checked. "
            "Before drafting a question, check whether the issue or visible comments already answer it."
        )
