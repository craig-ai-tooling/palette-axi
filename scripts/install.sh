#!/usr/bin/env bash
# Install the latest palette-axi single-file build onto your PATH.
#
# This repo is PRIVATE — there is no public release URL to `curl`, so this
# pulls the .pyz via the GitHub CLI instead. Needs `gh` authenticated with
# access to craig-ai-tooling/palette-axi (`gh auth status`), and a Python
# 3.10+ interpreter on the target (every lab box has one).
#
#   ./scripts/install.sh
#   BIN=/usr/local/bin/palette-axi ./scripts/install.sh   # custom target
set -euo pipefail

REPO="craig-ai-tooling/palette-axi"
BIN="${BIN:-$HOME/.local/bin/palette-axi}"

if ! command -v gh >/dev/null 2>&1; then
  echo "error: gh (GitHub CLI) is required — this is a private repo, so there is" >&2
  echo "       no public release URL to curl. Install gh and run 'gh auth login'." >&2
  exit 1
fi

mkdir -p "$(dirname "$BIN")"
gh release download --repo "$REPO" --pattern 'palette-axi.pyz' --output "$BIN" --clobber
chmod +x "$BIN"
"$BIN" --help >/dev/null
echo "installed $BIN"
echo "next: $BIN doctor"
