# Progress — lm-91 (extract the shared AXI module)

Append one entry per iteration, newest at the bottom, using the format in
`ralph/PROMPT.md`. This file is the only memory the loop carries between
iterations — write for a reader who knows nothing about this repo.

## Starting state (planning pass, no code changed)

- `palette-axi` is one 622-line stdlib-only Python 3 executable. Lines 45-112 are
  the block duplicated with `opp-axi`: the `E_*` exit codes, then `_tv`, `toon`,
  `emit`, `nxt`, `die`, `size_hint`, `trunc`, `ts`.
- `python3 -m unittest test_palette_axi` runs 25 tests, all passing. That number
  is the floor; nothing in this milestone may reduce it.
- `axi_common.py`, `test_axi_common.py`, `install.sh` and `SHARED_MODULE.md` do
  not exist yet. `README.md` has no link to a shared-module doc. This repo is
  flat — there is no documentation subdirectory, so that doc goes at the root.
- The extraction was prototyped end-to-end during planning against a scratch copy
  of the repo: all 25 tests stayed green, `./palette-axi --help` exited 0, and the
  tool worked through a symlink invoked from an unrelated working directory. The
  approach is known to work — the tasks are execution, not exploration.
- `shellcheck` is installed on this box; `ruff`, `black` and `prettier` are not,
  so the PostToolUse hook only actually gates `.sh` files here. CI runs Python
  3.9, 3.11 and 3.12, so avoid syntax newer than 3.9.

No iterations have run yet.
