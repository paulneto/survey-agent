<!-- Thanks for contributing to survey-agent! See CONTRIBUTING.md. -->

## What & why
<!-- What does this change, and why? Link any issue. -->

## Checklist
- [ ] `pytest -q` is green
- [ ] `survey-agent validate surveys/*.survey.json` is clean (and `.md` ⇄ `.survey.json` in sync if touched)
- [ ] Ran `survey-agent simulate <survey> -n 500` if I changed logic, and `critique` for new surveys/types
- [ ] The engine (`src/survey_agent/engine/`) stayed pure — no storage, network, web-framework or LLM imports, no `eval`
- [ ] No deployer/vendor *panel* names added to the open core
- [ ] Schema/docs/tests updated for any new field or capability
