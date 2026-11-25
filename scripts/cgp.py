#!/usr/bin/env python3
"""Concurrent Git Protocol helper: status, isolate, check-stale, integrate, cleanup."""

from __future__ import annotations

import argparse
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TRUNKS = ("main", "master", "develop")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass
class WorktreeInfo:
    path: Path
    head: str | None = None
    branch: str | None = None
    bare: bool = False
    detached: bool = False


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def git_ok(cwd: Path, *args: str) -> str:
    proc = git(cwd, *args)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {msg}")
    return proc.stdout.strip()


def parse_worktrees(repo: Path) -> list[WorktreeInfo]:
    out = git_ok(repo, "worktree", "list", "--porcelain")
    worktrees: list[WorktreeInfo] = []
    current: WorktreeInfo | None = None
    for line in out.splitlines():
        if not line.strip():
            if current is not None:
                worktrees.append(current)
                current = None
            continue
        if line.startswith("worktree "):
            if current is not None:
                worktrees.append(current)
            current = WorktreeInfo(path=Path(line[len("worktree ") :]).resolve())
        elif current is None:
            continue
        elif line.startswith("HEAD "):
            current.head = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            if ref.startswith("refs/heads/"):
                current.branch = ref[len("refs/heads/") :]
            else:
                current.branch = ref
        elif line == "bare":
            current.bare = True
        elif line == "detached":
            current.detached = True
    if current is not None:
        worktrees.append(current)
    if not worktrees:
        raise RuntimeError("No worktrees reported by git worktree list --porcelain.")
    return worktrees


def current_worktree_root(repo: Path) -> Path:
    return Path(git_ok(repo, "rev-parse", "--show-toplevel")).resolve()


def branch_exists(primary: Path, name: str) -> bool:
    return git(primary, "rev-parse", "--verify", f"refs/heads/{name}").returncode == 0


def detect_trunk(primary: WorktreeInfo, explicit_trunk: str | None) -> str:
    if explicit_trunk:
        if not branch_exists(primary.path, explicit_trunk):
            raise RuntimeError(f"Specified trunk branch '{explicit_trunk}' does not exist.")
        return explicit_trunk
    if primary.branch in DEFAULT_TRUNKS:
        return primary.branch
    for candidate in DEFAULT_TRUNKS:
        if branch_exists(primary.path, candidate):
            return candidate
    raise RuntimeError(
        "Could not detect trunk branch (checked main, master, develop). Pass --trunk <name>."
    )


def is_clean(wt: Path) -> bool:
    return git_ok(wt, "status", "--porcelain") == ""


def resolve_context(
    repo_arg: str, explicit_trunk: str | None
) -> tuple[WorktreeInfo, WorktreeInfo, str]:
    repo = Path(repo_arg).resolve()
    if not repo.exists():
        raise RuntimeError(f"Path does not exist: {repo}")
    top = current_worktree_root(repo)
    worktrees = parse_worktrees(top)
    primary = worktrees[0]
    current = next((w for w in worktrees if w.path == top), None)
    if current is None:
        raise RuntimeError(f"Current worktree {top} not found in git worktree list.")
    trunk = detect_trunk(primary, explicit_trunk)
    return primary, current, trunk


def cmd_status(args: argparse.Namespace) -> int:
    primary, current, trunk = resolve_context(args.repo, args.trunk)
    primary_clean = is_clean(primary.path)
    current_clean = is_clean(current.path)
    in_agent = current.path != primary.path and bool(
        current.branch and current.branch.startswith("agent/")
    )
    trunk_sha = git_ok(primary.path, "rev-parse", trunk)
    base_sha = git_ok(current.path, "merge-base", "HEAD", trunk)
    behind = git_ok(current.path, "rev-list", "--count", f"HEAD..{trunk}")

    print(f"primary={primary.path}")
    print(f"primary_branch={primary.branch or '(detached)'}")
    print(f"primary_clean={str(primary_clean).lower()}")
    print(f"trunk={trunk}")
    print(f"trunk_sha={trunk_sha}")
    print(f"current_worktree={current.path}")
    print(f"current_branch={current.branch or '(detached)'}")
    print(f"current_clean={str(current_clean).lower()}")
    print(f"in_agent_worktree={str(in_agent).lower()}")
    print(f"base_sha={base_sha}")
    print(f"behind_trunk={behind}")
    return 0


