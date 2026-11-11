export type IssueComparison = {
  issue_number: number;
  relationship:
    "possible_duplicate" | "related_but_different" | "insufficient_evidence";
  reason: string;
  similarities: string[];
  differences: string[];
  source_ids: string[];
};

export type IssueCandidate = {
  number: number;
  title: string;
  url: string;
  state: string;
  source_id: string | null;
};

export type IssueSearch = {
  query: string;
  total_count: number;
  incomplete_results: boolean;
  candidate_numbers: number[];
  source_id: string;
};

const LABELS = {
  possible_duplicate: "Possible duplicate",
  related_but_different: "Related but different",
  insufficient_evidence: "Insufficient evidence",
};

export function SimilarIssues({
  candidates,
  searches,
  comparisons,
  sources,
}: {
  candidates: IssueCandidate[];
  searches: IssueSearch[];
  comparisons: IssueComparison[];
  sources: { id: string; url: string; title: string }[];
}) {
  const comparedNumbers = new Set(comparisons.map((item) => item.issue_number));
  const orderedCandidates = [...candidates].sort(
    (left, right) =>
      Number(comparedNumbers.has(right.number)) -
      Number(comparedNumbers.has(left.number)),
  );
  return (
    <div className="similar-panel">
      <div className="panel-intro">
        <h3>Similar issue investigation</h3>
        <p>
          Keyword matches are candidates. Each accepted comparison cites both
          issue bodies.
        </p>
      </div>
      <div className="similar-counts">
        <span>
          <strong>{searches.length}</strong> searches
        </span>
        <span>
          <strong>{candidates.length}</strong> candidates
        </span>
        <span>
          <strong>{comparisons.length}</strong> compared
        </span>
      </div>
      {!searches.length ? (
        <p className="muted">
          The similar-issue search has not completed. Check Activity and the
          analysis limitations.
        </p>
      ) : !candidates.length ? (
        <div className="similar-empty">
          <h3>No candidates found for these keywords</h3>
          <p>
            The recorded searches found no eligible issues in this repository.
            Different wording may return other results; this does not establish
            that no duplicate exists.
          </p>
        </div>
      ) : null}
      {searches.length > 0 && (
        <details className="search-scope">
          <summary>Search scope & queries</summary>
          <p>
            The target issue, pull requests and results from other repositories
            are excluded.
          </p>
          <ul>
            {searches.map((search, index) => {
              const source = sources.find(
                (item) => item.id === search.source_id,
              );
              return (
                <li key={index}>
                  <code>{search.query}</code>
                  <span>
                    {search.total_count} GitHub matches ·{" "}
                    {search.candidate_numbers.length} retained candidates
                    {search.incomplete_results
                      ? " · GitHub reported incomplete results"
                      : ""}
                  </span>
                  {source && (
                    <a href={source.url} target="_blank" rel="noreferrer">
                      Open search ↗
                    </a>
                  )}
                </li>
              );
            })}
          </ul>
        </details>
      )}
      <div className="candidate-list">
        {orderedCandidates.map((candidate) => {
          const comparison = comparisons.find(
            (item) => item.issue_number === candidate.number,
          );
          return (
            <article className="candidate-card" key={candidate.number}>
              <div className="candidate-heading">
                <span className="candidate-number">
                  #{candidate.number} · {candidate.state}
                </span>
                <span
                  className={`comparison-status ${comparison?.relationship || "uncompared"}`}
                >
                  {comparison
                    ? LABELS[comparison.relationship]
                    : "Not compared"}
                </span>
              </div>
              <a
                className="source-title"
                href={candidate.url}
                target="_blank"
                rel="noreferrer"
              >
                {candidate.title}
                <span>↗</span>
              </a>
              {comparison ? (
                <>
                  <p className="comparison-reason">{comparison.reason}</p>
                  <div className="comparison-columns">
                    <div>
                      <h4>Similarities</h4>
                      {comparison.similarities.length ? (
                        <ul>
                          {comparison.similarities.map((item, index) => (
                            <li key={index}>{item}</li>
                          ))}
                        </ul>
                      ) : (
                        <p>No concrete similarities established.</p>
                      )}
                    </div>
                    <div>
                      <h4>Differences & uncertainty</h4>
                      {comparison.differences.length ? (
                        <ul>
                          {comparison.differences.map((item, index) => (
                            <li key={index}>{item}</li>
                          ))}
                        </ul>
                      ) : (
                        <p>
                          No concrete differences established; the cause may
                          still be unverified.
                        </p>
                      )}
                    </div>
                  </div>
                  <div className="comparison-evidence">
                    Evidence{" "}
                    <span className="citations">
                      {comparison.source_ids.map((id) => {
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
                  </div>
                </>
              ) : (
                <p className="comparison-reason muted">
                  {candidate.source_id
                    ? "The issue body was retrieved, but no final comparison was accepted."
                    : "Found in keyword search. Its body has not been read for comparison."}
                </p>
              )}
            </article>
          );
        })}
      </div>
    </div>
  );
}
