# AGENTS.md

Primary context file for **every** AI agent working in this repo (Claude Code, Copilot,
Cursor, Codex, or a human). Read this first.

---

## no-mistakes mode

This repo touches a **live, customer-facing API** (`api.spectrocloud.com`, tenants like
`custeng-prod`). It runs in **no-mistakes mode**: verb behavior, output format, and API
calls against the live Palette API must **not change without evidence** (a live probe, a
failing test, a confirmed transcript) — not on a hunch, not for style. Packaging, tests,
docs, and the `doctor` subcommand are fair game; the existing verbs' request/response
handling is not, unless something has actually changed on the Palette side.

---

## Why

Agent-ergonomic CLI over the Spectro Cloud Palette API: TOON output, pre-computed
aggregates, definitive empty states, structured exit codes, next-step disclosure.
**READ-ONLY** — no verb creates, updates, deletes, or deploys anything. Sibling to
`launchpad-axi` / `opp-axi`; same AXI (Agent eXperience Interface) conventions and the
same packaging standard.

---

## What — project map

| Path | What lives here |
|---|---|
| `palette_axi/` | Application code (`cli.py` has everything; `__init__.py` holds `__version__`) |
| `palette_axi/axi.py` | Exit codes and TOON output helpers, vendored from `craig-ai-tooling/axi-py` (`make vendor-axi`) — never edited here |
| `tests/` | Offline unit tests — no network, no 1Password |
| `.github/workflows/` | CI (`ci.yml`) + release (`release.yml`, fires on a `v*` tag) |
| `scripts/install.sh` | Installs the released `.pyz` by curling the public release asset |

---

## How — the only commands that matter

| Task | Command |
|---|---|
| Build (zipapp) | `make build` → `dist/palette-axi.pyz` |
| Install | `make install` |
| Test | `python3 -m unittest discover -s tests` |
| Lint | `ruff check .` |
| Compile check | `python3 -m compileall palette_axi` |
| Doctor (connector health) | `palette-axi doctor` / `./dist/palette-axi.pyz doctor --json` |

**Definition of done** — build, test, lint all pass, and every verb's `--help` still
works. CI runs exactly these commands.

---

## Conventions

**Naming** — one handler per subcommand, named `cmd_<verb>`, dispatched by argparse.

**Errors** — never swallow. `die(msg, code)` prints `error: <msg>` to stderr and exits
with one of the exit codes below. Per-row/list failures are reported, never dropped.

**Output** — TOON (`toon()`/`emit()`/`nxt()`), vendored from `craig-ai-tooling/axi-py`
into `palette_axi/axi.py`; never edited here. `None` and `False` are different facts
and must render differently.

**Tests** — offline; the network layer (`api()`, `get_api_key()`, `_op_*`) is
monkeypatched or stubbed. A test that only checks the source text for the right endpoint
string is not enough — assert on the request actually issued (see `tests/test_palette_axi.py`
for the pattern after two endpoint migrations bit this tool live).

**`doctor`** — one connector table in `cli.py` (`DOCTOR_CONNECTORS`). Every probe must:
catch its own exceptions rather than crash the command, never print a secret value
(`PALETTE_API_KEY` shows `set`/`unset` only), and put the exact fix (a command or env
var) in `detail` whenever status isn't `ok`. Exit 0 iff every *required* connector is
`ok`; exit 1 otherwise. Adding a connector means adding one entry to that table plus a
probe function — not touching the existing verbs.

**Comments** — explain *why*, especially the live-API gotchas (see README's "Real API
behavior discovered while building this"). If a comment would just restate the code,
delete it.

---

## Hard rules

- **Never commit secrets.** No API keys, no 1Password item contents, no `.env` files.
- **Never push to `main`.** Branch, PR, review.
- **Never change a verb's request, response handling, or output for convenience.**
  If the live API changed, say so with evidence (in the commit message and, if it's a
  gotcha worth remembering, in README's "Real API behavior" section) — don't just patch
  around a symptom.
- **Never leave the tree broken.** Build + test + lint green before you commit.
- **The API key is the credential.** Never log it, never commit it, never print it —
  `doctor` included.
