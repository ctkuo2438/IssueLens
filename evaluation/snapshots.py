"""Validated fictional cases and a local HTTP transport, with no GitHub network access."""

import base64
from pathlib import Path
import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.models import IssueTarget, RepositoryLabel


Relationship = Literal["possible_duplicate", "related_but_different", "insufficient_evidence"]


class FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SnapshotIssue(FixtureModel):
    number: int = Field(gt=0)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    state: Literal["open", "closed"] = "open"
    labels: list[str] = Field(default_factory=list)


class SnapshotComment(FixtureModel):
    id: int = Field(gt=0)
    author: str
    body: str


class Snapshot(FixtureModel):
    target_url: str
    issues: list[SnapshotIssue] = Field(min_length=1)
    comments: list[SnapshotComment]

    @property
    def target(self) -> IssueTarget:
        return IssueTarget.from_url(self.target_url)

    @model_validator(mode="after")
    def target_exists(self) -> "Snapshot":
        numbers = [issue.number for issue in self.issues]
        if len(numbers) != len(set(numbers)) or self.target.number not in numbers:
            raise ValueError("Each snapshot needs unique issue numbers and its target issue.")
        return self


class ExpectedComparison(FixtureModel):
    issue_number: int = Field(gt=0)
    relationship: Relationship


class Expectations(FixtureModel):
    comparisons: list[ExpectedComparison]
    required_labels: list[str]
    allowed_labels: list[str]
    missing_information_required: bool = False
    no_candidates_expected: bool = False


class EvaluationCase(FixtureModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    description: str
    snapshot: Snapshot
    expected: Expectations
    reference_notes: str
    review_questions: list[str] = Field(min_length=1)


class Dataset(FixtureModel):
    id: str
    version: int = Field(gt=0)
    description: str
    annotation_status: Literal["assistant_authored_reference_pending_human_review"]
    labels: list[RepositoryLabel] = Field(min_length=1)
    cases: list[EvaluationCase] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def references_are_consistent(self) -> "Dataset":
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Evaluation case IDs must be unique.")
        catalog = {label.name.casefold() for label in self.labels}
        if len(catalog) != len(self.labels):
            raise ValueError("Catalog label names must be unique.")
        for case in self.cases:
            target = case.snapshot.target
            candidates = {issue.number for issue in case.snapshot.issues if issue.number != target.number}
            expected_numbers = [item.issue_number for item in case.expected.comparisons]
            if len(set(expected_numbers)) != len(expected_numbers) or not set(expected_numbers) <= candidates:
                raise ValueError("Expected comparisons must reference unique non-target snapshot issues.")
            if case.expected.no_candidates_expected and expected_numbers:
                raise ValueError("An empty-search reference cannot also require a comparison.")
            existing = {name.casefold() for issue in case.snapshot.issues if issue.number == target.number for name in issue.labels}
            required = {name.casefold() for name in case.expected.required_labels}
            allowed = {name.casefold() for name in case.expected.allowed_labels}
            if not required <= allowed <= catalog or allowed & existing:
                raise ValueError("Reference labels must be catalog names that are not already applied.")
            if any(name.casefold() not in catalog for issue in case.snapshot.issues for name in issue.labels):
                raise ValueError("Snapshot issue labels must exist in the catalog.")
        return self


def load_dataset(path: Path) -> Dataset:
    return Dataset.model_validate_json(path.read_text(encoding="utf-8"))


class SnapshotGitHub:
    """Serve only supplied evidence. This object never receives reference expectations."""

    def __init__(self, snapshot: Snapshot, labels: list[RepositoryLabel]) -> None:
        self.snapshot = snapshot
        self.labels = labels
        self.requests: list[dict[str, object]] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append({
            "method": request.method, "path": request.url.path,
            "query": dict(request.url.params),
        })
        target = self.snapshot.target
        path = request.url.path
        if request.method != "GET" or request.url.host != "api.github.com":
            return httpx.Response(403, json={"message": "Only scoped read-only snapshot requests are available."})
        if path == "/search/issues":
            query = request.url.params.get("q", "")
            scope = f"repo:{target.owner}/{target.repo} is:issue in:title,body "
            terms = re.findall(r'"([^"]+)"', query.removeprefix(scope))
            if not query.startswith(scope) or not terms:
                return httpx.Response(422, json={"message": "Use repository-scoped quoted keywords."})
            # A deterministic AND-substring matcher, not a simulation of GitHub ranking.
            matches = [
                self.issue_payload(issue) for issue in self.snapshot.issues
                if all(term.casefold() in f"{issue.title}\n{issue.body}".casefold() for term in terms)
            ]
            return httpx.Response(200, json={"total_count": len(matches), "incomplete_results": False, "items": matches})
        if path == f"{target.api_path}/labels":
            return httpx.Response(200, json=[label.model_dump() for label in self.labels])
        if path == f"{target.api_path}/contents/.github/ISSUE_TEMPLATE":
            return httpx.Response(404, json={"message": "This fixed snapshot has no issue template directory."})
        if path == f"{target.api_path}/issues/{target.number}/comments":
            page = int(request.url.params.get("page", "1"))
            comments = self.snapshot.comments[(page - 1) * 10:page * 10]
            return httpx.Response(200, json=[{
                "id": item.id, "user": {"login": item.author}, "body": item.body,
                "author_association": "NONE", "created_at": "2026-11-08T00:00:00Z",
            } for item in comments])
        if path == f"{target.api_path}/readme":
            text = "A fictional document editor. No additional investigation evidence is available in this README."
            return httpx.Response(200, json={
                "encoding": "base64", "content": base64.b64encode(text.encode()).decode(),
                "size": len(text), "path": "README.md",
                "html_url": f"https://github.com/{target.owner}/{target.repo}/blob/main/README.md",
            })
        issue = next((item for item in self.snapshot.issues if path == f"{target.api_path}/issues/{item.number}"), None)
        if issue:
            return httpx.Response(200, json=self.issue_payload(issue))
        return httpx.Response(404, json={"message": "Resource not present in this fixed snapshot."})

    def issue_payload(self, issue: SnapshotIssue) -> dict[str, object]:
        target = self.snapshot.target
        return {
            "number": issue.number, "title": issue.title, "body": issue.body, "state": issue.state,
            "user": {"login": "fixture-reporter"}, "labels": [{"name": name} for name in issue.labels],
            "comments": len(self.snapshot.comments) if issue.number == target.number else 0,
            "repository_url": f"https://api.github.com{target.api_path}",
        }
