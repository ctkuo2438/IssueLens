"""An original bounded tool-calling loop; no agent framework is used."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import time
from typing import Any, Literal, cast

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.types.responses import (
    Response, ResponseFunctionToolCall, ResponseInputParam, ResponseTextConfigParam, ToolChoiceFunctionParam,
)
from openai.types.responses.response_create_params import ResponseCreateParamsNonStreaming
from pydantic import ValidationError
import tiktoken

from backend.app.agent_prompt import INSTRUCTIONS
from backend.app.analysis_validation import validate_analysis
from backend.app.checkpoint import AgentCheckpoint, CheckpointWriter, PendingTools, ScheduledRetry
from backend.app.config import Settings
from backend.app.context import BuiltContext, ContextBuilder
from backend.app.github import (
    CommentArguments, GitHubTools, RepositoryNotPublicError, SearchArguments, TOOLS, ToolError, TransientToolError,
)
from backend.app.models import (
    AnalysisContent, AnalysisResult, ContextBuildReport, EventKind, IssueTarget, RejectedAnswer,
)
from backend.app.retry import TransientFailure, openai_transient_failure
from backend.app.trace import TraceLog


REPAIR_RESERVE_TOKENS = 256
CONTINUATION_RESERVE_TOKENS = 512
CANDIDATE_RESERVE_TOKENS = 2400
PENDING_LOOKUP_RESERVE_TOKENS = 1024
FINAL_REPAIR_REQUESTS = 2
MAX_OUTPUT_TOKENS = 2400
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS if tool["type"] == "function")
RETRY_BUDGET_NAMES = {
    "model_request_budget": "model request budget",
    "tool_call_budget": "tool-call budget",
    "candidate_read_budget": "candidate-read budget",
}
Clip = Callable[[str, int], tuple[str, bool]]


def checkpoint_signature(settings: Settings) -> str:
    """Bind a saved run to its model, investigation limits, and request contract."""
    contract = {
        "model": settings.model, "reasoning_effort": settings.reasoning_effort,
        "max_iterations": settings.max_iterations, "max_tool_calls": settings.max_tool_calls,
        "runtime_seconds": float(settings.runtime_seconds), "context_tokens": settings.context_tokens,
        "max_candidates": settings.max_candidates, "max_candidate_reads": settings.max_candidate_reads,
        "instructions": INSTRUCTIONS, "tools": TOOLS, "schema": AnalysisContent.model_json_schema(),
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class RequestPlan:
    """What one model request may do, decided from the evidence collected so far."""

    iteration: int
    repairing: bool
    at_limit: bool
    closing_for_repairs: bool
    require_comments: bool
    required_action: str | None
    action_instruction: str
    tool_choice: Literal["auto", "none"] | ToolChoiceFunctionParam
    instructions: str
    future_reserve: int
    minimum_future_reserve: int
    # Set when the context budget cannot reserve room for more tools, so the request
    # becomes a final answer from existing evidence.
    finalizing_for_context: bool = False


class AgentRun:
    def __init__(
        self, target: IssueTarget, settings: Settings, emit: Callable[[EventKind, str], None],
        *, github_transport: httpx.AsyncBaseTransport | None = None,
        checkpoint: AgentCheckpoint | None = None, save_checkpoint: CheckpointWriter | None = None,
        trace: TraceLog | None = None,
    ) -> None:
        self.target = target
        self.settings = settings
        self.emit = emit
        # Evaluation can supply fixed HTTP responses; application runs use live GitHub.
        self.github_transport = github_transport
        self.checkpoint = checkpoint
        self.save_checkpoint = save_checkpoint
        self.trace = trace if trace is not None else TraceLog()
        self.result = checkpoint.result if checkpoint else AnalysisResult(analysis_date=datetime.now(UTC).date())
        self.tools: GitHubTools | None = None
        self.started = time.monotonic() - (checkpoint.elapsed_seconds() if checkpoint else 0)

    def _copy_evidence(self, result: AnalysisResult) -> None:
        assert self.tools is not None
        result.issue = self.tools.issue
        result.sources = list(self.tools.sources.values())
        result.comment_check = self.tools.comment_check
        result.candidates = list(self.tools.candidates.values())
        result.searches = list(self.tools.searches)
        result.similar_issue_check = self.tools.similar_issue_check
        result.repo_labels = list(self.tools.repo_labels.values())
        result.labels_source_id = self.tools.labels_source_id
        result.labels_complete = self.tools.labels_complete
        result.issue_templates = self.tools.issue_templates
        result.templates_source_id = self.tools.templates_source_id
        result.templates_complete = self.tools.templates_complete


    # The snapshot method collects the current state of the analysis and returns it as an AnalysisResult object.
    def snapshot(self, reason: str | None = None, *, stop_reason: str | None = None) -> AnalysisResult:
        if stop_reason is not None:
            self.trace.stop_reason = stop_reason
        if self.tools is not None:
            self._copy_evidence(self.result)
            self.result.limitations = list(dict.fromkeys(self.result.limitations + self.tools.limitations))
            comments = self.result.comment_check
            if comments.status == "not_read" and comments.reported_count:
                self.result.limitations.append("The target reports comments, but no comments lookup was attempted.")
            elif comments.status == "failed":
                self.result.limitations.append("The comments lookup failed; comment absence was not established.")
            elif comments.status == "partial":
                self.result.limitations.append("Only part of the comments was retrieved, or comment counts were inconsistent; coverage is incomplete.")
            if self.tools.issue is not None and not self.tools.searches:
                self.result.limitations.append("A similar-issue search was not completed.")
            if self.tools.issue is not None and self.tools.labels_source_id is None:
                self.result.limitations.append("The repository label catalog was not retrieved; label recommendations were not checked.")
            compared = {item.issue_number for item in self.result.content.comparisons} if self.result.content else set()
            similar = self.result.similar_issue_check
            similar.compared_candidate_numbers = sorted(compared)
            if similar.read_candidate_numbers:
                similar.comparison_status = "compared" if set(similar.read_candidate_numbers) <= compared else "partial"
            if similar.search_status == "partial" and not self.tools.candidates and self.required_lookup() is not None:
                self.result.limitations.append("The empty-search check is partial: a second distinct successful query was not completed, or a query failed.")
            if similar.candidate_status == "failed":
                self.result.limitations.append("Candidate body retrieval failed; an unread candidate cannot support a comparison.")
            if self.tools.candidates and not compared:
                self.result.limitations.append("Search candidates were found, but no grounded comparison was completed.")
            if any(candidate.source_id and candidate.number not in compared for candidate in self.tools.candidates.values()):
                self.result.limitations.append("Some full candidate reads were not included in the final comparisons.")
        elif self.checkpoint and self.checkpoint.tools:
            self.result.limitations.extend(self.checkpoint.tools.limitations)
        if reason and reason not in self.result.limitations:
            self.result.limitations.append(reason)
        self.result.limitations = list(dict.fromkeys(self.result.limitations))
        self.result.usage.elapsed_seconds = round(time.monotonic() - self.started, 2)
        return self.result


    # The citation_requirements method exposes server-owned source roles without inventing or adding citations.
    def citation_requirements(self) -> dict[str, Any]:
        """Expose server-owned source roles without inventing or adding citations."""
        assert self.tools is not None
        target = next((source.id for source in self.tools.sources.values() if source.kind == "issue"), None)
        return {
            "target_issue": target,
            "label_recommendations": [target, self.tools.labels_source_id] if target and self.tools.labels_source_id else [],
            "comparisons": {
                str(candidate.number): [target, candidate.source_id]
                for candidate in self.tools.candidates.values() if target and candidate.source_id
            },
        }


    # The required_lookup method determines the next required action for the agent to take based on the current state of the tools and the investigation.
    def required_lookup(self) -> tuple[str, str] | None:
        """Require minimum scoped evidence once; failures remain explicit and bounded."""
        assert self.tools is not None
        if self.tools.issue is None:
            return None
        if not self.tools.label_lookup_attempted:
            return "get_repo_labels", "Read the repository label catalog before finalizing."
        if not self.tools.search_attempts:
            return "search_issues", "Search this repository before finalizing."
        if self.tools.successful_search_queries and not self.tools.candidates and len(self.tools.search_attempts) < 2:
            return "search_issues", "The first search found no candidates. Try a distinct broader query before finalizing."
        if self.tools.candidates and not any(candidate.source_id for candidate in self.tools.candidates.values()) and not self.tools.candidate_read_attempts:
            return "get_related_issue", "Read the most relevant discovered candidate body before finalizing."
        return None


    def future_context_reserve(self) -> int:
        """Allow for one required candidate and pending lookups before freezing excerpts."""
        assert self.tools is not None
        reserve = CONTINUATION_RESERVE_TOKENS
        if self.tools.issue is None or self.required_lookup() is None:
            return reserve
        pending_search = not self.tools.search_attempts or bool(
            self.tools.successful_search_queries and not self.tools.candidates
            and len(self.tools.search_attempts) < 2
        )
        pending_candidate = not self.tools.candidate_read_attempts and not any(
            candidate.source_id for candidate in self.tools.candidates.values()
        ) and (pending_search or bool(self.tools.candidates))
        if pending_candidate:
            # The tool clips the body to 1800 text tokens; allow another 600 for
            # its JSON envelope and source/citation metadata. This is an estimate.
            reserve += CANDIDATE_RESERVE_TOKENS
        if not self.tools.label_lookup_attempted:
            reserve += PENDING_LOOKUP_RESERVE_TOKENS
        if pending_search:
            reserve += PENDING_LOOKUP_RESERVE_TOKENS
        return reserve

    def plan_request(self, iteration: int, repair_feedback: str | None) -> RequestPlan:
        """Decide what the next model request may do. It reads the run's state and changes nothing."""
        assert self.tools is not None
        repairing = repair_feedback is not None
        at_limit = (
            iteration == self.settings.max_iterations
            or self.result.usage.tool_calls >= self.settings.max_tool_calls
        )
        needs_first_page = bool(
            self.tools.issue and self.tools.issue.comment_count > 0
            and 1 not in self.tools.comment_attempted_pages
        )
        # Repair regenerates from the same evidence checkpoint. Pending
        # lookups stay incomplete rather than reopening investigation.
        lookup = None if repairing else self.required_lookup()
        closing_for_repairs = bool(
            not repairing and self.settings.max_iterations >= 6
            and iteration >= self.settings.max_iterations - FINAL_REPAIR_REQUESTS
            and self.tools.issue and self.tools.labels_source_id and self.tools.searches
            and not needs_first_page
            and lookup is None
        )
        require_comments = needs_first_page and not at_limit and not repairing
        required_action = "get_issue_comments" if require_comments else lookup[0] if lookup and not at_limit else None
        action_instruction = (
            "The required next action is get_issue_comments with page=1."
            if require_comments else f"The required next action is {lookup[0]}. {lookup[1]}"
            if lookup and not at_limit else ""
        )
        tool_choice: Literal["auto", "none"] | ToolChoiceFunctionParam = "none" if at_limit or repairing or closing_for_repairs else "auto"
        if required_action:
            tool_choice = {"type": "function", "name": required_action}
        instructions = (
            f"{INSTRUCTIONS}\nThis is model request {iteration} of {self.settings.max_iterations}. "
            "Use the remaining requests carefully. Finalize before the last request "
            "when possible, leaving room to correct validation errors.\n"
            f"Current server-owned comment_check: {self.tools.comment_check.model_dump_json()}. "
            f"Analysis date (UTC): {self.result.analysis_date}. "
            f"Current citation_requirements: {json.dumps(self.citation_requirements())}. "
            + action_instruction
            + (f"\nFinal-answer repair feedback: {repair_feedback}" if repairing else "")
            + ("\nCore checks are available. Return final JSON now, leaving the remaining requests for validation repair." if closing_for_repairs else "")
        )
        future_reserve = self.future_context_reserve() if not repairing and tool_choice != "none" else 0
        minimum_future_reserve = 0
        if future_reserve:
            minimum_future_reserve = (
                CANDIDATE_RESERVE_TOKENS if required_action == "get_related_issue"
                else CONTINUATION_RESERVE_TOKENS
            )
        return RequestPlan(
            iteration=iteration, repairing=repairing, at_limit=at_limit, closing_for_repairs=closing_for_repairs,
            require_comments=require_comments, required_action=required_action,
            action_instruction=action_instruction, tool_choice=tool_choice, instructions=instructions,
            future_reserve=future_reserve, minimum_future_reserve=minimum_future_reserve,
        )


    async def _save_execution_state(
        self, context: ContextBuilder, history: ResponseInputParam, *, repair_feedback: str | None = None,
        pending_tools: PendingTools | None = None, in_flight: Literal["model", "tool"] | None = None,
        retry: ScheduledRetry | None = None,
    ) -> None:
        if self.checkpoint is None or self.save_checkpoint is None:
            return
        assert self.tools is not None
        result = self.result.model_copy()
        self._copy_evidence(result)
        await self.save_checkpoint(AgentCheckpoint(
            signature=self.checkpoint.signature, started_at=self.checkpoint.started_at, result=result,
            # SDK input TypedDicts are plain dictionaries at the storage boundary.
            history=cast(list[dict[str, Any]], history), context=context.checkpoint(), tools=self.tools.checkpoint(),
            repair_feedback=repair_feedback, pending_tools=pending_tools, in_flight=in_flight, retry=retry,
        ))

    def _remaining_seconds(self) -> float:
        return self.settings.runtime_seconds - (time.monotonic() - self.started)

    def _plan_retry(
        self, operation: str, failure: TransientFailure, retry: ScheduledRetry | None, *, exhausted_budget: str | None,
        stops_analysis: bool = False,
    ) -> ScheduledRetry | None:
        if retry is not None:
            stop_reason = "retry_exhausted"
            reason = "the one retry was exhausted"
        elif exhausted_budget is not None:
            stop_reason = exhausted_budget
            reason = f"the {RETRY_BUDGET_NAMES[exhausted_budget]} was exhausted"
        elif failure.delay >= self._remaining_seconds():
            stop_reason = "retry_wait_exceeds_runtime"
            reason = "the required wait exceeds the remaining job time"
        else:
            not_before = datetime.now(UTC) + timedelta(seconds=failure.delay)
            attempt_id = self.trace.finish("failed", reason=failure.reason, retry_scheduled_for=not_before)
            self.emit("warning", f"Retrying {operation} in {failure.delay:.2f} seconds because {failure.reason}.")
            return ScheduledRetry(reason=failure.reason, not_before=not_before, attempt_id=attempt_id)
        self.trace.finish("failed", reason=failure.reason, stop_reason=stop_reason)
        if stops_analysis:
            self.trace.stop_reason = stop_reason
        message = f"Stopped retrying {operation} after {failure.reason} because {reason}."
        self.result.limitations.append(message)
        self.emit("warning", message)
        return None

    async def _wait_for_retry(self, retry: ScheduledRetry) -> None:
        delay = (retry.not_before - datetime.now(UTC)).total_seconds()
        if delay >= self._remaining_seconds():
            raise TimeoutError
        if delay > 0:
            await asyncio.sleep(delay)
        if self._remaining_seconds() <= 0:
            raise TimeoutError

    async def _confirm_public_repository(self) -> AnalysisResult | None:
        """Run the server's visibility check before any read; return a refusal if it does not pass.

        The check is not a model tool and uses no tool budget. It is read-only and
        repeated after an interruption, so its one retry needs no checkpoint.
        """
        assert self.tools is not None
        if self.tools.repository_public:
            # Confirmed before an interruption. A restored cooldown limits the next reads,
            # which fail like any other lookup, so it must not discard the evidence.
            return None
        retried = False
        while True:
            # Honor GitHub's cooldown, including one restored from a checkpoint.
            while (cooldown := (self.tools.cooldown_until or 0) - time.time()) > 0:
                if cooldown >= self._remaining_seconds():
                    return self._refuse_repository(
                        "GitHub's requested wait exceeds the remaining job time.", "repository_check_failed",
                    )
                await asyncio.sleep(cooldown)
            try:
                await self.tools.require_public_repository()
                return None
            except RepositoryNotPublicError as exc:
                return self._refuse_repository(str(exc), "repository_not_public")
            except TransientToolError as exc:
                if retried or exc.failure.delay >= self._remaining_seconds():
                    return self._refuse_repository(str(exc), "repository_check_failed")
                retried = True
                self.emit("warning", (
                    f"Retrying the repository visibility check in {exc.failure.delay:.2f} seconds "
                    f"because {exc.failure.reason}."
                ))
                await asyncio.sleep(exc.failure.delay)
            except ToolError as exc:
                return self._refuse_repository(str(exc), "repository_check_failed")

    def _refuse_repository(self, detail: str, stop_reason: str) -> AnalysisResult:
        """Publish no repository evidence or model output, including state restored from a checkpoint."""
        assert self.tools is not None
        if stop_reason == "repository_not_public":
            message = detail
        else:
            # Transient failure reasons are sentence fragments; GitHub status messages are sentences.
            detail = detail if detail.endswith(".") else f"{detail}."
            message = f"Could not confirm that the repository is public, so no repository data is included. {detail}"
        self.tools = GitHubTools(self.tools.client, self.target, self.tools.clip, require_public=True)
        self.result = AnalysisResult(analysis_date=self.result.analysis_date, usage=self.result.usage)
        self.emit("warning", message)
        return self.snapshot(message, stop_reason=stop_reason)

    def _tool_failure(self, name: str, message: str) -> dict[str, Any]:
        self.result.limitations.append(f"{name}: {message}")
        self.emit("warning", f"{name}: {message}")
        return {"error": message}

    async def _execute_tool(
        self, context: ContextBuilder, history: ResponseInputParam, pending: PendingTools, retry: ScheduledRetry | None,
    ) -> dict[str, Any]:
        assert self.tools is not None
        call = pending.calls[0]

        async def save_attempt() -> None:
            await self._save_execution_state(context, history, pending_tools=pending, in_flight="tool", retry=retry)
            if retry is not None and self._remaining_seconds() <= 0:
                raise TimeoutError

        while True:
            if retry is not None:
                await self._wait_for_retry(retry)
            self.result.usage.tool_calls += 1
            name = call.name if call.name in TOOL_NAMES else "unknown_tool"
            self.trace.begin("tool", name, self.result.usage.tool_calls, retry)
            self.emit("tool", f"Calling {call.name} with {call.arguments}.")
            await save_attempt()
            try:
                if pending.required_action and call.name != pending.required_action:
                    raise ToolError(f"The minimum-lookup policy requires {pending.required_action} first.")
                if (
                    retry is None and pending.required_action == "search_issues"
                    and SearchArguments.model_validate_json(call.arguments).query.casefold() in self.tools.search_attempts
                ):
                    raise ToolError("Use a distinct broader query; a cached repeat does not complete the empty-search check.")
                if pending.require_comments and (
                    call.name != "get_issue_comments" or CommentArguments.model_validate_json(call.arguments).page != 1
                ):
                    raise ToolError("The comment policy requires get_issue_comments page=1 first.")
                output = await self.tools.execute(call.name, call.arguments, before_candidate_read=save_attempt)
            except TransientToolError as exc:
                exhausted_budget = None
                if self.result.usage.tool_calls >= self.settings.max_tool_calls:
                    exhausted_budget = "tool_call_budget"
                elif call.name == "get_related_issue" and self.tools.candidate_reads >= self.settings.max_candidate_reads:
                    exhausted_budget = "candidate_read_budget"
                retry = self._plan_retry(call.name, exc.failure, retry, exhausted_budget=exhausted_budget)
                if retry is None:
                    return self._tool_failure(call.name, str(exc))
                await self._save_execution_state(context, history, pending_tools=pending, retry=retry)
            except (ToolError, ValidationError, ValueError) as exc:
                message = str(exc) if isinstance(exc, ToolError) else "The tool arguments or returned data were invalid."
                self.trace.finish("failed", reason=message, stop_reason="non_retryable_failure")
                return self._tool_failure(call.name, message)
            else:
                partial = (
                    (name == "get_repo_labels" and self.tools.labels_complete is False)
                    or output.get("complete") is False
                    or (name == "search_issues" and output.get("search", {}).get("incomplete_results") is True)
                )
                if partial:
                    self.trace.finish("partial", reason="The lookup returned partial data.")
                else:
                    self.trace.finish("succeeded")
                if retry is not None:
                    self.emit("status", f"The retry for {call.name} succeeded.")
                self.emit("tool", f"Finished {call.name}; {len(self.tools.sources)} sources collected.")
                return output

    async def _complete_tool_round(
        self, context: ContextBuilder, history: ResponseInputParam, pending: PendingTools,
        *, retry: ScheduledRetry | None = None, retry_interrupted: bool = False,
    ) -> None:
        assert self.tools is not None
        for index, call in enumerate(pending.calls):
            output: dict[str, Any]
            if retry is not None and retry_interrupted:
                output = self._tool_failure(call.name, "The Worker stopped during its one retry. The retry was exhausted.")
            elif pending.closing_for_repairs:
                output = {"error": "Tool use is closed to preserve final-answer repair requests. Return final JSON with existing evidence."}
            elif self.result.usage.tool_calls >= self.settings.max_tool_calls or pending.at_limit:
                output = {"error": "The tool-call budget was reached. Finalize with limitations."}
            else:
                output = await self._execute_tool(
                    context, history, pending.model_copy(update={"calls": pending.calls[index:]}), retry,
                )
            if call.name in {"get_issue", "get_issue_comments"}:
                check = self.tools.comment_check
                output["comment_check"] = check.model_dump()
                self.emit("status", (
                    f"Comment check: {check.status}; reported: {check.reported_count}; "
                    f"retrieved: {check.retrieved_count}; pages read: {check.read_pages}; "
                    f"failed pages: {check.failed_pages}; complete: {check.complete}."
                ))
            if call.name in {"search_issues", "get_related_issue"}:
                check = self.tools.similar_issue_check
                self.emit("status", (
                    f"Similar-issue check: search={check.search_status}; candidate={check.candidate_status}; "
                    f"read={check.read_candidate_numbers}; failed={check.failed_candidate_numbers}."
                ))
            history.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(output)})
            remaining = pending.calls[index + 1:]
            await self._save_execution_state(
                context, history, pending_tools=pending.model_copy(update={"calls": remaining}) if remaining else None,
            )
            retry = None
            retry_interrupted = False

    async def run(self) -> AnalysisResult:
        result = await self._investigate()
        if self.checkpoint is not None and self.save_checkpoint is not None:
            await self.save_checkpoint(AgentCheckpoint(
                signature=self.checkpoint.signature, started_at=self.checkpoint.started_at, result=result, finished=True,
            ))
        return result

    async def _investigate(self) -> AnalysisResult:
        """Run the bounded loop: plan a request, fit it in context, send it, then answer or use tools."""
        checkpoint = self.checkpoint
        self._announce_start()
        # This tokenizer estimates the serialized request, with a margin for protocol overhead.
        # It is a conservative local guard, not the provider's exact billing token count.
        encoding = await asyncio.to_thread(tiktoken.get_encoding, "o200k_base")

        def clip(text: str, budget: int) -> tuple[str, bool]:
            tokens = encoding.encode(text, disallowed_special=())
            return encoding.decode(tokens[:budget]), len(tokens) > budget

        context = ContextBuilder(
            lambda text: len(encoding.encode(text, disallowed_special=())), self.settings.context_tokens,
        )
        history = self._starting_history(context)
        text_format: ResponseTextConfigParam = {"format": {
            "type": "json_schema", "name": "issue_analysis",
            "schema": AnalysisContent.model_json_schema(), "strict": True,
        }}
        repair_feedback = checkpoint.repair_feedback if checkpoint else None
        model_retry = checkpoint.retry if checkpoint and checkpoint.pending_tools is None else None
        async with (
            self._github_client() as github,
            AsyncOpenAI(api_key=self.settings.openai_api_key, timeout=35, max_retries=0) as client,
        ):
            stopped = await self._start_tools(github, clip, context, history, model_retry)
            if stopped is not None:
                return stopped
            for iteration in range(self.result.usage.model_requests + 1, self.settings.max_iterations + 1):
                if model_retry is not None:
                    await self._wait_for_retry(model_retry)
                plan = self.plan_request(iteration, repair_feedback)
                built, plan = self._fit_context(context, history, plan, text_format)
                stopped = self._report_context(built.report, iteration)
                if stopped is not None:
                    return stopped
                history = built.history
                self._announce_request(plan)
                self.result.usage.model_requests += 1
                request = self._model_request(plan, history, text_format)
                self.trace.begin("model", "responses.create", self.result.usage.model_requests, model_retry)
                await self._save_execution_state(
                    context, history, repair_feedback=repair_feedback, in_flight="model", retry=model_retry,
                )
                if model_retry is not None and self._remaining_seconds() <= 0:
                    raise TimeoutError
                built.report.sent = True
                try:
                    response = await client.responses.create(**request)
                except (APIConnectionError, APIStatusError) as exc:
                    model_retry = self._retry_model_request(exc, model_retry, iteration)
                    if model_retry is None:
                        raise
                    await self._save_execution_state(context, history, repair_feedback=repair_feedback, retry=model_retry)
                    continue
                if model_retry is not None:
                    self.emit("status", "OpenAI returned a response to the retry.")
                    model_retry = None
                if response.usage:
                    self.result.usage.total_tokens += response.usage.total_tokens
                calls = [item for item in response.output if item.type == "function_call"]
                stopped = self._stop_on_response(response, calls, plan)
                if stopped is not None:
                    return stopped
                if calls:
                    await self._use_tools(context, history, response, calls, plan)
                    continue
                try:
                    content = self._validated_answer(response, plan, context)
                except (ValidationError, ValueError) as exc:
                    repair_feedback = self._reject_answer(exc, response, plan, context, clip)
                    await self._save_execution_state(context, history, repair_feedback=repair_feedback)
                    continue
                self.result.content = content
                self.trace.finish("succeeded", reason="The final answer passed validation.")
                return self.snapshot(stop_reason="final_answer")
            return self.snapshot(
                "The iteration limit was reached without a valid final analysis.", stop_reason="model_request_budget",
            )

    def _announce_start(self) -> None:
        checkpoint = self.checkpoint
        if not self.result.usage.model_requests:
            self.emit(
                "status",
                f"Model: {self.settings.model}; reasoning effort: {self.settings.reasoning_effort or 'provider default'}; "
                f"output limit: {MAX_OUTPUT_TOKENS} tokens including reasoning and visible text.",
            )
        if checkpoint and checkpoint.in_flight:
            self.emit("warning", (
                f"The previous Worker stopped before saving a {checkpoint.in_flight} response. "
                "The interrupted attempt counts toward the job budget."
            ))

    def _starting_history(self, context: ContextBuilder) -> ResponseInputParam:
        """The task message, or the checkpoint's model-visible history with its context state."""
        checkpoint = self.checkpoint
        history: ResponseInputParam = [{
            "role": "user", "content": f"Investigate this GitHub issue and draft a maintainer reply: {self.target.url}",
        }]
        if checkpoint and checkpoint.history:
            if checkpoint.history[0] != history[0]:
                raise ValueError("The checkpoint does not belong to the requested issue.")
            history = cast(ResponseInputParam, checkpoint.history)
            context.restore(checkpoint.context, history)
        return history

    def _github_client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "IssueLens-Local-MVP"}
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        return httpx.AsyncClient(
            base_url="https://api.github.com", headers=headers, timeout=12,
            follow_redirects=False, transport=self.github_transport,
        )

    async def _start_tools(
        self, github: httpx.AsyncClient, clip: Clip, context: ContextBuilder, history: ResponseInputParam,
        model_retry: ScheduledRetry | None,
    ) -> AnalysisResult | None:
        """Create the tools and finish what a checkpoint left in progress.

        Returns the run's result when it ends before the next model request.
        """
        checkpoint = self.checkpoint
        # A server credential may read private repositories; anonymous reads cannot.
        require_public = bool(self.settings.github_token)
        self.tools = GitHubTools(
            github, self.target, clip, self.settings.max_candidates, self.settings.max_candidate_reads,
            require_public=require_public,
        )
        if checkpoint and checkpoint.tools:
            self.tools.restore(checkpoint.tools)
        if require_public:
            refusal = await self._confirm_public_repository()
            if refusal is not None:
                return refusal
        if checkpoint and checkpoint.pending_tools:
            await self._complete_tool_round(
                context, history, checkpoint.pending_tools,
                retry=checkpoint.retry, retry_interrupted=checkpoint.in_flight == "tool",
            )
        if model_retry is not None and checkpoint and checkpoint.in_flight == "model":
            message = f"The Worker stopped during the OpenAI retry after {model_retry.reason}. The retry was exhausted."
            self.emit("warning", message)
            return self.snapshot(message, stop_reason="retry_exhausted")
        return None

    def _fit_context(
        self, context: ContextBuilder, history: ResponseInputParam, plan: RequestPlan,
        text_format: ResponseTextConfigParam,
    ) -> tuple[BuiltContext, RequestPlan]:
        """Build the request input within the local context budget.

        When the reserves for more tool use do not fit, the plan becomes a final answer
        from existing evidence, built first with a repair reserve and then without one.
        """
        assert self.tools is not None
        focus = self.tools.issue.title if self.tools.issue else ""
        built = context.build(
            history, instructions=plan.instructions, tools=TOOLS, text_format=text_format,
            request_number=plan.iteration, focus=focus,
            repair_reserve_tokens=0 if plan.repairing else REPAIR_RESERVE_TOKENS,
            future_reserve_tokens=plan.future_reserve,
            minimum_future_reserve_tokens=plan.minimum_future_reserve,
        )
        built.report.phase = "repair" if plan.repairing else "investigation"
        self.result.context_builds.append(built.report)
        if built.report.fits or plan.repairing:
            return built, plan
        # Stop tool planning when the remaining space cannot accommodate
        # its reserves. Retry only as a final answer from existing evidence.
        if built.report.future_reserve_minimum_tokens:
            limitation = (
                f"Finalizing early: at least {built.report.future_reserve_minimum_tokens} tokens for future evidence "
                "and loop growth could not be reserved within the local context budget. "
                "No further tool calls are allowed; pending checks remain incomplete."
            )
            self.result.limitations.append(limitation)
            self.emit("warning", limitation)
        instructions = plan.instructions
        if plan.action_instruction:
            instructions = instructions.replace(plan.action_instruction, "")
        plan = replace(
            plan, require_comments=False, required_action=None, tool_choice="none", instructions=instructions,
            finalizing_for_context=True,
        )
        built = context.build(
            history, instructions=plan.instructions, tools=TOOLS, text_format=text_format,
            request_number=plan.iteration, focus=focus, repair_reserve_tokens=REPAIR_RESERVE_TOKENS,
        )
        self.result.context_builds.append(built.report)
        if not built.report.fits:
            built = context.build(
                history, instructions=plan.instructions, tools=TOOLS, text_format=text_format,
                request_number=plan.iteration, focus=focus,
            )
            self.result.context_builds.append(built.report)
            if built.report.fits:
                limitation = "Finalization used the remaining context without a full repair reserve; another correction may not fit."
                self.result.limitations.append(limitation)
                self.emit("warning", limitation)
        return built, plan

    def _report_context(self, report: ContextBuildReport, iteration: int) -> AnalysisResult | None:
        """Report what the request input holds. Returns the run's result when nothing fits."""
        omitted = [item.source_id for item in report.sources if item.action == "omitted"]
        self.emit("status", (
            f"Context request {iteration}: estimated {report.estimated_tokens}/{report.budget_tokens} "
            f"input tokens including {report.margin_tokens} margin; "
            f"{report.repair_reserve_tokens} tokens reserved for repair feedback; "
            f"{report.future_reserve_tokens}/{report.future_reserve_target_tokens} tokens reserved for future evidence "
            f"and loop growth (minimum {report.future_reserve_minimum_tokens}); "
            f"{len(omitted)} excerpts omitted; {len(report.reused_call_ids)} repeated results reused."
        ))
        if not report.fits:
            self.emit("warning", "The local context budget cannot fit protected evidence and replay history; no model request was sent.")
            return self.snapshot(
                "The local context budget was reached: protected evidence and unchanged replay history "
                "could not fit. Newly fetched evidence in this build was not sent to the model.",
                stop_reason="context_budget",
            )
        if omitted:
            limitation = (
                f"Context Builder omitted fetched excerpts from model input: {', '.join(omitted)}. "
                "Retrieved excerpts remain available in Sources; omitted content was not reviewed by the model."
            )
            self.result.limitations.append(limitation)
            self.emit("warning", limitation)
        return None

    def _announce_request(self, plan: RequestPlan) -> None:
        if plan.at_limit:
            self.result.limitations.append("The agent reached its investigation budget; additional checks were not performed.")
        self.emit("model", (
            f"Answer repair step {plan.iteration}: regenerating from unchanged evidence; tools disabled."
            if plan.repairing else f"Investigation step {plan.iteration}: choosing the next action."
        ))
        if plan.closing_for_repairs:
            self.emit("status", f"Finalizing with existing evidence; reserving up to {self.settings.max_iterations - plan.iteration} requests for validation repair.")

    def _model_request(
        self, plan: RequestPlan, history: ResponseInputParam, text_format: ResponseTextConfigParam,
    ) -> ResponseCreateParamsNonStreaming:
        request: ResponseCreateParamsNonStreaming = {
            "model": self.settings.model,
            "instructions": plan.instructions,
            "input": list(history),
            "tools": TOOLS,
            "tool_choice": plan.tool_choice,
            "parallel_tool_calls": False,
            "text": text_format,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "store": False,
        }
        if self.settings.reasoning_effort is not None:
            request["reasoning"] = {"effort": self.settings.reasoning_effort}
        return request

    def _retry_model_request(
        self, exc: APIConnectionError | APIStatusError, retry: ScheduledRetry | None, iteration: int,
    ) -> ScheduledRetry | None:
        """Schedule the one retry of a transient OpenAI failure. Returns None when the run must stop."""
        failure = openai_transient_failure(exc)
        if failure is None:
            reason = (
                f"OpenAI returned HTTP {exc.status_code}." if isinstance(exc, APIStatusError)
                else "Could not connect to OpenAI."
            )
            self.trace.finish("failed", reason=reason, stop_reason="non_retryable_failure")
            self.trace.stop_reason = "model_request_failed"
            return None
        return self._plan_retry(
            "the OpenAI model request", failure, retry,
            exhausted_budget="model_request_budget" if iteration == self.settings.max_iterations else None,
            stops_analysis=True,
        )

    def _stop_on_response(
        self, response: Response, calls: list[ResponseFunctionToolCall], plan: RequestPlan,
    ) -> AnalysisResult | None:
        """Returns the run's result when this response ends it, before any tool runs or answer is checked."""
        assert self.tools is not None
        if response.status != "completed":
            if response.incomplete_details and response.incomplete_details.reason == "max_output_tokens":
                self.trace.finish("incomplete", stop_reason="max_output_tokens")
                return self.snapshot(
                    f"The model response was incomplete: max_output_tokens reached {MAX_OUTPUT_TOKENS}. "
                    "This output limit includes reasoning and visible text; no final analysis was accepted.",
                    stop_reason="max_output_tokens",
                )
            failed = response.status == "failed"
            stop_reason = "model_response_failed" if failed else "model_response_incomplete"
            self.trace.finish("failed" if failed else "incomplete", stop_reason=stop_reason)
            return self.snapshot(
                "The model response was incomplete; no final analysis was accepted.",
                stop_reason=stop_reason,
            )
        if calls and plan.finalizing_for_context:
            self.trace.finish("rejected", stop_reason="tool_use_closed")
            return self.snapshot(
                "The model requested a tool during context-budget finalization; no tool was executed.",
                stop_reason="context_budget",
            )
        if plan.required_action:
            self.emit("status", f"Required lookup before finalization: {plan.required_action}.")
        if calls and plan.repairing:
            self.trace.finish("rejected", stop_reason="tool_use_closed")
            return self.snapshot(
                "The model requested a tool during final-answer repair; no tool was executed.",
                stop_reason="tool_use_closed",
            )
        if not calls and self.tools.issue is None:
            self.trace.finish("rejected", stop_reason="target_issue_unavailable")
            return self.snapshot(
                "The model finished without successfully reading the target issue.",
                stop_reason="target_issue_unavailable",
            )
        return None

    async def _use_tools(
        self, context: ContextBuilder, history: ResponseInputParam, response: Response,
        calls: list[ResponseFunctionToolCall], plan: RequestPlan,
    ) -> None:
        if plan.closing_for_repairs or plan.at_limit:
            self.trace.finish(
                "rejected", reason="The response requested a tool while tool use was closed.",
                stop_reason="model_request_budget" if plan.iteration == self.settings.max_iterations else None,
            )
        else:
            self.trace.finish("succeeded", reason="The response requested tool calls.")
        # Continue tool planning with every original SDK output item intact,
        # including encrypted reasoning and function-call linkage.
        history.extend(cast(ResponseInputParam, [item.to_dict() for item in response.output]))
        pending = PendingTools(
            calls=calls, required_action=plan.required_action, require_comments=plan.require_comments,
            closing_for_repairs=plan.closing_for_repairs, at_limit=plan.at_limit,
        )
        await self._save_execution_state(context, history, pending_tools=pending)
        await self._complete_tool_round(context, history, pending)

    def _validated_answer(self, response: Response, plan: RequestPlan, context: ContextBuilder) -> AnalysisContent:
        """The final answer, or ValidationError or ValueError when it cannot be accepted."""
        assert self.tools is not None
        if plan.required_action:
            raise ValueError(f"Complete {plan.required_action} before finalizing. {plan.action_instruction}")
        content = AnalysisContent.model_validate_json(response.output_text)
        validate_analysis(content, self.tools, context.visible_source_ids)
        return content

    def _reject_answer(
        self, exc: ValueError, response: Response, plan: RequestPlan, context: ContextBuilder, clip: Clip,
    ) -> str | None:
        """Record a rejected answer and return the repair feedback for the next request."""
        self.trace.finish(
            "rejected", reason="The response did not pass final-answer validation.",
            stop_reason="model_request_budget" if plan.iteration == self.settings.max_iterations else None,
        )
        detail = "Follow the required analysis JSON schema." if isinstance(exc, ValidationError) else str(exc)
        next_step = "No correction budget remains."
        if plan.iteration < self.settings.max_iterations:
            next_step = "Retrying the required lookup." if plan.required_action else "Requesting a correction."
        self.emit("warning", f"The answer failed validation: {detail} {next_step}")
        # Retry from the same evidence checkpoint, not as a continuation of
        # the rejected response. Its text is audit data, never new evidence.
        repair_feedback = clip(
            f"{detail} Available IDs: {sorted(context.visible_source_ids)}. "
            "Regenerate consistent final JSON from the supplied evidence.",
            REPAIR_RESERVE_TOKENS - 24,
        )[0]
        # Count the escaped instruction suffix, including its heading.
        while context.count_tokens(json.dumps(
            f"\nFinal-answer repair feedback: {repair_feedback}", ensure_ascii=False,
        )) > REPAIR_RESERVE_TOKENS:
            repair_feedback = clip(repair_feedback, max(0, context.count_tokens(repair_feedback) - 16))[0]
        self.result.rejected_answers.append(RejectedAnswer(
            request_number=plan.iteration, reason=detail, output_text=response.output_text,
            output_sha256=hashlib.sha256(json.dumps({
                "output": [item.to_dict() for item in response.output],
                "output_text": response.output_text,
            }, sort_keys=True).encode()).hexdigest(),
            feedback=repair_feedback,
        ))
        if plan.required_action:
            # A premature answer violates tool selection. Retry the
            # required lookup as investigation, not final-answer repair.
            return None
        return repair_feedback
