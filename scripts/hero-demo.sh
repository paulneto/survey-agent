#!/usr/bin/env bash
# survey-agent — hero demo. The whole agentic loop in ~60 seconds, from one sentence.
#
#   Describe a survey  →  an agent drafts it, *stress-tests it on synthetic respondents and fixes
#   its own logic*  →  preview who qualifies with persona respondents  →  open a live report.
#
# Record it:  asciinema rec hero.cast -c "scripts/hero-demo.sh"   (then asciinema upload, or agg -> GIF)
# With ANTHROPIC_API_KEY / OPENAI_API_KEY set you'll see the model self-correct on real coverage gaps;
# with no key it runs fully offline on the deterministic stub (still end-to-end).
set -euo pipefail

BRIEF="${1:-screen US adults who currently pay for a video streaming service, quota 300, route qualifiers to a task}"
OUT="$(mktemp -d)/surveys"
say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$1"; }
run() { printf '\033[2m$ %s\033[0m\n' "$*"; "$@"; }

say "1. Describe the survey in plain English — the agent drafts, validates, and SIMULATES it,"
echo "   feeding any coverage gaps back to itself until every path actually works:"
run survey-agent author "$BRIEF" --out "$OUT" --simulate 300

SURVEY="$(ls "$OUT"/*.survey.json | head -1)"

say "2. Pre-test it with role-played persona respondents — preview real incidence + who screens out,"
echo "   before spending a cent on real people:"
run survey-agent pretest "$SURVEY" --n 6

say "3. Open a self-contained HTML report (funnel, incidence, LOI, per-question charts):"
run survey-agent report "$SURVEY" --simulate 500 --open

say "Done. One sentence → a validated, stress-tested, fieldable survey you own. No GUI, no seat license."
echo "   Take it as a form or a chat:  survey-agent demo --survey \"$SURVEY\"   |   survey-agent convo --survey \"$SURVEY\""
