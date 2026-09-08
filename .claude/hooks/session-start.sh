#!/bin/sh
# SessionStart — inject repo state so a fresh context starts oriented.
# Adapted from claude-remote-session-webhook's .claude/hooks/session-start.sh
# (lm-02: propagate the crswd guardrails to the repos that had none).
#
# Contract (https://code.claude.com/docs/en/hooks):
#   SessionStart cannot block. To put text in front of Claude, print JSON on
#   stdout with hookSpecificOutput.additionalContext and exit 0.
set -u

REPO_ROOT=${CLAUDE_PROJECT_DIR:-$(pwd)}
cd "$REPO_ROOT" 2>/dev/null || exit 0

BUF=""
add() { BUF="${BUF}$1
"; }

add "## Repo state (injected by .claude/hooks/session-start.sh)"
add ""

# --- git ---------------------------------------------------------------------
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || printf 'unknown')
  add "Branch: ${BRANCH}"
  STATUS=$(git status --short 2>/dev/null | head -40)
  if [ -n "$STATUS" ]; then
    add ""
    add "Uncommitted changes (git status --short):"
    add '```'
    add "$STATUS"
    add '```'
  else
    add "Working tree clean."
  fi
fi

# --- open TODOs --------------------------------------------------------------
# `cut -c1-200` is load-bearing, not cosmetic: a generated/minified file (a
# sourcemap, a built mkdocs site, a data dump) can match TODO/FIXME inside a
# single enormous "line" with no real newlines, and grep -n prints the whole
# thing. Measured while testing lm-02: one such match alone injected 1.7MB of
# additionalContext into every session start for a docs repo. The exclude-dir
# list is defense in depth; the byte cap is what actually bounds the worst case.
if command -v grep >/dev/null 2>&1; then
  TODOS=$(grep -rIn -E '(TODO|FIXME|XXX|HACK)' . \
            --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=dist \
            --exclude-dir=build --exclude-dir=vendor --exclude-dir=.venv \
            --exclude-dir=site --exclude-dir=.wrangler --exclude-dir=__pycache__ \
            --exclude-dir=target --exclude-dir=.next --exclude-dir=coverage \
            --exclude-dir=data --exclude-dir=images --exclude-dir=cloud-images \
            --exclude-dir=assets \
            2>/dev/null | cut -c1-200 | head -15)
  if [ -n "$TODOS" ]; then
    add ""
    add "Open TODO/FIXME markers:"
    add '```'
    add "$TODOS"
    add '```'
  fi
fi

# --- the standing reminder ---------------------------------------------------
add ""
add "### Before you touch anything"
add "1. Read AGENTS.md or README.md (root) if present -- repo-specific contract."
add "2. READ-ONLY CLI against the live custeng-prod Palette tenant by default. No verb in this tool may create, update, delete, or deploy anything -- that was deliberately left out (see README 'Future work'). If a change would add a write verb, stop and flag it rather than adding it."
add "3. Hooks in .claude/settings.json are enforced. Do not try to route around them."

# Emit as JSON if we can; otherwise plain stdout still reaches the transcript.
if command -v jq >/dev/null 2>&1; then
  printf '%s' "$BUF" | jq -Rs '{
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext: .
    }
  }'
elif command -v python3 >/dev/null 2>&1; then
  printf '%s' "$BUF" | python3 -c 'import sys,json
print(json.dumps({"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":sys.stdin.read()}}))'
else
  printf '%s' "$BUF"
fi

exit 0
