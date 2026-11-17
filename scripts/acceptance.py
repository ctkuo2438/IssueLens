"""Run three public issues through the current live agent and save review evidence."""

import asyncio
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Literal

from openai import AuthenticationError, RateLimitError
from pydantic import BaseModel, Field, model_validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agent import AgentRun, MAX_OUTPUT_TOKENS
from backend.app.config import Settings
from backend.app.models import AnalysisResult, EventKind, IssueTarget, ProgressEvent
from backend.app.quality import comment_claim_fields, draft_action_claims, draft_closure_claims, draft_has_service_offer, draft_temporal_claims, label_consistency_fields, pr_only_recommendations, reproduction_claim_fields


CASES_PATH = ROOT / "evaluation/live_acceptance_cases.json"
SELECTION_PATH = ROOT / "evaluation/live_acceptance_selection.json"
OUTPUT_ROOT = ROOT / "evidence/m2/acceptance"


class AcceptanceCase(BaseModel):
    id: str
    kind: Literal["complete_bug_report", "insufficient_information_report", "feature_request"]
    issue_url: str
    selection_reason: str
    review_checks: list[str] = Field(min_length=1)


class AcceptanceSet(BaseModel):
    id: str
    selected_at: str
    selection_source: str
    annotation_status: str
    scope: str
    cases: list[AcceptanceCase] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_cases(self) -> "AcceptanceSet":
        urls = [IssueTarget.from_url(case.issue_url).url for case in self.cases]
        if len(set(urls)) != 3 or len({case.id for case in self.cases}) != 3 or len({case.kind for case in self.cases}) != 3:
            raise ValueError("Select three distinct issues and one case of each kind.")
        return self


def functional_checks(result: AnalysisResult) -> dict[str, bool | None]:
    content = result.content
    sent = [report for report in result.context_builds if report.sent]
    similar = result.similar_issue_check
    return {
        "final_answer_accepted": content is not None,
        "no_known_unperformed_maintainer_action": not draft_action_claims(content.draft_reply) if content else None,
        "no_known_closure_resolution_inference": not draft_closure_claims(content.draft_reply) if content else None,
        "label_decision_consistent": not label_consistency_fields(content) if content else None,
        "target_retrieved": result.issue is not None,
        "same_repository_search_completed": bool(result.searches),
        "minimum_similar_issue_checks_completed": (
            similar.search_status == "empty" and similar.comparison_status == "not_needed"
            or similar.search_status == "candidates_found" and similar.candidate_status == "read" and similar.comparison_status == "compared"
        ) if content else None,
        "label_catalog_retrieved": result.labels_source_id is not None,
        "dispatched_contexts_within_budget": (
            len(sent) == result.usage.model_requests and all(
                report.fits and report.future_reserve_tokens >= report.future_reserve_minimum_tokens
                and report.estimated_tokens + report.repair_reserve_tokens + report.future_reserve_tokens <= report.budget_tokens
                for report in sent
            )
        ) if result.usage.model_requests else None,
        "known_first_person_reproduction_pattern_absent": not reproduction_claim_fields(content) if content else None,
        "known_assistant_service_offer_pattern_absent": not draft_has_service_offer(content.draft_reply) if content else None,
        "draft_time_claims_attributed": not draft_temporal_claims(content.draft_reply) if content else None,
        "reported_comments_first_page_attempted": 1 in result.comment_check.attempted_pages if result.issue and result.issue.comment_count > 0 else None,
        "known_comment_claim_patterns_supported": not comment_claim_fields(content, result.comment_check) if content else None,
        "no_known_pr_only_label_recommendations": not pr_only_recommendations(content, result.repo_labels) if content and result.labels_source_id else None,
    }


