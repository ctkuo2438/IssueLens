"""Read server-side configuration without exposing credentials to the browser."""

from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import load_dotenv
import httpx
from openai.types.shared import ReasoningEffort


ROOT = Path(__file__).resolve().parents[2]
GITHUB_CALLBACK_PATH = "/api/v1/auth/github/callback"
REASONING_EFFORTS: dict[str, ReasoningEffort] = {
    "none": "none", "low": "low", "medium": "medium",
    "high": "high", "xhigh": "xhigh", "max": "max",
}


@dataclass(frozen=True)
class GitHubOAuthConfig:
    client_id: str
    client_secret: str = field(repr=False)
    callback_url: str

    def __post_init__(self) -> None:
        try:
            url = httpx.URL(self.callback_url)
        except (httpx.InvalidURL, ValueError):
            raise ValueError("GITHUB_OAUTH_CALLBACK_URL is invalid.") from None
        if not self.client_id or not self.client_secret:
            raise ValueError("Set both GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET.")
        if (
            not url.host or url.userinfo or url.fragment or url.raw_path != GITHUB_CALLBACK_PATH.encode()
            or url.scheme not in {"http", "https"}
            or (url.scheme == "http" and url.host not in {"localhost", "127.0.0.1", "::1"})
        ):
            raise ValueError(
                f"GITHUB_OAUTH_CALLBACK_URL must end in {GITHUB_CALLBACK_PATH} and use HTTPS or loopback HTTP."
            )

    @property
    def origin(self) -> str:
        return str(httpx.URL(self.callback_url).copy_with(raw_path=b"/")).rstrip("/")


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = field(default="", repr=False)
    github_token: str = field(default="", repr=False)
    database_url: str = field(default="", repr=False)
    model: str = "gpt-5.6"
    reasoning_effort: ReasoningEffort = None
    max_iterations: int = 8
    max_tool_calls: int = 15
    runtime_seconds: float = 90
    context_tokens: int = 24_000
    max_candidates: int = 20
    max_candidate_reads: int = 5
    max_jobs: int = 30
    # Queued or running analyses one signed-in account may hold in the shared queue.
    max_active_per_account: int = 2
    concurrent_jobs: int = 2
    job_ttl_seconds: int = 3600
    github_oauth: GitHubOAuthConfig | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        requested_effort = os.getenv("OPENAI_REASONING_EFFORT", "").strip()
        # Select a typed SDK value without casting an arbitrary environment string.
        effort = REASONING_EFFORTS.get(requested_effort)
        if requested_effort and effort is None:
            raise ValueError("OPENAI_REASONING_EFFORT must be blank, none, low, medium, high, xhigh, or max.")
        oauth_values = (
            os.getenv("GITHUB_OAUTH_CLIENT_ID", "").strip(),
            os.getenv("GITHUB_OAUTH_CLIENT_SECRET", "").strip(),
            os.getenv("GITHUB_OAUTH_CALLBACK_URL", "").strip(),
        )
        oauth = GitHubOAuthConfig(*oauth_values) if any(oauth_values) else None
        settings = cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            database_url=os.getenv("DATABASE_URL", "").strip(),
            model=os.getenv("OPENAI_MODEL", "gpt-5.6"),
            reasoning_effort=effort,
            max_iterations=int(os.getenv("AGENT_MAX_ITERATIONS", "8")),
            max_tool_calls=int(os.getenv("AGENT_MAX_TOOL_CALLS", "15")),
            runtime_seconds=float(os.getenv("AGENT_RUNTIME_SECONDS", "90")),
            context_tokens=int(os.getenv("AGENT_CONTEXT_TOKENS", "24000")),
            max_candidates=int(os.getenv("AGENT_MAX_CANDIDATES", "20")),
            max_candidate_reads=int(os.getenv("AGENT_MAX_CANDIDATE_READS", "5")),
            github_oauth=oauth,
        )
        if settings.database_url and not settings.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URL or blank.")
        if oauth is not None and not settings.database_url:
            raise ValueError("GitHub sign-in requires DATABASE_URL.")
        if not (
            1 <= settings.max_iterations <= 20
            and 1 <= settings.max_tool_calls <= 30
            and 1 <= settings.runtime_seconds <= 300
            and 2000 <= settings.context_tokens <= 32_000
            and 1 <= settings.max_candidates <= 20
            and 1 <= settings.max_candidate_reads <= 5
        ):
            raise ValueError("Agent limits are outside the supported local development range.")
        return settings
