"""Run five fixed fictional GitHub snapshots through the real model and save a local report."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sys
import time

import httpx
from openai import AuthenticationError, RateLimitError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agent import AgentRun, MAX_OUTPUT_TOKENS
from backend.app.config import Settings
from backend.app.models import EventKind, ProgressEvent
from evaluation.checks import Check, check_result, summarize_cases
from evaluation.snapshots import Dataset, SnapshotGitHub


DATASET_PATH = ROOT / "evaluation" / "cases.json"
OUTPUT_ROOT = ROOT / "evidence" / "evaluations"


async def main() -> Path:
    # Validate references before reading credentials or starting any model request.
    dataset_bytes = DATASET_PATH.read_bytes()
    dataset = Dataset.model_validate_json(dataset_bytes)
    settings = replace(Settings.from_env(), github_token="")
    if not settings.openai_api_key:
        raise SystemExit("Set OPENAI_API_KEY before running the real-model evaluation. Offline checks are in tests/test_evaluation.py.")

    started_at = datetime.now(UTC)
    directory = OUTPUT_ROOT / started_at.strftime("%Y%m%dT%H%M%S_%fZ")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "cases.json").write_bytes(dataset_bytes)
    path = directory / "report.json"
    agent_hash = hashlib.sha256()
    for filename in (
        "agent.py", "agent_prompt.py", "analysis_validation.py", "github.py",
        "models.py", "config.py", "quality.py", "templates.py", "context.py",
    ):
        agent_hash.update(filename.encode() + b"\0" + (ROOT / "backend" / "app" / filename).read_bytes())
    evaluation_hash = hashlib.sha256()
    for filename in ("evaluation/checks.py", "evaluation/snapshots.py", "scripts/evaluate.py"):
        evaluation_hash.update(filename.encode() + b"\0" + (ROOT / filename).read_bytes())
    case_reports: list[dict[str, object]] = []
    case_checks: list[list[Check]] = []
    report: dict[str, object] = {
        "started_at": started_at.isoformat(), "finished_at": None,
        "model_api": "real", "github_data": "fixed_fictional_snapshots",
        "model": settings.model, "dataset_id": dataset.id, "dataset_version": dataset.version,
        "reasoning_effort": settings.reasoning_effort, "max_output_tokens": MAX_OUTPUT_TOKENS,
        "annotation_status": dataset.annotation_status,
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "agent_code_sha256": agent_hash.hexdigest(), "evaluation_code_sha256": evaluation_hash.hexdigest(),
        "search_behavior": "Case-insensitive AND-substring matching on snapshot titles and bodies; not GitHub search or ranking.",
        "limits_per_case": {
            "model_requests": settings.max_iterations, "tool_calls": settings.max_tool_calls,
            "runtime_seconds": settings.runtime_seconds, "context_tokens": settings.context_tokens,
            "candidates": settings.max_candidates, "candidate_reads": settings.max_candidate_reads,
        },
        "cases_expected": len(dataset.cases), "cases": case_reports,
        "cases_not_run": [case.id for case in dataset.cases],
        "summary": summarize_cases([]), "stop_reason": None,
        "usage": {"model_requests": 0, "tool_calls": 0, "reported_total_tokens": 0},
        "limitations": [
            "Five assistant-authored fictional cases are a starting regression set, not a representative benchmark.",
            "Reference expectations are hidden from the agent and remain pending human review.",
            "Automatic checks do not establish factual support, reason quality or production search recall.",
            "Real model requests use API quota. GitHub is not contacted; snapshot source URLs are fictional.",
            "Token totals include only usage returned by completed API responses; failed requests may be unreported.",
        ],
    }
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Model API: real. GitHub data: fixed fictional snapshots. Human review: pending.", flush=True)
    print(f"Model: {settings.model}. Cases: {len(dataset.cases)}. Report: {path}", flush=True)
    print(f"Reasoning effort: {settings.reasoning_effort or 'provider default'}. Output limit: {MAX_OUTPUT_TOKENS} tokens including reasoning and text.", flush=True)

    total_requests = total_tools = total_tokens = accepted = 0
    started = time.monotonic()
    for index, case in enumerate(dataset.cases, start=1):
        print(f"[{index}/{len(dataset.cases)}] {case.id}", flush=True)
        events: list[ProgressEvent] = []

        def emit(kind: EventKind, message: str) -> None:
            events.append(ProgressEvent(id=len(events) + 1, kind=kind, message=message))

        # Only evidence enters the transport; expectations stay outside the agent loop.
        snapshot = SnapshotGitHub(case.snapshot, dataset.labels)
        run = AgentRun(case.snapshot.target, settings, emit, github_transport=httpx.MockTransport(snapshot.respond))
        error_type: str | None = None
        stop_reason: str | None = None
        try:
            result = await asyncio.wait_for(run.run(), timeout=settings.runtime_seconds)
            outcome = "completed" if result.content and not result.limitations else "partial" if result.content else "no_final_answer"
        except TimeoutError:
            error_type = "TimeoutError"
            outcome = "timeout"
            result = run.snapshot("The evaluation case runtime limit was reached.")
        except Exception as exc:
            # Do not serialize upstream errors, HTTP headers, response bodies or credentials.
            error_type = type(exc).__name__
            outcome = "model_error"
            result = run.snapshot("The evaluation case failed; only the exception type was recorded.")
            if isinstance(exc, (AuthenticationError, RateLimitError)):
                stop_reason = "A credential or rate-limit error stopped the suite. Remaining cases were not run."
        checks = check_result(case, result)
        case_checks.append(checks)
        accepted += result.content is not None
        total_requests += result.usage.model_requests
        total_tools += result.usage.tool_calls
        total_tokens += result.usage.total_tokens
        case_reports.append({
            "case_id": case.id, "description": case.description,
            "target_url": case.snapshot.target_url, "outcome": outcome, "error_type": error_type,
            "final_answer_accepted": result.content is not None,
            "expected": case.expected.model_dump(), "reference_notes": case.reference_notes,
            "checks": [check.model_dump() for check in checks],
            "human_review": {"status": "pending", "questions": case.review_questions, "notes": ""},
            "result": result.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
            "snapshot_requests": snapshot.requests,
        })
        report["cases_not_run"] = [item.id for item in dataset.cases[index:]]
        report["summary"] = {**summarize_cases(case_checks), "cases_with_accepted_answers": accepted}
        report["usage"] = {"model_requests": total_requests, "tool_calls": total_tools, "reported_total_tokens": total_tokens}
        report["stop_reason"] = stop_reason
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        failures = [check.name for check in checks if check.status == "failed"]
        print(f"  Outcome: {outcome}. Failed checks: {failures or 'none'}. Human review: pending.", flush=True)
        if stop_reason:
            break
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {path}", flush=True)
    return path


if __name__ == "__main__":
    if sys.argv[1:]:
        raise SystemExit("This evaluation script takes no command-line arguments.")
    asyncio.run(main())
