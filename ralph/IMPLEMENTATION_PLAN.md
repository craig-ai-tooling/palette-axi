# lm-91 — Extract the shared AXI module

`palette-axi`'s `toon()` and `_tv()` are byte-identical to `opp-axi`'s and carry
a comment saying so: *"copied verbatim from opp-axi — both tools must agree."*
The `E_OK/E_ERR/E_USAGE/E_NOTFOUND/E_REFUSED` tuple is duplicated the same way,
and the two `nxt()` docstrings have already drifted apart. This milestone gives
the two Python AXI tools one definition of that contract, and gives the Python
tools the version pin and update path the npm-managed AXI tools already have.

Read `ralph/VALIDATION_CONTRACT.md` for what "done" means. Record every
iteration in `ralph/PROGRESS.md`.

## Facts measured in this repo (do not re-derive these)

- `palette-axi` is a single stdlib-only Python 3 executable, 622 lines. Lines
  **45–112** are exactly the duplicated block: the `E_*` comment and tuple, then
  `_tv`, `toon`, `emit`, `nxt`, `die`, `size_hint`, `trunc`, `ts`.
- `is_unhealthy` (line 114) and `dget` (line 121) sit in the same commented
  section but are **not** shared: both document Palette-API-specific findings
  (health casing across two Palette endpoints; `"status": null` on pack objects).
- `size_hint` is defined but never called in this repo — it is alive in `opp-axi`.
  It moves to the shared module rather than being deleted.
- `python3 -m unittest test_palette_axi` currently runs **25 tests, all green**.
- `test_palette_axi.py` loads the CLI via `SourceFileLoader` and monkeypatches
  `palette_axi.api` / `palette_axi.emit` / `palette_axi.get_api_key`. Using
  `from axi_common import ...` keeps those names in `palette_axi`'s namespace, so
  every existing stub keeps working. `import axi_common` + qualified calls would
  break them — do not do that.
- CPython sets `sys.path[0]` to the **realpath** of the main script, so a sibling
  `axi_common.py` imports fine through a `~/.local/bin` symlink. Verified on 3.12.
  The explicit `sys.path.insert(..., realpath(__file__))` bootstrap is still
  required, because `SourceFileLoader` in the test harness does not set it.
- `.github/workflows/ci.yml` runs on Python 3.9, 3.11 and 3.12: `py_compile`, the
  unit tests, then a `--help` smoke loop over all eight verbs.
- The `.claude` PostToolUse hook runs `shellcheck -S error` on every `.sh` file
  written. `shellcheck` is installed; `ruff`/`black`/`prettier` are not, so
  Python and Markdown are not auto-reformatted.
- The npm install is `~/.local/share/axi-tools/package.json`: `gh-axi`, `gws-axi`,
  `quota-axi`, `tasks-axi` at caret ranges, plus
  `overrides: {"@toon-format/toon": "^2.3.1"}` — the JS tools already share their
  TOON encoder as one centrally pinned dependency. `~/.local/bin/<tool>` symlinks
  into `node_modules/.bin/`. Neither `opp-axi` nor `palette-axi` is installed
  there at all.

## Decisions already made (do not re-open)

1. **Module name and shape**: one file, `axi_common.py`, beside the executable.
   Stdlib-only, no Palette references, no PyPI dependency — `toon-format` on PyPI
   is 0.1.0 and self-described as beta, and there is no `axi-sdk-py`.
2. **Module contents**: the five `E_*` codes and `_tv, toon, emit, nxt, die,
   size_hint, trunc, ts`. `is_unhealthy` and `dget` stay in `palette-axi`.
3. **Docstring drift**: `palette-axi`'s wording wins verbatim —
   `"""Contextual disclosure — what to run next, not a wall of help text."""`
   `opp-axi`'s differing `nxt()` docstring is superseded, noted in the doc.
4. **`opp-axi` does NOT join the npm `axi-tools` install.** npm would supply a
   version string but not the two things that actually matter here: a Python
   interpreter constraint, and shipping `axi_common.py` alongside the executable.
   Joining means publishing two Python tools to a registry whose value to the JS
   tools is dependency resolution these tools do not use — and lm-90 has already
   put new AXI work on Node/axi-sdk-js, so the Python pair is not the growth path.
   Instead both Python tools get the same `install.sh` shape: a versioned
   directory under `$PREFIX/share/` plus a `$PREFIX/bin/` symlink, mirroring how
   `axi-tools` lays out `~/.local/bin` → versioned store, minus the npm publish.
5. **Cross-repo work is out of scope.** The `opp-axi` repository is a different
   checkout and cannot be edited from here. This milestone's obligation to it is
   the written adoption path plus a module and a test file it can consume as-is.
