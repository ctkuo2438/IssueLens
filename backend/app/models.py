"""Validated API data and the model's structured final answer."""

from datetime import UTC, date, datetime
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IssueTarget(BaseModel):
    owner: str
    repo: str
    number: int

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/issues/{self.number}"

    @property
    def api_path(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"

    @classmethod
    def from_url(cls, value: str) -> "IssueTarget":
        url = urlsplit(value.strip())
        match = re.fullmatch(
            r"/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})/issues/([1-9][0-9]{0,9})/?",
            url.path,
        )
        if url.scheme != "https" or url.netloc.lower() != "github.com" or not match:
            raise ValueError("Enter a GitHub issue URL: https://github.com/owner/repo/issues/123")
        owner, repo, number = match.groups()
        if repo in {".", ".."}:
            raise ValueError("The repository name is invalid.")
        return cls(owner=owner, repo=repo, number=int(number))


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_url: str = Field(min_length=1, max_length=2048)

    @field_validator("issue_url")
    @classmethod
    def validate_issue_url(cls, value: str) -> str:
        return IssueTarget.from_url(value).url


class GitHubUser(BaseModel):
    id: int = Field(strict=True, gt=0, le=2**63 - 1)
    login: str = Field(min_length=1, max_length=100)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    source_ids: list[str]


class IssueComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_number: int
    relationship: Literal["possible_duplicate", "related_but_different", "insufficient_evidence"]
    reason: str
    similarities: list[str]
    differences: list[str]
    source_ids: list[str]


class LabelRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    reason: str
    source_ids: list[str]


class RepositoryLabel(BaseModel):
    name: str
    description: str
    color: str = Field(pattern=r"^[0-9a-fA-F]{6}$")


class TemplateField(BaseModel):
    label: str
    description: str
    kind: str
    required: bool | None
    required_options: list[str] = Field(default_factory=list)


class IssueTemplate(BaseModel):
    path: str
    name: str
    description: str
    format: Literal["form", "markdown"]
    source_id: str
    fields: list[TemplateField] = Field(default_factory=list)
    requirements_known: bool = False
    notes: list[str] = Field(default_factory=list)


class AnalysisContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: Finding
    known_facts: list[Finding]
    missing_information: list[str]
    draft_reply: str
    limitations: list[str]
    comparisons: list[IssueComparison]
    label_recommendations: list[LabelRecommendation]


SourceKind = Literal["issue", "comment", "readme", "search", "related_issue", "repo_labels", "issue_template", "template_lookup"]


class Source(BaseModel):
    id: str
    kind: SourceKind
    title: str
    url: str
    excerpt: str


class IssueInfo(BaseModel):
    title: str
    url: str
    number: int
    state: str
    author: str
    labels: list[str]
    comment_count: int
    created_at: datetime | None = None
    closed_at: datetime | None = None


class CommentCheck(BaseModel):
    status: Literal["not_read", "empty", "read", "partial", "failed"] = "not_read"
    reported_count: int | None = None
    attempted_pages: list[int] = Field(default_factory=list)
    read_pages: list[int] = Field(default_factory=list)
    failed_pages: list[int] = Field(default_factory=list)
    retrieved_count: int = 0
    source_ids: list[str] = Field(default_factory=list)
    has_next_page: bool | None = None
    count_mismatch: bool = False
    complete: bool = False


class Usage(BaseModel):
    model_requests: int = 0
    tool_calls: int = 0
    total_tokens: int = 0
    elapsed_seconds: float = 0


class IssueCandidate(BaseModel):
    number: int
    title: str
    url: str
    state: str
    source_id: str | None = None


class IssueSearch(BaseModel):
    query: str
    total_count: int
    incomplete_results: bool
    candidate_numbers: list[int]
    source_id: str


class SimilarIssueCheck(BaseModel):
    search_status: Literal["not_checked", "empty", "candidates_found", "partial", "failed"] = "not_checked"
    attempted_queries: list[str] = Field(default_factory=list)
    failed_queries: list[str] = Field(default_factory=list)
    candidate_status: Literal["not_checked", "not_needed", "read", "partial", "failed"] = "not_checked"
    attempted_candidate_numbers: list[int] = Field(default_factory=list)
    failed_candidate_numbers: list[int] = Field(default_factory=list)
    read_candidate_numbers: list[int] = Field(default_factory=list)
    compared_candidate_numbers: list[int] = Field(default_factory=list)
    comparison_status: Literal["not_checked", "not_needed", "compared", "partial", "unavailable"] = "not_checked"


class ContextSourceDecision(BaseModel):
    source_id: str
    kind: SourceKind
    action: Literal["kept", "omitted", "reused", "redundant"]
    reason: str
    excerpt_tokens: int
    priority: int | None = None


class ContextBuildReport(BaseModel):
    request_number: int
    budget_tokens: int
    estimated_tokens_before: int
    estimated_tokens: int
    margin_tokens: int
    repair_reserve_tokens: int = 0
    future_reserve_tokens: int = 0
    future_reserve_target_tokens: int = 0
    future_reserve_minimum_tokens: int = 0
    phase: Literal["investigation", "repair"] = "investigation"
    fits: bool
    sent: bool = False
    sources: list[ContextSourceDecision] = Field(default_factory=list)
    reused_call_ids: list[str] = Field(default_factory=list)


class RejectedAnswer(BaseModel):
    request_number: int
    reason: str
    output_text: str
    output_sha256: str
    feedback: str


class AnalysisResult(BaseModel):
    analysis_date: date | None = None
    issue: IssueInfo | None = None
    content: AnalysisContent | None = None
    sources: list[Source] = Field(default_factory=list)
    comment_check: CommentCheck = Field(default_factory=CommentCheck)
    candidates: list[IssueCandidate] = Field(default_factory=list)
    searches: list[IssueSearch] = Field(default_factory=list)
    similar_issue_check: SimilarIssueCheck = Field(default_factory=SimilarIssueCheck)
    repo_labels: list[RepositoryLabel] = Field(default_factory=list)
    labels_source_id: str | None = None
    labels_complete: bool | None = None
    issue_templates: list[IssueTemplate] = Field(default_factory=list)
    templates_source_id: str | None = None
    templates_complete: bool | None = None
    context_builds: list[ContextBuildReport] = Field(default_factory=list)
    rejected_answers: list[RejectedAnswer] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)


JobStatus = Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled"}


EventKind = Literal["status", "model", "tool", "warning"]


class ProgressEvent(BaseModel):
    id: int
    kind: EventKind
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JobSnapshot(BaseModel):
    job_id: str
    issue_url: str
    status: JobStatus
    created_at: datetime
    result: AnalysisResult | None = None
    error: str | None = None
    events: list[ProgressEvent] = Field(default_factory=list)
