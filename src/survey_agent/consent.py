"""Default consent / disclosure language for AI processing of respondent data.

Whenever an LLM touches respondent answers — `source: llm` context enrichment, or conversational
mode (where the AI reads replies to record them) — respondents should be told, and (per policy) asked
to consent. These are sensible, plain-English defaults so nothing ships blank; override per survey via
`settings.ai.notice` / `settings.ai.consent_question`, and gate analysis with a spec's
`requires_consent`. This is a starting point for product+legal review, not legal advice.
"""

# Shown on the consent/intro page when LLM analysis is enabled for a form survey.
DEFAULT_AI_NOTICE = (
    "Some of your responses may be analysed by an AI system to help us understand your answers "
    "(for example, summarising open-text feedback). Your responses are processed securely, are not "
    "used to train AI models, and taking part in this analysis is optional."
)

# A ready-made consent question authors can drop in (or point `requires_consent` at an existing one).
DEFAULT_AI_CONSENT_QUESTION = "Do you consent to your responses being analysed by AI, as described above?"

# Shown at the start of a conversational survey (the AI reads your replies to record answers).
DEFAULT_CONVO_NOTICE = (
    "Heads up: this is an AI-assisted conversation — an AI reads your replies to record your answers. "
    "Your responses are processed securely and aren't used to train AI models. You can stop at any time."
)


def ai_settings(survey):
    return (survey.get("settings", {}) or {}).get("ai", {}) or {}


def ai_notice(survey):
    """The survey's AI disclosure text, or the default."""
    return ai_settings(survey).get("notice") or DEFAULT_AI_NOTICE


def convo_notice(survey):
    return ai_settings(survey).get("convo_notice") or DEFAULT_CONVO_NOTICE


def consent_required(survey):
    return bool(ai_settings(survey).get("consent_required"))


def uses_llm(survey):
    """True if any context source is an LLM (so the AI notice applies)."""
    return any(isinstance(s, dict) and s.get("source") == "llm"
               for s in survey.get("settings", {}).get("enrichments", []))
