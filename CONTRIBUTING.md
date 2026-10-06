# Contributing to survey-agent

Thanks for considering a contribution! survey-agent is a small, deliberately boring **engine** with a
lot of agentic surface around it. The engine is about 2,700 lines of pure Python and the bar for
changing it is high; most contributions are new *plugins* (a store, an integration, a theme, a context source) or new
*question types*, which the architecture makes cheap and safe to add.

## The agentic route

You're welcome to extend the project by **describing the change to an agent that scaffolds and tests
it**. Either way, the same machine gate is the merge requirement:

> **schema + semantic lint + tests + a simulation coverage gate** must all pass.

You don't have to work that way, but the gate is the same either way: if those four pass and the
engine stays pure, a change is in good shape.

## Dev setup

```bash
git clone https://github.com/paulneto/survey-agent && cd survey-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,llm,mcp]"
pytest -q
```

Everything runs **offline** without an API key (a deterministic stub backs the AI features); set
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` to exercise the real LLM paths.

## Architecture rules (please don't break these)

- **`survey_agent.engine` is pure Python.** Standard library plus `jsonschema`, `PyYAML` and
  `regex`; no database, network, web-framework or LLM imports. It's the correctness core, reused by
  the runtime, simulator, conversation, MCP, and demo. I/O lives in `store/`, `definitions/`, `enrich/`,
  `integrations/`, and the API gateway (`api/`).
- **Logic is data, never code.** Conditions are the bounded AST in `engine/ast_eval.py`: no `eval`,
  no scripting. New operators go in the closed set + the schema + the validator + a test.
- **Definitions are immutable + versioned**; the JSON Schema (`engine/schema/survey.schema.json`) is a
  real gate (`additionalProperties:false`, so declare new fields before using them).
- **Display-only types** (`image`, `video`, `instructions`) are stimulus: never required, never
  validated.
- **Repeated answers are ordinary answers** (`qid[item]`), so logic, piping, quotas and export need
  no special case. Keep it that way.
- **Keep the open core free of any deployer's *panel* names and vocabulary.** Generic third-party
  services (an ESP like Mailchimp, a warehouse) can ship as reference adapters; a specific panel
  belongs in a separate plugin.

## Adding things (the common cases)

- **A question type** → engine (`runtime.py` validation), the renderer (`server.py`), the simulator
  (`simulate.py answer_question`), export (`exporting.py` / `engine/export.py`), the schema, **and a
  test**. Run `validate` + `simulate` on a survey that uses it.
- **A store** → implement the store contract (the methods listed in `store/__init__.py`, including
  `reserve_quotas` for atomic all-or-nothing quota reservation, and `find_respondent` /
  `erase_respondent` for DSAR access and erasure across responses and sessions) and point
  `SURVEY_STORE` at `your_pkg.module:YourStore`. Built in:
  `memory`, `file` (the default), `sqlite`, `http`, plus a reference `mongo`; more database adapters
  belong in their own packages.
- **An integration** (passback / CRM) → implement `Integration` (`integrations/base.py`); register via
  the `survey_agent.integrations` entry-point group, or add a built-in. `mailchimp.py` is a worked example.
- **A context source** (enrichment) → implement a provider with `resolve(uid, specs)` (see
  `enrich/__init__.py`) and select it with `SURVEY_ENRICH=your_pkg.module:YourProvider`.
- **A theme** → add `themes/<name>/tokens.json` (or point `SURVEY_THEMES_DIR` at your own folder).

## Before you open a PR

1. `pytest -q` is green.
2. `survey-agent validate surveys/*.survey.json` is clean; keep each `.md` ⇄ `.survey.json` in sync
   (`survey-agent compile`), and run `survey-agent simulate <survey> -n 500` if you touched logic.
3. New surveys/types: also run `survey-agent critique` and address high-severity items.
4. The engine stayed pure; no deployer-specific panel names landed in core.
5. If you changed docs: `pytest -q tests/test_docs.py` checks the doc map and every relative link and
   anchor.
6. Keep commits focused; describe the *why*. CI runs the suite + survey validation on every push.

To try the whole product end-to-end as a new user, follow [docs/QUICKSTART.md](docs/QUICKSTART.md)
and then [demo-survey-agent.md](demo-survey-agent.md).

By contributing you agree your work is licensed under the project's [Apache License 2.0](LICENSE), and to
abide by our [Code of Conduct](CODE_OF_CONDUCT.md). Security issues: see [SECURITY.md](SECURITY.md),
and please don't open public issues for vulnerabilities. Deeper architecture notes:
[docs/ENGINEERING.md](docs/ENGINEERING.md).
