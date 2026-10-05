"""survey-agent engine — the pure-Python, storage/framework-agnostic core.

This package is the part that must be correct. It has no Django/Mongo dependency so it
can be unit-tested, validated, and simulated in isolation, and reused by the Django
runtime, the MCP authoring tools, and the simulator.

Modules:
  ast_eval    condition AST evaluator (logic) — no eval(), conditions are data
  piping      merge-token + option piping (resolved at render, never stored)
  randomize   deterministic, seed-stable randomization with locked positions
  runtime     server-driven page-flow state machine
  validate    publish-time JSON-Schema + semantic lint
  compile_md  authorable Markdown survey -> canonical *.survey.json
  terminal    pure mapping: runtime terminal status -> generic delivery event (integration-agnostic)
"""

from .runtime import SurveyRuntime, unwrap  # noqa: F401

__all__ = ["SurveyRuntime", "unwrap"]
