"""Pack new tool evidence without rewriting a model-visible conversation prefix."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any, cast

from openai.types.responses import FunctionToolParam, ResponseInputParam, ResponseTextConfigParam
from openai.types.responses.response_input_param import FunctionCallOutput
from pydantic import BaseModel, Field, NonNegativeInt

from backend.app.models import ContextBuildReport, ContextSourceDecision

REQUEST_MARGIN_TOKENS = 1024
STOP_WORDS = {"the", "and", "for", "with", "from", "this", "that", "does", "issue"}


def _source_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Only the seven local tools' explicit source locations are inspected."""
    rows = [payload[key] for key in ("source", "lookup") if key in payload]
    for key in ("comments", "sources"):
        rows.extend(payload.get(key, []))
    return [row for row in rows if isinstance(row, dict) and {"id", "kind", "excerpt"} <= row.keys()]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_-]{2,}", text.casefold())) - STOP_WORDS


@dataclass
class BuiltContext:
    history: ResponseInputParam
    report: ContextBuildReport


@dataclass
class PendingResult:
    item: FunctionCallOutput
    payload: dict[str, Any]

    def serialize(self) -> None:
        self.item["output"] = json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))


class ContextCheckpoint(BaseModel):
    frozen_items: NonNegativeInt = 0
    seen_results: dict[str, str] = Field(default_factory=dict)
    visible_source_ids: set[str] = Field(default_factory=set)