def cmd_isolate(args: argparse.Namespace) -> int:
    if not SLUG_RE.match(args.slug):
        print(
            f"ERROR: slug '{args.slug}' must be lowercase hyphenated words (a-z, 0-9, -).",
            file=sys.stderr,
        )
        return 2

    primary, current, trunk = resolve_context(args.repo, args.trunk)

    # Idempotent adoption if already inside an agent/* worktree
    if current.path != primary.path and current.branch and current.branch.startswith("agent/"):
        base_sha = git_ok(current.path, "merge-base", "HEAD", trunk)
        print("STATUS=ADOPTED_EXISTING")
        print(f"AGENT_WT={current.path}")
        print(f"BRANCH={current.branch}")
        print(f"TRUNK={trunk}")
        print(f"BASE_SHA={base_sha}")
        return 0

    if primary.branch != trunk:
        print(
            f"PRIMARY_WRONG_BRANCH: primary worktree ({primary.path}) is on "
            f"'{primary.branch or '(detached)'}', expected trunk '{trunk}'. "
            "Ask the user before touching anything.",
            file=sys.stderr,
        )
        return 3

    agent_id = args.agent_id
    if agent_id:
        if not AGENT_ID_RE.match(agent_id):
            print(f"ERROR: invalid agent-id '{agent_id}'.", file=sys.stderr)
            return 2
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        agent_id = f"a{ts}-{secrets.token_hex(2)}"

    branch = f"agent/{agent_id}/{args.slug}"
    agent_wt = primary.path.parent / f"{primary.path.name}-wt-{agent_id}"

    if agent_wt.exists():
        print(
            f"ERROR: target worktree path already exists: {agent_wt}",
            file=sys.stderr,
        )
        return 2
    if branch_exists(primary.path, branch):
        print(f"ERROR: branch already exists: {branch}", file=sys.stderr)
        return 2

    proc = git(primary.path, "worktree", "add", str(agent_wt), "-b", branch, trunk)
    if proc.returncode != 0:
        print((proc.stderr or proc.stdout).strip(), file=sys.stderr)
        return proc.returncode

    base_sha = git_ok(agent_wt, "merge-base", "HEAD", trunk)
    print("STATUS=CREATED")
    print(f"AGENT_WT={agent_wt}")
    print(f"BRANCH={branch}")
    print(f"TRUNK={trunk}")
    print(f"BASE_SHA={base_sha}")
    return 0


def cmd_check_stale(args: argparse.Namespace) -> int:
    primary, current, trunk = resolve_context(args.repo, args.trunk)

    if args.fetch:
        remotes = git_ok(current.path, "remote")
        if remotes:
            git(current.path, "fetch", "--all", "--quiet")
            remote_ref = f"origin/{trunk}"
            if git(current.path, "rev-parse", "--verify", remote_ref).returncode == 0:
                remote_ahead = int(
                    git_ok(current.path, "rev-list", "--count", f"{trunk}..{remote_ref}")
                )
                if remote_ahead > 0:
                    print(
                        f"REMOTE_AHEAD: '{remote_ref}' is {remote_ahead} commit(s) ahead of "
                        f"local '{trunk}' (local trunk was not modified)."
                    )

    base_sha = git_ok(current.path, "merge-base", "HEAD", trunk)
    trunk_sha = git_ok(current.path, "rev-parse", trunk)

    if base_sha == trunk_sha:
        print(f"UP_TO_DATE: trunk '{trunk}' ({trunk_sha[:8]}) has not advanced since base.")
        return 0

    count = git_ok(current.path, "rev-list", "--count", f"{base_sha}..{trunk}")
    print(f"STALE: trunk '{trunk}' is {count} commit(s) ahead of base {base_sha[:8]}.")
    log_out = git_ok(current.path, "log", "--oneline", trunk, "--not", base_sha)
    if log_out:
        print("\nCommits on trunk:")
        print(log_out)
    diff_stat = git_ok(current.path, "diff", "--stat", f"{base_sha}..{trunk}")
    if diff_stat:
        print("\nChanged files:")
        print(diff_stat)
    return 0


def cmd_integrate(args: argparse.Namespace) -> int:
    primary, current, trunk = resolve_context(args.repo, args.trunk)

    if current.path == primary.path or not (current.branch and current.branch.startswith("agent/")):
        print(
            f"ERROR: integrate must run from an agent/* worktree (current: {current.path} on '{current.branch}').",
            file=sys.stderr,
        )
        return 2

    if not is_clean(current.path):
        print(
            f"AGENT_DIRTY: commit or resolve changes in {current.path} before integrating.",
            file=sys.stderr,
        )
        return 2

    if primary.branch != trunk:
        print(
            f"PRIMARY_WRONG_BRANCH: primary worktree ({primary.path}) is on "
            f"'{primary.branch or '(detached)'}', expected trunk '{trunk}'. "
            "Stop and ask the user.",
            file=sys.stderr,
        )
        return 3

    if not is_clean(primary.path):
        print(
            f"PRIMARY_DIRTY: primary worktree ({primary.path}) has uncommitted changes. "
            "Stop and ask the user; never stash or discard user state.",
            file=sys.stderr,
        )
        return 4

    agent_branch = current.branch
    attempts = max(1, args.lock_retries)
    for attempt in range(1, attempts + 1):
        proc = git(primary.path, "merge", "--ff-only", agent_branch)
        if proc.returncode == 0:
            new_sha = git_ok(primary.path, "rev-parse", "--short", trunk)
            print(f"SUCCESS: fast-forwarded '{trunk}' to {new_sha} from '{agent_branch}'.")
            return 0

        combined = f"{proc.stdout}\n{proc.stderr}".strip()
        lower = combined.lower()
        if "index.lock" in lower or ("unable to create" in lower and ".lock" in lower):
            if attempt < attempts:
                time.sleep(args.lock_wait)
                continue
            print(
                f"LOCK_BUSY: primary git lock persisted after {attempts} attempts. "
                "Wait and retry; never delete *.lock files by hand.",
                file=sys.stderr,
            )
            return 11

        if "not possible to fast-forward" in lower or "cannot fast-forward" in lower:
            print(
                f"NEEDS_REBASE: '{trunk}' advanced before integration. "
                f"Run 'git rebase {trunk}', re-verify tests, then retry integrate.",
                file=sys.stderr,
            )
            return 10

        print(f"INTEGRATE_FAILED: {combined}", file=sys.stderr)
        return proc.returncode

    return 11


