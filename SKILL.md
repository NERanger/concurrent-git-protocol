---
name: concurrent-git-protocol
description: "Use when multiple agents may concurrently change the same git repository: isolating work with git worktree, continuing past unaware foreign commits, rebasing over unknown changes, resolving merge conflicts from unknown edits, merging an agent branch back to trunk, and cleaning up worktrees after a verified merge. Covers multi-agent git workflows, concurrent-edit safety, and self-merge integration. Do not use for ordinary single-writer git operations with no concurrent writers."
metadata:
  short-description: "Git protocol for concurrent agents: isolate, continue, merge, clean up"
---

# Concurrent git protocol

Other agents may commit to this repository while you work, without notifying you. This protocol keeps your edits isolated in a private worktree, integrates cleanly onto trunk through rebase plus fast-forward merge, and cleans up only after verified integration.

Skipping isolation turns concurrent edits into silent lost updates and damaged history.

## Model and vocabulary

| Term | Meaning |
| --- | --- |
| `trunk` | The single shared integration branch (`main`, `master`, or `develop`). |
| `primary worktree` | The worktree where `trunk` is checked out. Kept clean; used only for fast-forward integration. |
| `agent worktree` | Your private sibling worktree (`<repo>-wt-<agent-id>`). All reads and edits happen here. |
| `agent branch` | Your private branch `agent/<agent-id>/<slug>`, checked out nowhere else. |
| `base SHA` | `git merge-base HEAD <trunk>`, your divergence reference point. |

```text
primary worktree   -> trunk checked out, kept clean, integration only
agent worktree A   -> branch agent/<id-a>/<slug>   (agent A edits here)
agent worktree B   -> branch agent/<id-b>/<slug>   (agent B edits here)
          \  /
           \/
   rebase onto trunk, ff-only merge into trunk in primary, clean up
```

Use the bundled helper `<skill-dir>/scripts/cgp.py` for Phases 0, 1, 2, 4, and 5. It parses `git worktree list --porcelain` (safe with spaces), prevents same-second agent ID collisions, verifies the primary worktree is clean and on `trunk` before merging, and runs all cleanup commands via `git -C "$PRIMARY"` so deleting your current worktree never breaks subsequent commands.

## Phase 0: Detect your situation

Run from your starting directory:

```sh
python3 <skill-dir>/scripts/cgp.py status
```

Classify the result:

1. **Already in an `agent/*` worktree**: adopt it and skip Phase 1.
2. **In the primary worktree on `trunk`**: proceed to Phase 1. Never edit files in the primary worktree.
3. **In the primary worktree on another branch**: stop and ask the user before touching anything.

## Phase 1: Isolate in a private worktree

Create your sibling worktree and switch into it:

```sh
python3 <skill-dir>/scripts/cgp.py isolate --slug <two-to-six-hyphen-words> [--agent-id <id>]
cd "<printed-AGENT_WT>"
```

If `--agent-id` is omitted, `cgp.py` generates a collision-resistant ID (`a<timestamp>-<4hex>`). If you are already in an `agent/*` worktree, `isolate` is idempotent and returns it without creating a second worktree.

Rules for the rest of the task:

- Do all reads, edits, builds, and tests inside your agent worktree.
- Never run `git checkout <trunk>` inside your agent worktree.
- Never create a second worktree for yourself.

## Phase 2: Work through unaware changes

Foreign commits landing on `trunk` do not touch your worktree. Keep working without locks, polling, or waiting.

Commit discipline makes integration survivable:

- Commit early and often, with one logical change per commit.
- Write **intent** in commit messages so another agent resolving a conflict knows why the change exists (`Extract retry helper so upload callers share backoff policy`, not `update utils.py`).
- Add an identity trailer when multiple agents share the repo: `Agent-Id: <agent-id>`.

At phase boundaries (before large edits or before Phase 3), run a cheap staleness check:

```sh
python3 <skill-dir>/scripts/cgp.py check-stale
```

