# Survey templates and scaffolding

How survey-agent scaffolds a new survey from a reusable starting point, and how a deployer adds
their own templates without changing the open core.

For a quick how-to, see [templates/README.md](../../templates/README.md). This page covers the design.

---

## 1. What it's for

- **Scaffold by survey type.** "Give me a screener", "an onboarding explainer", "a data-download
  study", "an NPS check": each becomes a validated `surveys/<slug>.survey.json` you then refine.
- **Keep the core generic.** The mechanism and a few brand-neutral reference templates ship in the
  package. A deployer's own templates (their branding, copy and URL-parameter contract) live outside
  the package and take precedence.

Templates are a seam like the others (`SURVEY_STORE`, `SURVEY_INTEGRATION`, `SURVEY_ENRICH`, themes):
the core ships the loader and reference content, and a deployer supplies their own. See
[DESIGN_DECISIONS.md](DESIGN_DECISIONS.md#nothing-brand--or-vendor-specific-in-the-core).

## 2. What a template is

A template is **data, not code**: a flat-file artifact like a theme or a survey. One template is one
directory:

```
<template-id>/
  template.yaml        # manifest: metadata, type, params, requirements
  survey.json          # the spec, with ${param} tokens (a valid survey once substituted)
  README.md            # optional: how to use / customize
```

A bare `<template-id>.survey.json` file also works; its manifest is inferred from the spec (title,
description, first tag as the type, theme), and only the canonical `slug` / `title` params apply.

Templates are resolved only at **scaffold time** (`survey-agent new`). The engine, runtime, store and
export never see a template, only the normal `*.survey.json` it produces.

## 3. Where templates come from

Three layers. **Later layers win by `id`**, so a deployer can shadow a core template without editing
the package.

| Layer | Where | Who owns it |
|---|---|---|
| **core** | `src/survey_agent/templates/` (packaged) | the project: `blank`, `screener`, `explainer`, `data-download`, `feedback-nps` |
| **plugin** | pip packages registering the entry-point group `survey_agent.templates` | a shared or third-party package |
| **overlay** | `./overlay/templates`, `./templates`, then `$SURVEY_TEMPLATES_DIR` (path-separated; wins) | the deployer |

A plugin entry point resolves to a directory path (or a callable returning one). A malformed template
is skipped rather than breaking discovery of the others. `survey-agent templates` lists every template
with its `source` (`core`, `plugin` or `overlay`).

## 4. The manifest (`template.yaml`)

```yaml
id: explainer
title: Onboarding / explainer
type: explainer                 # discovery category (see §5)
description: A multi-page explainer with a required agreement, optional contact, and a thank-you.
theme: paper
tags: [onboarding, intro]

params:                         # declared substitution variables: the only tokens allowed in survey.json
  - { name: slug,  prompt: "Survey id / slug", required: true }
  - { name: title, prompt: "Survey title",     required: true }
  - { name: asset_base, prompt: "Asset folder under /assets", default: "${slug}" }

requires:
  url_variables: [uid, rid]     # merged into settings.url_variables on scaffold (de-duplicated)
```

`slug` and `title` are canonical: they set the new survey's `id` and `title`. `type` is discovery
metadata; the engine ignores it. Of the `requires` block, `url_variables` is applied on scaffold.

## 5. Survey types

`type` groups templates so `survey-agent templates --type screener` (and an agent) can find the right
starting point. The core set, which a deployer can extend:

| type | shape |
|---|---|
| `blank` | minimal valid single-page survey |
| `screener` | qualifying questions + `disqualify` actions + a route-onward `complete` |
| `explainer` | instruction pages + agreement + optional contact + thanks |
| `data-download` | screen for use of a service → step-by-step data-export request → confirm → route onward |
| `feedback-nps` | NPS + reason + satisfaction |

## 6. Parameters are deliberately simple

- **Declared params only.** `survey.json` may contain `${name}` tokens only for names declared in
  `params`. Every token sits inside a JSON string.
- **Plain substitution, then validation.** Values are JSON-escaped, so free text with quotes or
  apostrophes is safe. A default can reference another param (`default: "${slug}"`). An unresolved
  token, a missing required param, or a result that isn't valid JSON fails loudly; `new` never writes
  a broken survey.
- **No template language.** No conditionals, loops, includes or expressions. Branching belongs in the
  survey's own logic, authored after scaffolding.
- **`asset_base`-style params decouple assets**, so a copied template points at `/assets/<slug>/…`
  rather than the original survey's folder. Assets are served from `$SURVEY_ASSETS_DIR` (default
  `./assets`).