def cmd_cleanup(args: argparse.Namespace) -> int:
    primary, current, trunk = resolve_context(args.repo, args.trunk)

    if current.path == primary.path or not (current.branch and current.branch.startswith("agent/")):
        print(
            f"ERROR: cleanup must target an agent/* worktree (current: {current.path} on '{current.branch}').",
            file=sys.stderr,
        )
        return 2

    agent_wt = current.path
    agent_branch = current.branch

    # Verify commits actually landed on trunk (run via primary so cwd is irrelevant)
    anc = git(primary.path, "merge-base", "--is-ancestor", agent_branch, trunk)
    if anc.returncode != 0:
        unmerged = git(primary.path, "log", "--oneline", f"{trunk}..{agent_branch}").stdout.strip()
        print(
            f"NOT_MERGED: '{agent_branch}' is not fully merged into '{trunk}'. "
            "Refusing to clean up unmerged work.",
            file=sys.stderr,
        )
        if unmerged:
            print(unmerged, file=sys.stderr)
        return 5

    if not is_clean(primary.path):
        print(
            f"PRIMARY_DIRTY: primary worktree ({primary.path}) is not clean; refusing cleanup.",
            file=sys.stderr,
        )
        return 4

    # Remove worktree without --force via primary
    rm_proc = git(primary.path, "worktree", "remove", str(agent_wt))
    if rm_proc.returncode != 0:
        status_out = git(agent_wt, "status", "--short").stdout.strip()
        print(
            f"WORKTREE_NOT_EMPTY: git worktree remove refused for {agent_wt}. "
            "Inspect status below; never pass --force over unidentified content.",
            file=sys.stderr,
        )
        if status_out:
            print(status_out, file=sys.stderr)
        return 6

    # Delete branch with -d (never -D) via primary
    del_proc = git(primary.path, "branch", "-d", agent_branch)
    if del_proc.returncode != 0:
        print(
            f"ERROR: git branch -d '{agent_branch}' failed: {(del_proc.stderr or del_proc.stdout).strip()}",
            file=sys.stderr,
        )
        return del_proc.returncode

    git(primary.path, "worktree", "prune")
    trunk_head = git_ok(primary.path, "log", "--oneline", trunk, "-1")
    print(
        f"CLEANED: landed on '{trunk}' ({trunk_head}); "
        f"removed worktree '{agent_wt}' and branch '{agent_branch}'."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Helper for concurrent-git-protocol (isolate, check-stale, integrate, cleanup)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", default=".", help="Path inside current worktree (default: .).")
        p.add_argument("--trunk", default=None, help="Explicit trunk branch name.")

    p_status = sub.add_parser("status", help="Inspect primary worktree, trunk, and agent state.")
    add_common(p_status)
    p_status.set_defaults(func=cmd_status)

    p_iso = sub.add_parser("isolate", help="Create (or adopt) a private agent worktree and branch.")
    add_common(p_iso)
    p_iso.add_argument("--slug", required=True, help="Short lowercase hyphenated task slug.")
    p_iso.add_argument("--agent-id", default=None, help="Optional explicit unique agent ID.")
    p_iso.set_defaults(func=cmd_isolate)

    p_stale = sub.add_parser("check-stale", help="Check whether trunk advanced since base SHA.")
    add_common(p_stale)
    p_stale.add_argument("--fetch", action="store_true", help="Run git fetch --all --quiet first.")
    p_stale.set_defaults(func=cmd_check_stale)

    p_int = sub.add_parser("integrate", help="Fast-forward trunk in primary from clean agent branch.")
    add_common(p_int)
    p_int.add_argument("--lock-retries", type=int, default=5, help="Retries on transient index.lock.")
    p_int.add_argument("--lock-wait", type=float, default=1.5, help="Seconds between lock retries.")
    p_int.set_defaults(func=cmd_integrate)

    p_clean = sub.add_parser("cleanup", help="Verify merge, remove agent worktree, delete branch -d.")
    add_common(p_clean)
    p_clean.set_defaults(func=cmd_cleanup)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
