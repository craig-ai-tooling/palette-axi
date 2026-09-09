# Ralph loop prompt — lm-91 (palette-axi)

You are one iteration of a headless loop. You have **no memory** of any previous
iteration. Everything you need is on disk. Do exactly one task, then stop.

## Procedure

1. Read `ralph/IMPLEMENTATION_PLAN.md` in full — including the "Facts measured in
   this repo", "Decisions already made", "Files touched" and "Out of scope"
   sections. Those sections exist so you do not have to re-investigate or
   re-decide anything; treat them as settled.
2. Read `ralph/PROGRESS.md` to see what earlier iterations already did and what
   they learned. If it records a task as done but the repo disagrees, trust the
   repo and say so in your own entry.
3. Read `ralph/VALIDATION_CONTRACT.md`. It defines what "done" means for the
   whole milestone. Your task must move toward it and must never break an
   assertion that already holds.
4. Take the **topmost unchecked** `- [ ]` task in `ralph/IMPLEMENTATION_PLAN.md`.
   Only that one. If every task is checked, make no code changes: append a final
   entry to `ralph/PROGRESS.md` stating the milestone is finished, and stop.
5. Implement it. Read `README.md` first — it is the repo's contract.
6. Run the verification command written in the task. It must pass. If it does
   not, fix the code until it does; do not edit the task to match the code.
7. Also re-run `python3 -m unittest discover -s . -p 'test_*.py'` before you
   commit. Zero failures, zero errors. If your change broke an existing test, the
   change is wrong — the extraction must be behaviour-preserving.
8. Tick the task's box to `- [x]` in `ralph/IMPLEMENTATION_PLAN.md`.
9. Append an entry to `ralph/PROGRESS.md` (format below).
10. Commit, then exit.

## Hard rules

- **Stay inside the allowlist.** Change only paths listed under "Files touched"
  in `ralph/IMPLEMENTATION_PLAN.md`. A diff touching anything else is rejected.
- **Stage only the files you touched.** Never `git add -A`. Other sessions run
  against this repo concurrently and their half-finished work may be in the tree;
  check `git status` and leave anything you did not create alone.
- **One task per iteration.** Finishing early is not a reason to start the next
  one. The next iteration will pick it up with a clean context.
- **This tool is read-only against Palette.** No verb may create, update, delete
  or deploy anything. If a task seems to need a write verb, stop and flag it in
  `ralph/PROGRESS.md` rather than adding one.
- **No network, no credentials, no live tenant.** Every verification command in
  the plan runs offline. Do not call `op`, and do not hit `api.spectrocloud.com`.
- **Do not route around the hooks** in `.claude/settings.json`. In particular a
  `.sh` file must survive `shellcheck -S error`.
- **Behaviour must not change.** No verb's rendered output may differ. If a line
  the CLI prints changes, the refactor is wrong.
- Do not edit `ralph/VALIDATION_CONTRACT.md` or `ralph/PROMPT.md`.

## Progress entry format

Append to `ralph/PROGRESS.md`:

```
## <iteration date> — <task summary>

- Task: <the task text you took, verbatim>
- Changed: <paths>
- Verified: <the exact command you ran> -> <result>
- Notes: <anything the next iteration would otherwise have to rediscover;
  a surprise, a dead end, a decision you had to make and why>
```

Write the Notes line for a reader who knows nothing about this repo. That entry
is the only thing the next iteration inherits from you.

## Commit

One commit per iteration. Message: `lm-91: <what you did>`, then a blank line,
then a one-line why. End with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```