## 7. CLI and MCP surface

```bash
survey-agent templates [--type T] [--json]       # list: id · type · source · description
survey-agent new <template-id> --slug <slug> \   # scaffold a survey from a template
     [--title "…"] [--param k=v]… [--out DIR] [--simulate N] [--force]
survey-agent templatize <survey> [--id ID] [--out templates]   # turn an existing survey into a template
survey-agent copy <survey> --slug <new-slug>     # duplicate a survey (no template involved)
```

`new` resolves the template, collects params, substitutes, writes `<slug>.survey.json`, validates,
writes the readable `.md`, and optionally runs a coverage simulation. `templatize` is the inverse: it
tokenizes the survey's `id` / `title` to `${slug}` / `${title}`, lifts its theme and URL variables
into a generated `template.yaml`, and checks that the result renders.

Over MCP (see [AGENTIC_MODES.md](../AGENTIC_MODES.md)): the `list_templates`, `new_survey`,
`templatize_survey` and `copy_survey` tools, and the `survey://templates` resource.

`init` scaffolds a *workspace*; `new` scaffolds a *survey* inside one; `author` *generates* a survey
from a brief. They compose.

## 8. Keeping custom content out of the core

| Artifact | Home |
|---|---|
| template loader, `new` / `templates` CLI, MCP tools | core |
| `blank` / `screener` / `explainer` / `data-download` / `feedback-nps` | core: native text, default theme, no image assets |
| your brand theme | `$SURVEY_THEMES_DIR` or `./overlay/themes/<name>/` |
| your branded templates | `$SURVEY_TEMPLATES_DIR`, `./overlay/templates/` or a plugin package |
| your images (banners, slides, logos) | `$SURVEY_ASSETS_DIR` |
| passback to your panel or CRM | an integration plugin (`SURVEY_INTEGRATION`) |

Core reference templates carry no images. They use `instructions` pages (Markdown) and the default
theme, so they validate and demo with no asset dependencies. Image-heavy branded templates belong in
your overlay.

### Overlay shapes

- **A local directory** (the simplest):
  ```
  overlay/
    templates/onboarding-branded/{template.yaml,survey.json}
    themes/acme/{tokens.json,overrides.css}
  assets/<survey-slug>/*.png
  ```
  Git-ignore it or keep it in a private repository.
- **A private pip package** that registers a `survey_agent.templates` entry point. Best when several
  deployments share the same templates.

## 9. Worked example: a branded onboarding template

`overlay/templates/onboarding-branded/template.yaml`:

```yaml
id: onboarding-branded
title: Branded onboarding explainer
type: explainer
theme: acme                       # an overlay theme, not in core
description: Full-bleed banner + brand slides, required agreement, optional contact, thank-you.
params:
  - { name: slug,       required: true }
  - { name: title,      required: true }
  - { name: asset_base, default: "${slug}" }
requires:
  url_variables: [uid, rid, group]
```

In its `survey.json`, image URLs use `/assets/${asset_base}/banner.png`, and `id` / `title` are
`${slug}` / `${title}`. Then:

```bash
survey-agent new onboarding-branded --slug welcome-uk --title "Welcome"
```

reproduces the branded survey for a new market, with nothing brand-specific in the package.

## 10. Design notes

- **Non-goals.** No template editor UI, no template inheritance or partials, no expression language,
  no runtime template resolution. Templates are pre-filled `survey.json`, nothing more, and they don't
  add question or logic capabilities.
- **Precedence.** A local overlay beats a plugin with the same id, because it's the most specific to
  the deployment; `$SURVEY_TEMPLATES_DIR` beats both.
- **Required URL variables are merged**, not just warned about, so a scaffolded survey declares every
  variable its template expects.
- **Configuration is environment-based.** Template and theme locations come from environment variables
  and the conventional `./overlay` directories, not from `survey-agent.config.yaml`.
