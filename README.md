# concurrent-git-protocol

An Agent Skill and zero-dependency helper script for multiple agents concurrently editing the same Git repository without locks, lost updates, or damaged trunk history.

## How it works

1. **Isolate**: each agent edits in its own sibling `git worktree` on a private `agent/<agent-id>/<slug>` branch, leaving the primary worktree clean on `trunk`.
2. **Continue**: foreign commits landing on `trunk` never disturb an agent's worktree; lightweight staleness checks surface relevant upstream changes at phase boundaries.
3. **Sync and integrate**: agents rebase onto current `trunk`, re-run verification checks, and fast-forward `trunk` atomically inside the primary worktree (`merge --ff-only`). Lost races simply rebase and retry.
4. **Verify and clean up**: worktree removal (`worktree remove`, never `--force`) and branch deletion (`branch -d`, never `-D`) run via `git -C "$PRIMARY"` only after verifying `merge-base --is-ancestor`.

## Repository layout

| Path | Role |
| --- | --- |
| [SKILL.md](SKILL.md) | Main skill entrypoint, 5-phase protocol, and safety rules |
| [references/conflict-and-recovery.md](references/conflict-and-recovery.md) | On-demand conflict resolution policy and failure recovery guide |
| [scripts/cgp.py](scripts/cgp.py) | Zero-dependency Python 3 helper (`status`, `isolate`, `check-stale`, `integrate`, `cleanup`) |

## Helper CLI (`scripts/cgp.py`)

```sh
python3 scripts/cgp.py status
python3 scripts/cgp.py isolate --slug fix-upload-retry [--agent-id a42]
python3 scripts/cgp.py check-stale [--fetch]
python3 scripts/cgp.py integrate
python3 scripts/cgp.py cleanup
```

Built-in safety invariants:

- Parses `git worktree list --porcelain` (safe with spaces in repository paths).
- Generates collision-resistant agent IDs (`a<timestamp>-<4hex>`) when `--agent-id` is omitted, and adopts existing `agent/*` worktrees idempotently.
- Refuses to fast-forward if the primary worktree is dirty (`PRIMARY_DIRTY`) or checked out on a non-trunk branch (`PRIMARY_WRONG_BRANCH`).
- Retries briefly on transient `index.lock` contention without ever deleting lockfiles.
- Refuses cleanup if the agent branch is not an ancestor of `trunk` (`NOT_MERGED`), and executes all cleanup commands via `git -C "$PRIMARY"` so running from inside the deleted agent worktree works reliably.

## Installation

Clone this repository and link (or copy) it into your agent's skills directory:

```bash
git clone git@github.com:NERanger/concurrent-git-protocol.git ~/repo/concurrent-git-protocol
```

Example target directories:

```bash
# Codex (shared agents skills directory)
ln -s ~/repo/concurrent-git-protocol ~/.agents/skills/concurrent-git-protocol

# OpenCode
ln -s ~/repo/concurrent-git-protocol ~/.config/opencode/skills/concurrent-git-protocol

# Antigravity / Gemini
ln -s ~/repo/concurrent-git-protocol ~/.gemini/config/skills/concurrent-git-protocol

# Devin
ln -s ~/repo/concurrent-git-protocol ~/.config/devin/skills/concurrent-git-protocol
```

Replace `ln -s` with `cp -a` if your environment requires standalone copies instead of symlinks.
