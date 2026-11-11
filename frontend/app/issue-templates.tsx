export type IssueTemplate = {
  path: string;
  name: string;
  description: string;
  format: "form" | "markdown";
  source_id: string;
  requirements_known: boolean;
  notes: string[];
  fields: {
    label: string;
    description: string;
    kind: string;
    required: boolean | null;
    required_options: string[];
  }[];
};

type TemplateSource = {
  id: string;
  title: string;
  url: string;
  excerpt: string;
};

export function IssueTemplates({
  templates,
  lookupSourceId,
  complete,
  sources,
  active,
}: {
  templates: IssueTemplate[];
  lookupSourceId: string | null;
  complete: boolean | null;
  sources: TemplateSource[];
  active: boolean;
}) {
  const lookup = sources.find((source) => source.id === lookupSourceId);
  return (
    <div className="templates-panel">
      <div className="panel-intro">
        <h3>Reporting templates</h3>
        <p>
          Repository guidance to help review missing information. Match the
          template to the issue’s purpose.
        </p>
      </div>
      {active ? (
        <p className="muted">
          Follow actual template tool calls in Activity. The agent chooses
          whether reporting requirements need checking.
        </p>
      ) : !lookupSourceId ? (
        <div className="label-empty">
          <h3>Templates not checked</h3>
          <p>
            No successful template lookup was recorded. Review Activity for
            skipped or failed checks.
          </p>
        </div>
      ) : !templates.length ? (
        <div className="label-empty">
          <h3>
            {complete === false
              ? "Template coverage incomplete"
              : "No templates in the checked directory"}
          </h3>
          <p>
            {complete === false
              ? "No template could be fully included within this run’s limits. Read the lookup source and analysis limitations."
              : "No supported templates were found in this repository’s .github/ISSUE_TEMPLATE directory. Legacy and inherited templates were not searched."}
          </p>
        </div>
      ) : (
        <div className="template-list">
          {templates.map((template) => {
            const source = sources.find(
              (item) => item.id === template.source_id,
            );
            return (
              <article className="template-card" key={template.path}>
                <div className="template-heading">
                  <div>
                    <h3>{template.name}</h3>
                    <p>{template.description || template.path}</p>
                  </div>
                  <span className="template-format">
                    {template.format === "form" ? "YAML form" : "Markdown"}
                  </span>
                </div>
                {!template.requirements_known && (
                  <p className="template-note">
                    {template.format === "markdown"
                      ? "Required flags are not structured in Markdown. Headings alone do not make fields mandatory."
                      : "Some form requirements remain unknown. Review the source before treating a field as mandatory."}
                  </p>
                )}
                {template.fields.length > 0 && (
                  <ul className="template-fields">
                    {template.fields.map((field, index) => {
                      const status =
                        field.required_options.length || field.required === true
                          ? "Required"
                          : field.required === false
                            ? "Optional"
                            : "Unknown";
                      return (
                        <li key={index}>
                          <div className="template-field-title">
                            <strong>{field.label}</strong>
                            <span
                              className={`requirement-badge ${status.toLowerCase()}`}
                            >
                              {status}
                            </span>
                          </div>
                          {field.description && <p>{field.description}</p>}
                          {field.required_options.length > 0 && (
                            <p>
                              Required checkbox options:{" "}
                              {field.required_options.join("; ")}
                            </p>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
                {template.notes.map((note, index) => (
                  <p className="template-note" key={index}>
                    {note}
                  </p>
                ))}
                {source && (
                  <>
                    <p className="catalog-source">
                      Template source{" "}
                      <a href={source.url} target="_blank" rel="noreferrer">
                        {source.id} · {template.path} ↗
                      </a>
                    </p>
                    <details className="template-excerpt">
                      <summary>Retrieved excerpt</summary>
                      <pre>{source.excerpt}</pre>
                    </details>
                  </>
                )}
              </article>
            );
          })}
        </div>
      )}
      {complete === false && (
        <p className="label-coverage" role="status">
          Partial coverage: file count, content limits or parsing prevented a
          complete check of the supported directory.
        </p>
      )}
      <p className="template-scope">
        Current default branch · up to two files · repository directory only.
        Template requirements do not prove that the issue omitted the
        information.
      </p>
      {lookup && (
        <p className="catalog-source">
          Lookup source{" "}
          <a href={lookup.url} target="_blank" rel="noreferrer">
            {lookup.id} · Issue template chooser ↗
          </a>
        </p>
      )}
    </div>
  );
}
