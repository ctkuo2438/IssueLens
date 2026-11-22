import type { LabelRecommendation, RepositoryLabel, Source } from "./api-types";

function LabelChip({
  name,
  color = "819b65",
}: {
  name: string;
  color?: string;
}) {
  return (
    <span className="label-chip" style={{ borderColor: `#${color}` }}>
      <i style={{ backgroundColor: `#${color}` }} aria-hidden="true" />
      <span>{name}</span>
    </span>
  );
}

export function LabelRecommendations({
  recommendations,
  labels,
  existing,
  catalogSourceId,
  complete,
  sources,
  accepted,
  active,
}: {
  recommendations: LabelRecommendation[];
  labels: RepositoryLabel[];
  existing: string[];
  catalogSourceId: string | null;
  complete: boolean | null;
  sources: Source[];
  accepted: boolean;
  active: boolean;
}) {
  const catalogSource = sources.find((source) => source.id === catalogSourceId);
  const labelByName = (name: string) =>
    labels.find((label) => label.name.toLowerCase() === name.toLowerCase());
  return (
    <div className="labels-panel">
      <div className="panel-intro">
        <h3>Label suggestions</h3>
        <p>
          Additional labels for maintainer review, based on the issue and this
          repository’s catalog.
        </p>
      </div>
      {existing.length > 0 && (
        <div className="existing-labels">
          <span>Already applied</span>
          <div>
            {existing.map((name) => (
              <LabelChip
                key={name}
                name={name}
                color={labelByName(name)?.color}
              />
            ))}
          </div>
        </div>
      )}
      {active ? (
        <p className="muted">
          The label check is in progress. Follow the actual tool calls in
          Activity.
        </p>
      ) : catalogSourceId === null ? (
        <div className="label-empty">
          <h3>Label check not completed</h3>
          <p>
            The repository label catalog was not retrieved. Review Activity and
            the analysis limitations.
          </p>
        </div>
      ) : !accepted ? (
        <div className="label-empty">
          <h3>No final suggestions accepted</h3>
          <p>
            The catalog was retrieved, but the analysis did not produce an
            accepted final answer.
          </p>
        </div>
      ) : !recommendations.length ? (
        <div className="label-empty">
          <h3>No new labels suggested</h3>
          <p>
            {labels.length
              ? "No additional label was supported by the available evidence."
              : "No active repository labels were available in the retrieved catalog."}
          </p>
        </div>
      ) : (
        <div className="label-suggestion-list">
          {recommendations.map((recommendation) => {
            const label = labelByName(recommendation.name);
            return (
              <article
                className="label-suggestion-card"
                key={recommendation.name}
              >
                <LabelChip name={recommendation.name} color={label?.color} />
                {label?.description && (
                  <p className="label-description">{label.description}</p>
                )}
                <p className="label-reason">{recommendation.reason}</p>
                <div className="comparison-evidence">
                  Evidence{" "}
                  <span className="citations">
                    {recommendation.source_ids.map((id) => {
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
              </article>
            );
          })}
        </div>
      )}
      {complete === false && (
        <p className="label-coverage" role="status">
          Partial catalog: suggestions are restricted to the labels shown below.
        </p>
      )}
      {catalogSourceId !== null && (
        <details className="label-catalog">
          <summary>Available labels · {labels.length}</summary>
          {labels.length ? (
            <ul>
              {labels.map((label) => (
                <li key={label.name}>
                  <LabelChip name={label.name} color={label.color} />
                  <p>{label.description || "No description provided."}</p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">
              The retrieved catalog contains no active labels.
            </p>
          )}
        </details>
      )}
      {catalogSource && (
        <p className="catalog-source">
          Catalog source{" "}
          <a href={catalogSource.url} target="_blank" rel="noreferrer">
            {catalogSource.id} · Repository labels ↗
          </a>
        </p>
      )}
    </div>
  );
}
