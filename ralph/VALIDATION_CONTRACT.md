# Validation contract — lm-91

What "done" means for this milestone, decided before the tasks were written.

A later pass checks the built system against this file as a **black box**: no git
history, no diff, just the repo and a shell. Every assertion below is checkable
from a clean checkout, run from the repo root in a POSIX shell with nothing but
Python 3 and `git` available, offline. Each command was run against a working
prototype of the change while this contract was written, so the syntax is known
to execute. One bullet, one command, one observable outcome.

## Assertions

- `./palette-axi --help` exits 0, `./palette-axi <verb> --help` exits 0 for every one of `projects clusters cluster profiles profile edgehosts events packs`, and `python3 -m unittest discover -s . -p 'test_*.py'` exits 0 reporting zero failures, zero errors and at least 25 tests run — the CLI still behaves exactly as it did before, and the suite did not shrink to hide a regression.
- `R="$PWD"; d=$(mktemp -d); (cd "$d" && PYTHONPATH="$R" python3 -c "from axi_common import toon, E_OK, E_ERR, E_USAGE, E_NOTFOUND, E_REFUSED; print(toon('clusters', ['name','uid'], [])); print(E_OK, E_ERR, E_USAGE, E_NOTFOUND, E_REFUSED)")` exits 0 and prints exactly two lines, `clusters[0]{name,uid}: (none)` then `0 1 2 3 4` — the AXI output format and its exit-code table are consumable by a second tool as a library, from a directory that is not the repo, without copying source and without the Palette CLI, a network, `op`, or any non-stdlib package.
- `python3 -c "import importlib.util; from importlib.machinery import SourceFileLoader as L; l = L('palette_axi', 'palette-axi'); s = importlib.util.spec_from_loader('palette_axi', l); m = importlib.util.module_from_spec(s); l.exec_module(m); import axi_common as c; assert all(getattr(m, n) is getattr(c, n) for n in '_tv toon emit nxt die size_hint trunc ts'.split()); assert (m.E_OK, m.E_ERR, m.E_USAGE, m.E_NOTFOUND, m.E_REFUSED) == (0, 1, 2, 3, 4); print('single-definition OK')"` exits 0 and prints `single-definition OK` — the renderer and exit codes the CLI uses are the *same objects* the shared contract publishes, so a re-pasted second copy fails instead of drifting silently.
- `d=$(mktemp -d); ln -s "$PWD/palette-axi" "$d/palette-axi"; (cd / && "$d/palette-axi" --help)` exits 0 and prints a line starting `usage: palette-axi` — the tool still works the way it is actually deployed, through a symlink in another directory invoked from an unrelated working directory, so the shared contract resolves off the real script rather than the symlink or the caller's `cwd`. (This also passes on the pre-milestone code: it is a non-regression check, not a new capability.)
- `./palette-axi --version` exits 0 and prints a single non-empty line naming the tool and a version — the tool can state which build it is, which no AXI Python tool can do today, and which is why a bare symlink into a working repo is unpinnable.
- `d=$(mktemp -d); bash install.sh --prefix "$d" && "$d/bin/palette-axi" --version && bash install.sh --prefix "$d" && "$d/bin/palette-axi" --version` exits 0, printing the same version string both times — there is a repeatable, offline install with an update path, re-running it over an existing prefix is safe, and nothing is written outside the prefix.
- `grep -q '^## Decision' SHARED_MODULE.md && grep -q 'opp-axi' SHARED_MODULE.md && grep -q 'axi-tools' SHARED_MODULE.md && grep -q 'SHARED_MODULE' README.md` exits 0 — the cross-tool decision is written down and findable from the README: it names both the other Python tool and the npm-managed tool set, and states with its reason whether that tool joins the npm install.

## Explicitly not asserted

Nothing above requires a live Palette tenant, a `PALETTE_API_KEY`, or 1Password.
The tool stays read-only; no assertion here causes a write against any API.

Nothing above asserts anything about the contents of the `opp-axi` repository.
That is a different checkout and is outside this milestone's reach; the only
obligation this milestone takes on for it is the written decision above.
