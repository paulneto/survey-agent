# Survey templates

Reusable starting points for `survey-agent new`. A **template is data** — a validated survey
skeleton with `${param}` tokens — resolved only at scaffold time. The engine, runtime, store, and
export never see a template; they only ever handle a normal `surveys/<slug>.survey.json`.

```bash
survey-agent templates                                   # list everything (core / plugin / overlay)
survey-agent new explainer --slug intro-2026 --title "Welcome"
survey-agent new data-download --slug ai-data-export --title "AI data export" --param service=ChatGPT
```

`new` substitutes params → writes `surveys/<slug>.survey.json` → validates → writes the readable
`.md`. A missing required param or an unresolved `${token}` fails loudly; it never writes a broken
survey.

## Where templates come from (later layers win by id)

| Layer | Where | Who owns it |
|---|---|---|
| **core** | `src/survey_agent/templates/<id>/` (packaged) | the project — brand-neutral reference templates |
| **plugin** | pip package, entry-point group `survey_agent.templates` | a shared / 3rd-party package |
| **overlay** | `./overlay/templates/<id>/`, `./templates/<id>/`, then `$SURVEY_TEMPLATES_DIR` (wins) | the deployer |

Core ships `blank`, `screener`, `explainer`, `data-download` and `feedback-nps`. **Your own branded
templates belong in an overlay, not in the package**: for example `overlay/templates/acme-onboarding/`,
paired with an `overlay/themes/acme/` theme. Git-ignore the overlay or keep it in a private repo. This
is the same core-vs-overlay split that themes, enrichment providers and integrations use. Design
details: [docs/dev/TEMPLATES.md](../docs/dev/TEMPLATES.md).

## Writing a template

A template is one directory:

```
<id>/
  template.yaml      # manifest: id, title, type, description, theme, tags, params, requires
  survey.json        # a survey spec with ${param} tokens (every token sits inside a JSON string)
  README.md          # optional
```

A bare `<id>.survey.json` file also works as a template (the manifest is inferred from the spec).
To turn an existing survey into a template, run `survey-agent templatize <survey>`.

`template.yaml` params:

```yaml
params:
  - { name: slug,  prompt: "Survey id / slug", required: true }   # canonical — sets survey.id
  - { name: title, prompt: "Survey title",     required: true }   # canonical — sets survey.title
  - { name: service, prompt: "Service name", default: "the service" }
requires:
  url_variables: [uid, rid]    # merged into settings.url_variables on scaffold
```

Param values are JSON-escaped on substitution, so free text (quotes, apostrophes) is safe. Drop a
survey's slide images in `assets/<your-slug>/` (served at `/assets/…`) and point params at them.

## Rules (same as any survey)

- A question that branches on a **prior** answer goes on a **later page** (flow is server-driven).
- Display-only types (`image`, `video`, `instructions`) are never required and never validated.
- Declare every URL var you reference in `settings.url_variables`; give every screen-out a
  `disqualify` action with a `reason_code`.
