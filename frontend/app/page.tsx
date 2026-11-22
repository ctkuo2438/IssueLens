"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  isFinished,
  type AuthSession,
  type Finding,
  type Health,
  type Job,
  type Progress,
  type Source,
} from "./api-types";
import { IssueTemplates } from "./issue-templates";
import { LabelRecommendations } from "./label-recommendations";
import { SimilarIssues } from "./similar-issues";

type DraftEdit = { jobId: string; text: string };
type CopyFeedback = DraftEdit & { status: "copied" | "error" };
type IconName =
  | "lens"
  | "github"
  | "arrow"
  | "link"
  | "check"
  | "copy"
  | "activity"
  | "file"
  | "spark"
  | "stop";

const EXAMPLE = "https://github.com/pallets/flask/issues/6146";

function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, React.ReactNode> = {
    lens: (
      <>
        <circle cx="10.5" cy="10.5" r="6.5" />
        <circle cx="10.5" cy="10.5" r="2" />
        <path d="m16 16 5 5" />
      </>
    ),
    github: (
      <>
        <path
          d="M9 19c-4 1-4-2-6-2m12 5v-4a3.5 3.5 0 0 0-1-2.7c3.3-.4 6.7-1.6 6.7-7.3a5.7 5.7 0 0 0-1.5-4 5.3 5.3 0 0 0-.1-4S17.9.6 15 2a14 14 0 0 0-7 0C5.1.6 3.9 1 3.9 1a5.3 5.3 0 0 0-.1 4 5.7 5.7 0 0 0-1.5 4c0 5.7 3.4 6.9 6.7 7.3A3.5 3.5 0 0 0 8 19v3"
          transform="translate(1 0) scale(.95)"
        />
      </>
    ),
    arrow: (
      <>
        <path d="M5 12h14m-5-5 5 5-5 5" />
      </>
    ),
    link: (
      <>
        <path
          d="m10 13 4-4m-6 7-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 1 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"
          transform="translate(1 1)"
        />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    copy: (
      <>
        <rect x="8" y="8" width="12" height="13" rx="2" />
        <path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" />
      </>
    ),
    activity: <path d="M2 12h4l3-8 5 16 3-8h5" />,
    file: (
      <>
        <path d="M14 2H5a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V9ZM14 2v7h7M7 13h10M7 17h7" />
      </>
    ),
    spark: (
      <>
        <path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5ZM20 2v4m-2-2h4" />
      </>
    ),
    stop: <rect x="6" y="6" width="12" height="12" rx="2" />,
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name]}
    </svg>
  );
}

async function errorMessage(response: Response) {
  const body = (await response.json().catch(() => ({}))) as {
    detail?: string | { msg: string }[];
  };
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail))
    return body.detail.map((item) => item.msg).join(" ");
  return "The request could not be completed. Please try again.";
}

/** The owner/repo/issues/N label, or null when a stored record has no readable URL. */
function issuePath(issueUrl: string) {
  try {
    return new URL(issueUrl).pathname.slice(1);
  } catch {
    return null;
  }
}

function Citations({
  finding,
  sources,
}: {
  finding: Finding;
  sources: Source[];
}) {
  return (
    <span className="citations">
      {finding.source_ids.map((id) => {
        const source = sources.find((item) => item.id === id);
        return source ? (
          <a
            key={id}
            href={source.url}
            target="_blank"
            rel="noreferrer"
            title={source.title}
          >
            {id}
            <span className="sr-only">: {source.title}</span>
          </a>
        ) : null;
      })}
    </span>
  );
}