async def main() -> Path:
    # Review criteria remain in this runner, never in model input or tool responses.
    case_bytes = CASES_PATH.read_bytes()
    cases = AcceptanceSet.model_validate_json(case_bytes)
    selection_bytes = SELECTION_PATH.read_bytes()
    settings = Settings.from_env()
    if not settings.openai_api_key:
        raise SystemExit("Set OPENAI_API_KEY before running live acceptance. This command uses real model and GitHub APIs.")

    started_at = datetime.now(UTC)
    directory = OUTPUT_ROOT / started_at.strftime("%Y%m%dT%H%M%S_%fZ")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "cases.json").write_bytes(case_bytes)
    (directory / "selection.json").write_bytes(selection_bytes)
    code_paths = [f"backend/app/{name}.py" for name in ("agent", "github", "models", "config", "quality", "templates", "context")]
    code_paths.append("scripts/acceptance.py")
    reports: list[dict[str, object]] = []
    report: dict[str, object] = {
        "started_at": started_at.isoformat(), "finished_at": None,
        "model_api": "real", "github_data": "live_public_github", "model": settings.model,
        "reasoning_effort": settings.reasoning_effort, "max_output_tokens": MAX_OUTPUT_TOKENS,
        "github_authenticated": bool(settings.github_token),
        "case_manifest_sha256": hashlib.sha256(case_bytes).hexdigest(),
        "selection_sha256": hashlib.sha256(selection_bytes).hexdigest(),
        "code_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in code_paths},
        "limits_per_case": {
            "model_requests": settings.max_iterations, "tool_calls": settings.max_tool_calls,
            "runtime_seconds": settings.runtime_seconds, "context_tokens": settings.context_tokens,
            "candidates": settings.max_candidates, "candidate_reads": settings.max_candidate_reads,
        },
        "scope": cases.scope, "cases": reports, "cases_not_run": [case.id for case in cases.cases],
        "human_review_status": "pending", "stop_reason": None,
        "limitations": [
            "Three deliberately selected historical issues are a development acceptance exercise, not a representative quality benchmark.",
            "GitHub content can change between selection and investigation; results retain the bounded source excerpts actually retrieved.",
            "Only URLs enter AgentRun; selection notes and review criteria remain outside model input.",
            "Functional checks and accepted final JSON do not establish semantic correctness or completed human review.",
            "Reported token usage may omit failed or incomplete API requests. Tool calls are not HTTP request counts.",
        ],
    }
    path = directory / "report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Model API: real. GitHub: live public data. Human review: pending. Model: {settings.model}", flush=True)
    print(f"Report: {path}", flush=True)
    print(f"Reasoning effort: {settings.reasoning_effort or 'provider default'}. Output limit: {MAX_OUTPUT_TOKENS} tokens including reasoning and text.", flush=True)
    started = time.monotonic()
    for index, case in enumerate(cases.cases, start=1):
        print(f"[{index}/3] {case.id}: {case.issue_url}", flush=True)
        events: list[ProgressEvent] = []

        def emit(kind: EventKind, message: str) -> None:
            events.append(ProgressEvent(id=len(events) + 1, kind=kind, message=message))

        # No snapshot or custom transport: use the application's live GitHub tools.
        run = AgentRun(IssueTarget.from_url(case.issue_url), settings, emit)
        error_type: str | None = None
        stop_reason: str | None = None
        try:
            result = await asyncio.wait_for(run.run(), timeout=settings.runtime_seconds)
            outcome = "completed" if result.content and not result.limitations else "partial" if result.content else "no_final_answer"
        except TimeoutError:
            outcome, error_type = "timeout", "TimeoutError"
            result = run.snapshot("The live acceptance case runtime limit was reached.")
        except Exception as exc:
            outcome, error_type = "error", type(exc).__name__
            result = run.snapshot("The live acceptance case failed; only the exception type was recorded.")
            if isinstance(exc, (AuthenticationError, RateLimitError)):
                stop_reason = "A model credential or quota error stopped further cases."
        github_stop_errors = ("GitHub denied access", "GitHub rejected GITHUB_TOKEN", "GitHub rate limit was reached")
        if any(error in item for item in result.limitations for error in github_stop_errors):
            stop_reason = "A GitHub access or quota error stopped further cases."
        reports.append({
            "case_id": case.id, "kind": case.kind, "target_url": case.issue_url,
            "outcome": outcome, "error_type": error_type,
            "functional_checks": functional_checks(result),
            "human_review": {"status": "pending", "predeclared_checks": case.review_checks, "notes": ""},
            "result": result.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
        })
        report["cases_not_run"] = [item.id for item in cases.cases[index:]]
        report["stop_reason"] = stop_reason
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"  Outcome: {outcome}. Accepted answer: {result.content is not None}. Model requests: {result.usage.model_requests}. Tools: {result.usage.tool_calls}. Reported tokens: {result.usage.total_tokens}.", flush=True)
        if stop_reason:
            print(stop_reason, flush=True)
            break
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {path}", flush=True)
    return path


if __name__ == "__main__":
    if sys.argv[1:]:
        raise SystemExit("This live acceptance script takes no command-line arguments.")
    asyncio.run(main())
