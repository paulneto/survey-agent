#!/usr/bin/env bash
# deploy.sh — commit + push + (optionally) trigger a deploy, in one shot.
#
#   ./deploy.sh "your commit message"
#
# Pushing to your deploy branch is what publishes: your host (e.g. Render) redeploys on push, and a
# survey only goes live once that deploy finishes. Render's *native* auto-deploy webhook can be flaky,
# so the reliable pattern is a GitHub Action that calls a Render Deploy Hook on push (see docs/DEPLOY.md,
# Appendix A). For manual/local use, set RENDER_DEPLOY_HOOK (e.g. in .env) and this also pings the hook
# directly so a deploy fires immediately regardless of the host's webhook. This is the same publish
# action you'd wire into `survey-agent launch --deploy` via `deploy.command`.
set -euo pipefail

msg="${1:-deploy}"

# Resolve the deploy hook: an exported env var wins, else read it from .env (never printed).
hook="${RENDER_DEPLOY_HOOK:-}"
if [ -z "$hook" ] && [ -f .env ]; then
  hook="$(sed -n 's/^RENDER_DEPLOY_HOOK=//p' .env | tail -1)"
  hook="${hook%\"}"; hook="${hook#\"}"          # strip surrounding double-quotes if any
fi

git add -A
git commit -m "$msg" || echo "(nothing to commit — pushing anyway)"
git push

if [ -n "$hook" ]; then
  echo "→ triggering Render deploy hook…"
  curl -fsS "$hook" >/dev/null && echo "✓ deploy triggered"
else
  echo "ℹ no RENDER_DEPLOY_HOOK set — relying on push-triggered auto-deploy (GitHub Action / host webhook)."
fi