export default function Home() {
  const [url, setUrl] = useState("");
  const [health, setHealth] = useState<Health | null>(null);
  const [session, setSession] = useState<AuthSession | null>(null);
  const [requestedAnalysis, setRequestedAnalysis] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [events, setEvents] = useState<Progress[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [connection, setConnection] = useState("");
  const [tab, setTab] = useState("analysis");
  const [draftEdit, setDraftEdit] = useState<DraftEdit | null>(null);
  const [copyFeedback, setCopyFeedback] = useState<CopyFeedback | null>(null);
  const stream = useRef<EventSource | null>(null);
  // Starting a new analysis bumps this, so a slower load of the requested one is ignored.
  const loadVersion = useRef(0);
  const active = submitting || !!(job && !isFinished(job.status));
  const jobIssuePath = job ? issuePath(job.issue_url) : null;
  const signInRequired = session?.enabled && !session.user;
  const loginHref = requestedAnalysis
    ? `/api/v1/auth/github/login?analysis=${requestedAnalysis}`
    : "/api/v1/auth/github/login";
  const result = job?.result;
  const content = result?.content;
  const originalDraft = content?.draft_reply || "";
  const draftText =
    draftEdit && draftEdit.jobId === job?.job_id ? draftEdit.text : originalDraft;
  const draftChanged = draftText !== originalDraft;
  const currentCopyFeedback =
    copyFeedback &&
    copyFeedback.jobId === job?.job_id &&
    copyFeedback.text === draftText
      ? copyFeedback
      : null;
  const copied = currentCopyFeedback?.status === "copied";
  const sources = result?.sources || [];
  const candidates = result?.candidates || [];
  const searches = result?.searches || [];
  const comparisons = content?.comparisons || [];
  const labelRecommendations = content?.label_recommendations || [];
  const issueTemplates = result?.issue_templates || [];
  const limitations = [
    ...new Set([
      ...(result?.limitations || []),
      ...(content?.limitations || []),
    ]),
  ];

  const stopWatching = useCallback(() => {
    stream.current?.close();
    stream.current = null;
  }, []);

  const watch = useCallback((id: string, after: number) => {
    stopWatching();
    let cursor = after;

    function connect() {
      const source = new EventSource(
        `/api/v1/analyses/${id}/events?after=${cursor}`,
      );
      stream.current = source;
      source.onopen = () => setConnection("");
      source.onmessage = (event) => {
        const item = JSON.parse(event.data) as Progress;
        cursor = Math.max(cursor, item.id);
        setEvents((previous) =>
          previous.some((entry) => entry.id === item.id)
            ? previous
            : [...previous, item],
        );
        setJob((previous) =>
          previous && !isFinished(previous.status)
            ? { ...previous, status: "running" }
            : previous,
        );
      };
      const showResult = (completed: Job) => {
        stopWatching();
        setJob(completed);
        setEvents(completed.events);
        setTab("analysis");
        setConnection("");
      };
      source.addEventListener("done", (event) => {
        showResult(JSON.parse((event as MessageEvent).data) as Job);
      });
      source.onerror = async () => {
        if (stream.current !== source) return;
        source.close();
        setConnection("Live connection interrupted. Reconnecting…");
        try {
          const response = await fetch(`/api/v1/analyses/${id}`);
          if (response.ok) {
            const latest = (await response.json()) as Job;
            if (stream.current !== source) return;
            if (isFinished(latest.status)) {
              showResult(latest);
              return;
            }
          } else if (response.status < 500) {
            const message = await errorMessage(response);
            if (stream.current !== source) return;
            stopWatching();
            setConnection("");
            if (response.status === 401) {
              window.location.reload();
              return;
            }
            setError(message);
            if (response.status === 404) {
              setJob(null);
              setEvents([]);
            } else {
              setJob((previous) =>
                previous ? { ...previous, status: "failed" } : previous,
              );
            }
            return;
          }
        } catch {
          /* Retry the stream after a temporary network error. */
        }
        window.setTimeout(() => {
          if (stream.current === source) connect();
        }, 1000);
      };
    }

    connect();
  }, [stopWatching]);

  useEffect(() => {
    let mounted = true;
    const version = loadVersion.current;
    const current = () => mounted && loadVersion.current === version;
    const location = new URL(window.location.href);
    const candidate = location.searchParams.get("analysis");
    const id = candidate && /^[a-f0-9]{32}$/.test(candidate) ? candidate : null;
    setRequestedAnalysis(id);
    if (location.searchParams.get("auth_error") === "github") {
      setError("GitHub sign-in could not be completed. Please try again.");
      location.searchParams.delete("auth_error");
      window.history.replaceState({}, "", location);
    }
    fetch("/api/v1/health")
      .then(async (response) => {
        if (!response.ok) throw new Error(await errorMessage(response));
        return response.json() as Promise<Health>;
      })
      .then((data) => {
        if (mounted) setHealth(data);
      })
      .catch((reason: Error) => {
        if (mounted) setError(reason.message);
      });
    fetch("/api/v1/auth/session")
      .then(async (response) => {
        if (!response.ok) throw new Error(await errorMessage(response));
        return response.json() as Promise<AuthSession>;
      })
      .then(async (data) => {
        if (!mounted) return;
        setSession(data);
        if (!id || (data.enabled && !data.user)) return;
        const response = await fetch(`/api/v1/analyses/${id}`);
        if (!response.ok) throw new Error(await errorMessage(response));
        const analysis = (await response.json()) as Job;
        if (!current()) return;
        setJob(analysis);
        setUrl(analysis.issue_url);
        setEvents(analysis.events);
        if (!isFinished(analysis.status))
          watch(id, analysis.events.at(-1)?.id || 0);
      })
      .catch((reason: Error) => {
        if (current()) setError(reason.message);
      });
    return () => {
      mounted = false;
      stopWatching();
    };
  }, [stopWatching, watch]);

  async function analyze(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (session === null || signInRequired) return;
    loadVersion.current += 1;
    stopWatching();
    setError("");
    setConnection("");
    setDraftEdit(null);
    setCopyFeedback(null);
    setSubmitting(true);
    setJob(null);
    setEvents([]);
    setTab("activity");
    try {
      const response = await fetch("/api/v1/analyses", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ issue_url: url.trim() }),
      });
      if (response.status === 401) {
        window.location.reload();
        return;
      }
      if (!response.ok) throw new Error(await errorMessage(response));
      const data = (await response.json()) as Job;
      setJob(data);
      setEvents(data.events);
      const location = new URL(window.location.href);
      location.searchParams.set("analysis", data.job_id);
      window.history.replaceState({}, "", location);
      watch(data.job_id, data.events.at(-1)?.id || 0);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not start the analysis.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function cancel() {
    if (!job) return;
    try {
      const response = await fetch(`/api/v1/analyses/${job.job_id}`, {
        method: "DELETE",
      });
      if (response.status === 401 || response.status === 404) {
        window.location.reload();
        return;
      }
      if (!response.ok) throw new Error(await errorMessage(response));
      const data = (await response.json()) as Job;
      if (isFinished(data.status)) {
        stopWatching();
        setJob(data);
        setEvents(data.events);
        setConnection("");
      }
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not cancel the analysis.",
      );
    }
  }

  async function signOut() {
    setSigningOut(true);
    try {
      const response = await fetch("/api/v1/auth/logout", { method: "POST" });
      if (!response.ok) throw new Error(await errorMessage(response));
      stopWatching();
      window.location.replace("/");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not sign out.");
      setSigningOut(false);
    }
  }

  async function copyDraft() {
    if (!job || !content || !draftText.trim()) return;
    const copiedDraft = { jobId: job.job_id, text: draftText };
    try {
      await navigator.clipboard.writeText(draftText);
      setCopyFeedback({ ...copiedDraft, status: "copied" });
    } catch {
      setCopyFeedback({ ...copiedDraft, status: "error" });
    }
  }

  return (
    <div className="workspace">
      <header className="site-header">
        <a className="brand" href="/" aria-label="IssueLens home">
          <span className="brand-icon">
            <Icon name="lens" size={22} />
          </span>
          Issue<span className="brand-light">Lens</span>
        </a>
        <div className="header-right">
          {session?.enabled ? (
            session.user ? (
              <div className="account-control">
                <span className="account-name" title={session.user.login}>@{session.user.login}</span>
                <button className="account-action" type="button" onClick={signOut} disabled={signingOut}>
                  Sign out
                </button>
              </div>
            ) : (
              <a className="account-action" href={loginHref}>
                <Icon name="github" size={16} />
                Sign in with GitHub
              </a>
            )
          ) : (
            <>
              <span className="workspace-label">Investigation workspace</span>
              <span className="local-badge">
                <span />
                Local
              </span>
            </>
          )}
        </div>
      </header>

      <main>
        <section className={`intro ${job ? "compact" : ""}`}>
          <div className="eyebrow">
            <span className="tiny-line" /> LESS GUESSWORK. MORE CONTEXT.
          </div>
          <h1>
            A clearer view
            <br className="desktop-break" /> of <span>every issue.</span>
          </h1>
          <p className="intro-copy">
            Turn a GitHub issue into a grounded analysis.
            <br /> Follow the evidence. Find what’s missing. Draft a thoughtful
            reply.
          </p>
        </section>

        <section className="input-card" aria-labelledby="input-heading">
          <div className="input-card-heading">
            <span className="input-icon">
              <Icon name="github" />
            </span>
            <div>
              <h2 id="input-heading">Start with an issue</h2>
              <p>Paste a public GitHub issue URL to investigate.</p>
            </div>
            <span className="read-only">
              <Icon name="check" size={13} /> Read-only
            </span>
          </div>
          <form onSubmit={analyze}>
            <label htmlFor="issue-url" className="sr-only">
              GitHub issue URL
            </label>
            <div className="url-field">
              <Icon name="link" size={19} />
              <input
                id="issue-url"
                name="issue_url"
                type="url"
                required
                placeholder="https://github.com/owner/repo/issues/123"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                disabled={active}
                autoComplete="off"
                spellCheck={false}
              />
              <button
                className="primary-button"
                disabled={active || session === null || signInRequired || health?.openai_configured === false}
                type="submit"
              >
                {active ? (
                  <>
                    <span className="spinner" />
                    Investigating
                  </>
                ) : (
                  <>
                    Analyze issue
                    <Icon name="arrow" size={17} />
                  </>
                )}
              </button>
            </div>
            <div className="input-footnote">
              <span>
                Just exploring?{" "}
                <button
                  type="button"
                  className="text-button"
                  disabled={active}
                  onClick={() => setUrl(EXAMPLE)}
                >
                  Try an example <span>↗</span>
                </button>
              </span>
              <span className="service-status">
                <span
                  className={`status-dot ${health?.openai_configured && session && !signInRequired ? "online" : ""}`}
                />
                {signInRequired ? "Sign in to analyze" : health && session
                  ? health.openai_configured ? "Ready to investigate" : "API key needed"
                  : "Connecting to API"}
              </span>
            </div>
          </form>
          {!signInRequired && health?.openai_configured === false && (
            <p className="config-message">
              Add OPENAI_API_KEY to the project’s .env file and restart FastAPI
              to begin.
            </p>
          )}
        </section>

        {error && (
          <div className="alert" role="alert">
            <span>!</span>
            {error}
            <button onClick={() => setError("")} aria-label="Dismiss error">
              ×
            </button>
          </div>
        )}
        {connection && (
          <p className="connection-note" role="status">
            {connection}
          </p>
        )}

        {!job && !submitting && (
          <section
            className="empty-state"
            aria-label="How the investigation works"
          >
            <div className="empty-illustration" aria-hidden="true">
              <div className="orbit orbit-one" />
              <div className="orbit orbit-two" />
              <div className="orbit-dot" />
              <div className="illustration-file">
                <i />
                <i />
                <i />
                <i />
              </div>
              <span className="illustration-lens">
                <Icon name="lens" size={32} />
              </span>
            </div>
            <h2>Good answers start with good evidence.</h2>
            <p>
              Your investigation will appear here.
              <br />
              Every finding connects back to its source.
            </p>
            <div className="feature-row">
              <span>
                <Icon name="file" size={15} />
                Issue & discussion
              </span>
              <span>
                <Icon name="spark" size={15} />
                Similar issue comparison
              </span>
              <span>
                <Icon name="copy" size={15} />
                Ready-to-copy reply
              </span>
            </div>
          </section>
        )}

        {(job || submitting) && (
          <section className="investigation" aria-label="Investigation result">
            <div className="result-heading">
              <div>
                <div className="eyebrow small">INVESTIGATION</div>
                <h2>
                  {result?.issue?.title ||
                    (active
                      ? "Following the evidence…"
                      : "Investigation overview")}
                </h2>
                <div className="issue-meta">
                  {job && jobIssuePath && (
                    <a href={job.issue_url} target="_blank" rel="noreferrer">
                      <Icon name="github" size={14} />
                      {jobIssuePath} ↗
                    </a>
                  )}
                  {result?.issue && (
                    <span className="issue-state">{result.issue.state}</span>
                  )}
                </div>
              </div>
              <div className="result-actions">
                <span className={`result-status ${job?.status || "queued"}`}>
                  {active && <span className="spinner" />}
                  {job?.status === "completed" ? (
                    <Icon name="check" size={14} />
                  ) : null}
                  {job?.status === "completed"
                    ? "Analysis complete"
                    : job?.status === "partial"
                      ? "Partial analysis"
                      : active
                        ? "In progress"
                        : job?.status || "Starting"}
                </span>
                {active && job && (
                  <button
                    className="icon-button"
                    onClick={cancel}
                    aria-label="Stop analysis"
                    title="Stop analysis"
                  >
                    <Icon name="stop" size={17} />
                  </button>
                )}
              </div>
            </div>
            <div
              className="result-tabs"
              role="tablist"
              aria-label="Investigation views"
            >
              {[
                { id: "analysis", title: "Analysis", icon: "spark" },
                { id: "similar", title: "Similar issues", icon: "file" },
                { id: "labels", title: "Labels", icon: "check" },
                { id: "templates", title: "Templates", icon: "file" },
                { id: "sources", title: "Sources", icon: "link" },
                { id: "activity", title: "Activity", icon: "activity" },
              ].map((item) => (
                <button
                  key={item.id}
                  id={`tab-${item.id}`}
                  role="tab"
                  aria-selected={tab === item.id}
                  aria-controls={`panel-${item.id}`}
                  onClick={() => setTab(item.id)}
                  className={tab === item.id ? "selected" : ""}
                >
                  <Icon name={item.icon as IconName} size={15} />
                  {item.title}
                  {item.id === "sources" && (
                    <span className="count">{sources.length}</span>
                  )}
                  {item.id === "similar" && (
                    <span className="count">{candidates.length}</span>
                  )}
                  {item.id === "labels" && (
                    <span className="count">{labelRecommendations.length}</span>
                  )}
                  {item.id === "templates" && (
                    <span className="count">{issueTemplates.length}</span>
                  )}
                  {item.id === "activity" && active && (
                    <span className="live-dot" />
                  )}
                </button>
              ))}
            </div>

            <div
              role="tabpanel"
              id={`panel-${tab}`}
              aria-labelledby={`tab-${tab}`}
              className="tab-panel"
            >
              {tab === "analysis" && (
                <>
                  {content ? (
                    <div className="analysis-grid">
                      <div className="findings-column">
                        <article className="summary-block">
                          <div className="section-label">
                            <Icon name="spark" size={15} /> THE OVERVIEW
                          </div>
                          <p>
                            {content.summary.text}
                            <Citations
                              finding={content.summary}
                              sources={sources}
                            />
                          </p>
                        </article>
                        <button
                          className="similar-summary"
                          onClick={() => setTab("similar")}
                        >
                          <span>Similar issue check</span>
                          <strong>
                            {searches.length
                              ? `${comparisons.length} compared · ${candidates.length} candidates`
                              : "Search not completed"}{" "}
                            ↗
                          </strong>
                        </button>
                        <button
                          className="similar-summary"
                          onClick={() => setTab("labels")}
                        >
                          <span>Label suggestions</span>
                          <strong>
                            {result?.labels_source_id
                              ? `${labelRecommendations.length} suggestions`
                              : "Catalog not checked"}{" "}
                            ↗
                          </strong>
                        </button>
                        <article className="facts-block">
                          <h3>
                            What we know{" "}
                            <span>{content.known_facts.length}</span>
                          </h3>
                          <ul>
                            {content.known_facts.map((finding, index) => (
                              <li key={index}>
                                <span className="fact-check">
                                  <Icon name="check" size={13} />
                                </span>
                                <div>
                                  {finding.text}
                                  <Citations
                                    finding={finding}
                                    sources={sources}
                                  />
                                </div>
                              </li>
                            ))}
                          </ul>
                        </article>
                        <article className="missing-block">
                          <h3>What’s still missing</h3>
                          <button
                            className="text-button"
                            onClick={() => setTab("templates")}
                          >
                            {result?.templates_source_id
                              ? `Review reporting templates · ${issueTemplates.length} loaded`
                              : "Reporting templates not checked"}{" "}
                            ↗
                          </button>
                          {content.missing_information.length ? (
                            <ol>
                              {content.missing_information.map(
                                (question, index) => (
                                  <li key={index}>
                                    <span>{index + 1}</span>
                                    {question}
                                  </li>
                                ),
                              )}
                            </ol>
                          ) : (
                            <p className="muted">
                              No additional questions were identified from the
                              retrieved evidence.
                            </p>
                          )}
                        </article>
                      </div>
                      <aside className="draft-card">
                        <div className="draft-heading">
                          <span className="draft-icon">
                            <Icon name="copy" size={18} />
                          </span>
                          <div>
                            <h3>A thoughtful next reply</h3>
                          </div>
                        </div>
                        <textarea
                          aria-label="Draft reply"
                          aria-describedby="draft-note"
                          value={draftText}
                          onChange={(event) => {
                            if (!job) return;
                            setDraftEdit({
                              jobId: job.job_id,
                              text: event.target.value,
                            });
                            setCopyFeedback(null);
                          }}
                        />
                        <p className="draft-note" id="draft-note">
                          Edit before sharing. Reloading restores the original
                          draft.
                        </p>
                        <div className="draft-footer">
                          <div className="draft-actions">
                            <button
                              className="text-button draft-reset"
                              disabled={!draftChanged}
                              onClick={() => {
                                setDraftEdit(null);
                                setCopyFeedback(null);
                              }}
                            >
                              Restore original
                            </button>
                            <button
                              className="copy-button"
                              disabled={!draftText.trim()}
                              onClick={copyDraft}
                            >
                              <Icon name={copied ? "check" : "copy"} size={14} />
                              {copied ? "Copied" : "Copy draft"}
                            </button>
                          </div>
                        </div>
                        {currentCopyFeedback?.status === "error" && (
                          <p className="draft-copy-error" role="alert">
                            Clipboard access is unavailable. Select and copy the
                            draft text above.
                          </p>
                        )}
                      </aside>
                    </div>
                  ) : (
                    <div className="pending-state">
                      {active ? (
                        <>
                          <span className="spinner large" />
                          <h3>Building a picture of the issue</h3>
                          <p>
                            The agent is choosing what to read. Follow its
                            progress in Activity.
                          </p>
                          <button
                            className="text-button"
                            onClick={() => setTab("activity")}
                          >
                            View activity →
                          </button>
                        </>
                      ) : (
                        <>
                          <Icon name="file" size={30} />
                          <h3>No final analysis was accepted</h3>
                          <p>
                            {job?.error ||
                              "Review the collected sources and limitations below, then try again."}
                          </p>
                        </>
                      )}
                    </div>
                  )}
                  {limitations.length > 0 && (
                    <div className="limitations">
                      <h3>Scope & limitations</h3>
                      <ul>
                        {limitations.map((item, index) => (
                          <li key={index}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {job?.error && content && (
                    <div className="alert" role="alert">
                      {job.error}
                    </div>
                  )}
                </>
              )}

              {tab === "similar" && (
                <SimilarIssues
                  candidates={candidates}
                  searches={searches}
                  comparisons={comparisons}
                  sources={sources}
                />
              )}

              {tab === "labels" && (
                <LabelRecommendations
                  recommendations={labelRecommendations}
                  labels={result?.repo_labels || []}
                  existing={result?.issue?.labels || []}
                  catalogSourceId={result?.labels_source_id || null}
                  complete={result?.labels_complete ?? null}
                  sources={sources}
                  accepted={Boolean(content)}
                  active={active}
                />
              )}

              {tab === "templates" && (
                <IssueTemplates
                  templates={issueTemplates}
                  lookupSourceId={result?.templates_source_id || null}
                  complete={result?.templates_complete ?? null}
                  sources={sources}
                  active={active}
                />
              )}

              {tab === "sources" && (
                <div className="sources-list">
                  <div className="panel-intro">
                    <h3>The evidence trail</h3>
                    <p>Only successfully retrieved sources appear here.</p>
                  </div>
                  {sources.length ? (
                    sources.map((source) => (
                      <article
                        className="source-card"
                        key={source.id}
                        id={`source-${source.id}`}
                      >
                        <div className="source-top">
                          <span className="source-id">{source.id}</span>
                          <span className="source-kind">
                            {source.kind.replaceAll("_", " ")}
                          </span>
                        </div>
                        <a
                          className="source-title"
                          href={source.url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {source.title}
                          <span>↗</span>
                        </a>
                        <p>{source.excerpt}</p>
                        <a
                          className="source-url"
                          href={source.url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {source.url}
                        </a>
                      </article>
                    ))
                  ) : (
                    <p className="muted">
                      {active
                        ? "Sources will appear when the investigation finishes."
                        : "No sources were retrieved."}
                    </p>
                  )}
                </div>
              )}

              {tab === "activity" && (
                <div className="activity-panel">
                  <div className="panel-intro">
                    <h3>
                      {active ? "Investigation in motion" : "How we got here"}
                    </h3>
                    <p>
                      Actual model requests and tool execution, as they happen.
                    </p>
                  </div>
                  <ol className="timeline" aria-live="polite">
                    {events.map((event) => (
                      <li key={event.id} className={event.kind}>
                        <span className="timeline-marker">
                          <Icon
                            name={
                              event.kind === "tool"
                                ? "file"
                                : event.kind === "model"
                                  ? "spark"
                                  : "activity"
                            }
                            size={13}
                          />
                        </span>
                        <div>
                          <span className="event-kind">
                            {event.kind === "model"
                              ? "Agent"
                              : event.kind === "tool"
                                ? "GitHub tool"
                                : event.kind}
                          </span>
                          <p>{event.message}</p>
                        </div>
                        <time dateTime={event.created_at}>
                          {new Date(event.created_at).toLocaleTimeString(
                            "en-GB",
                          )}
                        </time>
                      </li>
                    ))}
                  </ol>
                  {active && (
                    <div className="activity-working">
                      <span className="spinner" />
                      Waiting for the next update…
                    </div>
                  )}
                </div>
              )}
            </div>
            {result && (
              <div className="result-metrics">
                <span>
                  <strong>{sources.length}</strong> sources
                </span>
                <span>
                  <strong>{result.usage.tool_calls}</strong> tool calls
                </span>
                <span>
                  <strong>{result.usage.model_requests}</strong> model requests
                </span>
                <span>
                  <strong>{result.usage.total_tokens.toLocaleString()}</strong>{" "}
                  tokens
                </span>
                <span className="elapsed">
                  {result.usage.elapsed_seconds.toFixed(1)}s elapsed
                </span>
              </div>
            )}
          </section>
        )}
      </main>
      <footer className="site-footer">
        <span>
          <Icon name="lens" size={14} />A little more context. A much better
          reply.
        </span>
        <span>
          IssueLens <span className="footer-dot">·</span> Local MVP
        </span>
      </footer>
    </div>
  );
}