class ContextBuilder:
    def __init__(self, count_tokens: Callable[[str], int], budget_tokens: int) -> None:
        self.count_tokens = count_tokens
        self.budget_tokens = budget_tokens
        self._frozen: ResponseInputParam = []
        self._seen_results: dict[str, str] = {}
        self.visible_source_ids: set[str] = set()

    def checkpoint(self) -> ContextCheckpoint:
        return ContextCheckpoint(
            frozen_items=len(self._frozen), seen_results=self._seen_results,
            visible_source_ids=self.visible_source_ids,
        )

    def restore(self, state: ContextCheckpoint, history: ResponseInputParam) -> None:
        if state.frozen_items > len(history):
            raise ValueError("The checkpoint is missing part of its model-visible history.")
        self._frozen = deepcopy(history[:state.frozen_items])
        self._seen_results = dict(state.seen_results)
        self.visible_source_ids = set(state.visible_source_ids)

    def build(
        self, history: ResponseInputParam, *, instructions: str,
        tools: list[FunctionToolParam], text_format: ResponseTextConfigParam,
        request_number: int, focus: str, repair_reserve_tokens: int = 0,
        future_reserve_tokens: int = 0,
        minimum_future_reserve_tokens: int | None = None,
    ) -> BuiltContext:
        """Pack toward the reserve target; commit only if input and minimums fit."""
        if history[:len(self._frozen)] != self._frozen:
            raise ValueError("The model-visible conversation prefix must remain unchanged.")
        if repair_reserve_tokens < 0 or future_reserve_tokens < 0:
            raise ValueError("Context reserves cannot be negative.")
        minimum = future_reserve_tokens if minimum_future_reserve_tokens is None else minimum_future_reserve_tokens
        if not 0 <= minimum <= future_reserve_tokens:
            raise ValueError("The minimum future reserve must be between zero and its target.")
        reserved = repair_reserve_tokens + future_reserve_tokens
        packed = deepcopy(history)

        # estimate() is called repeatedly as optional excerpts are dropped to fit the budget.
        def estimate() -> int:
            text = json.dumps([instructions, tools, text_format, packed], ensure_ascii=False)
            return self.count_tokens(text) + REQUEST_MARGIN_TOKENS

        before = estimate()
        seen = dict(self._seen_results)
        decisions: list[ContextSourceDecision] = []
        reused_call_ids: list[str] = []
        optional: list[tuple[int, PendingResult, dict[str, Any], ContextSourceDecision]] = []
        pending: list[PendingResult] = []
        visible = set(self.visible_source_ids)
        focus_words = _words(focus)

        for item in packed[len(self._frozen):]:
            if item.get("type") != "function_call_output":
                continue
            # Narrow only this SDK union variant after checking its discriminator.
            output = cast(FunctionCallOutput, item)
            call_id = output.get("call_id")
            if not call_id:
                raise ValueError("Local tool outputs must identify their function call.")
            if not isinstance(output["output"], str):
                raise ValueError("Local tools must return JSON text.")
            payload = json.loads(output["output"])
            if not isinstance(payload, dict):
                raise ValueError("Local tools must return JSON objects.")

            fingerprint = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            rows = _source_rows(payload)
            previous_call = seen.get(fingerprint)
            if previous_call is not None:
                reference = json.dumps({
                    "context_reuse": {"call_id": previous_call, "source_ids": [row["id"] for row in rows]},
                    "note": "Reuse the earlier model-visible result. Its earlier excerpt omissions still apply.",
                }, separators=(",", ":"))
            else:
                reference = None
            # Tiny results can be cheaper than a reference. Count their JSON string
            # representation, as the request estimate also includes string escaping.
            if (
                previous_call is not None and reference is not None
                and self.count_tokens(json.dumps(reference, ensure_ascii=False))
                < self.count_tokens(json.dumps(output["output"], ensure_ascii=False))
            ):
                output["output"] = reference
                reused_call_ids.append(previous_call)
                for row in rows:
                    decisions.append(ContextSourceDecision(
                        source_id=row["id"], kind=row["kind"], action="reused",
                        reason=f"Identical result already represented by {previous_call}.",
                        excerpt_tokens=self.count_tokens(row["excerpt"]),
                    ))
                continue
            seen[fingerprint] = call_id
            result = PendingResult(output, payload)
            pending.append(result)
            for row in rows:
                decision = ContextSourceDecision(
                    source_id=row["id"], kind=row["kind"], action="kept",
                    reason="Keep the retrieved excerpt and source identity.",
                    excerpt_tokens=self.count_tokens(row["excerpt"]),
                )
                decisions.append(decision)
                # Search metadata already carries these exact fields in structured form.
                if row["kind"] == "search" and isinstance(payload.get("search"), dict):
                    try:
                        summary = json.loads(row["excerpt"])
                    except (ValueError, TypeError):
                        summary = None
                    if isinstance(summary, dict) and summary and all(
                        key in payload["search"] and payload["search"][key] == value
                        for key, value in summary.items()
                    ):
                        row["excerpt"] = "Search evidence is in the search object of this result."
                        decision.action = "redundant"
                        decision.reason = "Exact search metadata is retained in the search object."
                # Target, candidate bodies, label rows, template fields, and lookup states
                # are protected. Optional excerpts are dropped as whole units, never sliced.
                priority = {"readme": 0, "issue_template": 1}.get(row["kind"])
                if row["kind"] == "comment":
                    association = row["excerpt"].split("\n", 1)[0]
                    maintainer = association in {"Author association: OWNER", "Author association: MEMBER", "Author association: COLLABORATOR"}
                    overlap = len(focus_words & _words(row["excerpt"]))
                    priority = 10 + 2 * overlap + 3 * maintainer
                    decision.reason = f"Comment heuristic: {overlap} matching title terms; maintainer association: {maintainer}."
                if priority is not None and row["excerpt"]:
                    if row["kind"] != "issue_template" or any(
                        template.get("source_id") == row["id"] for template in payload.get("templates", [])
                    ):
                        decision.priority = priority
                        optional.append((priority, result, row, decision))
                elif row["kind"] != "search":
                    decision.reason = "Protected evidence is retained without further excerpt reduction."
            result.serialize()

        after = estimate()
        for _, result, row, decision in sorted(optional, key=lambda entry: entry[0]):
            if after + reserved <= self.budget_tokens:
                break
            original_excerpt = row["excerpt"]
            original_selection = deepcopy(result.payload.get("context_selection"))
            row["excerpt"] = ""
            selection = result.payload.setdefault("context_selection", {
                "omitted_excerpt_source_ids": [],
                "note": "These excerpts were fetched but are absent from model input. Do not infer their contents or treat this omission as missing reporter information. Template field metadata, when present, remains available.",
            })
            selection["omitted_excerpt_source_ids"].append(row["id"])
            result.serialize()
            proposed = estimate()
            if proposed >= after:
                # Do not lose evidence when the omission notice costs more than it saves.
                row["excerpt"] = original_excerpt
                if original_selection is None:
                    result.payload.pop("context_selection")
                else:
                    result.payload["context_selection"] = original_selection
                result.serialize()
                continue
            decision.action = "omitted"
            decision.reason = "Fetched excerpt omitted before first model exposure to fit the request and its reserves within the local context budget."
            after = proposed

        for result in pending:
            for row in _source_rows(result.payload):
                if row["excerpt"] or row["kind"] not in {"comment", "readme"}:
                    visible.add(row["id"])
        allocated = min(future_reserve_tokens, max(0, self.budget_tokens - after - repair_reserve_tokens))
        fits = after + repair_reserve_tokens + minimum <= self.budget_tokens
        report = ContextBuildReport(
            request_number=request_number, budget_tokens=self.budget_tokens,
            estimated_tokens_before=before, estimated_tokens=after,
            margin_tokens=REQUEST_MARGIN_TOKENS, repair_reserve_tokens=repair_reserve_tokens,
            future_reserve_tokens=allocated, future_reserve_target_tokens=future_reserve_tokens,
            future_reserve_minimum_tokens=minimum,
            fits=fits, sources=decisions,
            reused_call_ids=reused_call_ids,
        )
        if fits:
            self._frozen = deepcopy(packed)
            self._seen_results = seen
            self.visible_source_ids = visible
        return BuiltContext(packed, report)
