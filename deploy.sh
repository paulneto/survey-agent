#!/usr/bin/env bash
# deploy.sh — commit + push + (optionally) trigger a deploy, in one shot.
#
#   ./deploy.sh "your commit message"
#
# Only survey definitions are staged (surveys/, templates/, triggers/ + the surveys' .registry.json;
# override with DEPLOY_PATHS). It refuses to commit if anything else is staged.
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

# Stage ONLY survey definitions — never `git add -A`, which would sweep up respondent uploads, response
# dumps, .env variants or anything else that happens to be lying in the working tree. Override the set
# with DEPLOY_PATHS (space-separated, repo-relative). `git add` still honours .gitignore inside them.
cd "$(git rev-parse --show-toplevel)"
paths="${DEPLOY_PATHS:-surveys templates triggers}"
allowed=()
for p in $paths; do
  case "$p" in /*|*..*) echo "✗ DEPLOY_PATHS entries must be repo-relative: $p" >&2; exit 1 ;; esac
  p="${p%/}"
  [ -e "$p" ] || git ls-files --error-unmatch -- "$p" >/dev/null 2>&1 || continue
  allowed+=("$p")
  git add -A -- "$p"
done
# The file-based lifecycle sidecar is gitignored by default, but a deploy is exactly how "live"
# reaches a file-backed runtime — so it is staged explicitly when it exists.
for p in ${allowed[@]+"${allowed[@]}"}; do
  [ -f "$p/.registry.json" ] && git add -f -- "$p/.registry.json"
done

# Refuse if anything outside the allowed definition paths is staged (by us or left over by you).
outside=""
while IFS= read -r f; do
  [ -z "$f" ] && continue
  ok=""
  for p in ${allowed[@]+"${allowed[@]}"}; do
    case "$f" in "$p"/*|"$p") ok=1; break ;; esac
  done
  [ -n "$ok" ] || outside="$outside  $f"$'\n'
done < <(git diff --cached --name-only)
if [ -n "$outside" ]; then
  echo "✗ refusing to deploy: files outside the definition paths (${allowed[*]:-none}) are staged:" >&2
  printf '%s' "$outside" >&2
  echo "  Commit or unstage them yourself (git restore --staged <file>) and re-run." >&2
  exit 1
fi

git commit -m "$msg" || echo "(nothing to commit — pushing anyway)"
git push

if [ -n "$hook" ]; then
  echo "→ triggering Render deploy hook…"
  curl -fsS "$hook" >/dev/null && echo "✓ deploy triggered"
else
  echo "ℹ no RENDER_DEPLOY_HOOK set — relying on push-triggered auto-deploy (GitHub Action / host webhook)."
fi