6. **Install layout.** `install.sh --prefix P` (default `$HOME/.local`) copies
   `palette-axi`, `axi_common.py` and a `VERSION` file holding the output of
   `git describe --tags --always --dirty` into `P/share/palette-axi/<ver>/`, then
   points a `P/bin/palette-axi` symlink at the copied executable. Re-running over
   the same prefix must succeed and leave a working symlink. No network access,
   and no writes outside `P`.
7. **The shared-module doc lives at the repo root**, as `SHARED_MODULE.md`. This
   repo is flat — `README.md`, the executable and its tests all sit at the root
   and there is no documentation subdirectory — so a one-file design doc joins
   them rather than creating a directory to hold a single page.

## Files touched

Only these paths may change:

- `axi_common.py`
- `test_axi_common.py`
- `palette-axi`
- `test_palette_axi.py`
- `install.sh`
- `.github/workflows/ci.yml`
- `README.md`
- `SHARED_MODULE.md`
- `ralph/IMPLEMENTATION_PLAN.md`
- `ralph/PROGRESS.md`

## Tasks

- [ ] Add `axi_common.py`: copy the exit-code comment and tuple plus `_tv, toon, emit, nxt, die, size_hint, trunc, ts` verbatim out of `palette-axi` lines 45-112, docstrings kept, stdlib-only, no Palette references. Do not edit `palette-axi` yet. Verify: `python3 -c "import axi_common as a; assert a.toon('x',['a'],[])=='x[0]{a}: (none)'; assert (a.E_OK,a.E_REFUSED)==(0,4)"` exits 0.
- [ ] Add `test_axi_common.py` testing `axi_common` alone (no `palette-axi` import, so `opp-axi` can run it unchanged): None vs False vs empty are distinct cells; quoting of comma, double-quote and newline; the `(none)` empty block; `nxt`, `emit`, `trunc`, `ts`, `size_hint`; `die` exiting with each of the five codes. Verify: `python3 -m unittest test_axi_common -v` exits 0.
- [ ] Rewire `palette-axi`: replace lines 45-112 with `sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))` followed by a single `from axi_common import ...` naming all 13 shared names. Keep `is_unhealthy` and `dget` local. Verify: `python3 -m unittest test_palette_axi -v` reports 25 tests, 0 failures, and `./palette-axi --help` exits 0.
- [ ] Add a no-drift test to `test_palette_axi.py`: import `axi_common` and assert each of `_tv toon emit nxt die size_hint trunc ts` on the loaded `palette_axi` module `is` the same object as on `axi_common`, and that the five `E_*` values are `(0,1,2,3,4)`. A re-pasted local copy must fail it. Verify: `python3 -m unittest test_palette_axi -v` exits 0.
- [ ] Add a symlink-install test to `test_palette_axi.py`: make a temp dir, symlink `palette-axi` into it, run that symlink with `--help` via `subprocess` from a third `cwd`, assert exit 0 and `usage:` in stdout — proving `axi_common` resolves off the real script path. Clean the temp dir up. Verify: `python3 -m unittest test_palette_axi -v` exits 0.
- [ ] Add `__version__ = "0.2.0"` to `axi_common.py` and a `--version` flag to `palette-axi` that prints `palette-axi <ver>`, where `<ver>` is a `VERSION` file beside the realpath-resolved script if present, else `__version__`. Verify: `./palette-axi --version` exits 0 printing one non-empty line, and `python3 -m unittest test_palette_axi -v` exits 0.
- [ ] Add `install.sh` implementing the install layout in decision 6 above — re-runnable, no network, writes nothing outside the prefix. Verify: `shellcheck -S error install.sh` exits 0; `d=$(mktemp -d); bash install.sh --prefix "$d"; bash install.sh --prefix "$d"; "$d/bin/palette-axi" --version` exits 0.
- [ ] CI: in `.github/workflows/ci.yml` add `axi_common.py` to the `py_compile` step and add a `python3 -m unittest test_axi_common -v` step before the existing one. Verify: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` exits 0, and `python3 -m py_compile palette-axi axi_common.py` and `python3 -m unittest test_axi_common test_palette_axi -v` both exit 0.
- [ ] Add `SHARED_MODULE.md` at the root: what `axi_common.py` covers, how `opp-axi` adopts it plus `test_axi_common.py`, the superseded `nxt()` docstring, and a `## Decision` section carrying decision 4 and why. Link from `README.md`. Verify: `grep -q '^## Decision' SHARED_MODULE.md && grep -q opp-axi SHARED_MODULE.md && grep -q axi-tools SHARED_MODULE.md && grep -q SHARED_MODULE README.md` exits 0.

## Out of scope

- No write verbs. This tool is read-only against Palette and stays that way; the
  repo's own session-start contract says to stop and flag rather than add one.
- No changes to the `opp-axi` or `axi-tools` repositories, and no npm publish.
- No behaviour change to any verb's output. If a rendered line changes, the
  extraction was done wrong.
