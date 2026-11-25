# Conflict resolution and failure recovery

Read this reference when a rebase hits conflicts, tests fail after a clean rebase, cleanup refuses, or git state looks wrong.

## Conflict resolution policy

Resolve concurrent changes by **intent**, never mechanically.

1. **Understand the other side first.** Before editing conflict markers, inspect the foreign commits:

   ```sh
   git log --oneline <trunk> --not <BASE>
   git show <commit>
   ```

2. **Preserve both intents on textual conflicts.** Combine when both behaviors belong. When both commits fix the same defect, keep the stronger fix and note why in the commit message. Never drop the other side silently.
3. **Mind the rebase label inversion.** During `git rebase`, `ours` is `trunk` (upstream) and `theirs` is your commit being replayed. Never run `git checkout --ours` or `--theirs` without inspecting the hunks.
4. **Investigate delete/rename conflicts.** If `trunk` deleted or renamed a file you edited, inspect its history before choosing:

   ```sh
   git log --follow --oneline <trunk> -- <path>
   ```

   Re-apply your edit at the new path if moved; port or drop deliberately (and say which) if removed.
5. **Fix semantic conflicts on your branch.** If text merged cleanly but build, types, or tests fail, reconcile the behaviors on your `agent/*` branch before Phase 4.
6. **Stop when requirements are ambiguous.** If reconciling intents requires guessing unstated requirements, leave the rebase in progress and ask the user. Do not run `git rebase --abort` and discard conflict analysis.

## Failure recovery

### Rebase stopped mid-conflict and you lost track

Run `git status` to see remaining paths and `git diff` to see hunks. Resolve per policy, stage the resolved files, and run `git rebase --continue`. Your pre-rebase commits stay safe in `ORIG_HEAD` until you abort.

### Rebase was already aborted and branch tip looks wrong

Inspect `git reflog` for the pre-rebase commit SHA. Recover with `git reset --hard <sha>` **on your own `agent/*` branch only**, never on `trunk`.

### Repeated fast-forward race losses

Rebasing and retrying is normal. If `integrate` returns `NEEDS_REBASE` three or more times in a row, stop spinning and tell the user that `trunk` integration is heavily contended.

### Committed on the wrong branch

If commits landed on the wrong `agent/*` branch, note their SHAs, `git cherry-pick` them onto the right branch, then use `git rebase --onto` to drop them from the wrong branch. If commits landed on `trunk` by mistake, stop and ask the user before rewriting anything.

### `cleanup` refused with `WORKTREE_NOT_EMPTY`

Inspect `git -C "$AGENT_WT" status --short`:

- Real uncommitted work: commit it on your branch (or report it to the user) before removing.
- Confirmed disposable build artifacts (`node_modules/`, `target/`, `dist/`, `__pycache__/`): delete only those identified artifacts, then re-run `scripts/cgp.py cleanup`.
- Never pass `--force` over unidentified files.

### Primary worktree is dirty or on a non-trunk branch

Stop and ask the user to clean, commit, stash, or switch the primary worktree back to `<trunk>`. Never modify the primary worktree state yourself.