If `trunk` advanced and touched files or APIs your remaining work depends on, inspect only those current `trunk` files, adapt your plan, and continue.

## Phase 3: Sync onto current trunk

Run inside your agent worktree:

1. Commit all work so `git status --porcelain` is empty.
2. Record your pre-rebase base (`BASE=$(git merge-base HEAD <trunk>)`) and rebase onto local `<trunk>`:

   ```sh
   git rebase <trunk>
   ```

   If this repository coordinates through a shared remote rather than local-only agents, pass `--fetch` to `check-stale` first; `git fetch origin` updates `origin/<trunk>`, not local `<trunk>`, so confirm with the user before fast-forwarding local `<trunk>` to `origin/<trunk>`.
3. If conflicts occur, read [references/conflict-and-recovery.md](references/conflict-and-recovery.md) and resolve by **intent**, remembering that under rebase `ours` is `trunk` and `theirs` is your commit.
4. Check for file overlap and semantic conflicts when `trunk` moved (`BASE != <trunk>`):
   - Compare `git diff --name-only "$BASE"..<trunk>` against files changed on your branch and any adjacent shared middleware, store/projection, or contract boundaries.
   - If `trunk` touched any overlapping file or adjacent boundary, inspect those foreign commits (`git log -p "$BASE"..<trunk> -- <files>`) for semantic conflicts (duplicate imports, dropped fields, changed contracts or assumptions) even when the text rebase succeeded without conflict markers.
5. Run the repository's build, tests, and linters after rebase. A clean text merge with failing tests is a semantic conflict; fix it on your branch before Phase 4. When step 4 found overlap on production code, do not rely on pre-rebase unit test results: re-run full build, typecheck, and the matching integration or browser suite on the rebased tree before Phase 4.
6. Re-verify assumptions (functions, paths, contracts) your change depends on still exist in the rebased tree.

## Phase 4: Integrate onto trunk

From your agent worktree, run:

```sh
python3 <skill-dir>/scripts/cgp.py integrate
```

`integrate` checks that your agent worktree is clean, verifies the primary worktree is clean and actually checked out on `<trunk>`, retries briefly on transient `index.lock` contention (never deleting lockfiles), and executes `git -C "$PRIMARY" merge --ff-only <agent-branch>`.

Handle the reported status:

- `SUCCESS`: your commits landed on `trunk`. Go to Phase 5.
- `NEEDS_REBASE`: another agent integrated first. Return to Phase 3 (rebase and re-verify), then retry. If this repeats 3+ times, report contention to the user and pause.
- `PRIMARY_DIRTY` or `PRIMARY_WRONG_BRANCH`: stop and ask the user. Never stash, switch, or discard user state in the primary worktree.
- `LOCK_BUSY`: wait a few seconds and retry `integrate` without touching `*.lock`.

## Phase 5: Clean up after verified integration

Cleanup is mandatory after `SUCCESS` and forbidden after any failure. Run:

```sh
python3 <skill-dir>/scripts/cgp.py cleanup
```

`cleanup` verifies `merge-base --is-ancestor <agent-branch> <trunk>` and a clean primary, then runs `worktree remove` (never `--force`), `branch -d` (never `-D`), and `worktree prune` via `git -C "$PRIMARY"`.

If cleanup stops with `WORKTREE_NOT_EMPTY`, read [references/conflict-and-recovery.md](references/conflict-and-recovery.md). Report one final line: what landed, and which worktree and branch were removed.

**Never clean up on failure.** Leave the worktree and branch intact as recovery points and report the exact state to the user.

## Safety rules

- Never force-push `trunk` or a branch you did not create.
- Never rewrite commits you do not own; rebase only your own `agent/*` branch.
- Never commit directly on `trunk` or check out `trunk` inside an agent worktree.
- Never remove another agent's worktree or branch.
- Never run `git clean -fd`, `git reset --hard`, or `git stash` in the primary worktree.
- Never delete `*.lock` files by hand.
- Never run `git branch -D` or `git worktree remove --force` over unidentified content.
