#!/usr/bin/env python3
"""Push the main2main patch as a new branch and open a GitHub pull request.

In CI mode (default when PUSH_TO_GITHUB=true):
  1. Ensure gh CLI is authenticated (use GH_TOKEN in CI, or existing gh auth).
  2. Configure git credential helper so git push uses the same token.
  3. If changes are already on a working branch (no --patch-path), use it directly;
     otherwise create a branch from the current commit and apply the final patch.
  4. Push the branch to the fork repo.
  5. Open a draft PR via ``gh pr create`` with proper commit-range title.
  6. Add labels to the PR.
  7. Write the PR URL to a file for downstream workflow steps.

In local mode (PUSH_TO_GITHUB not set):
  1-4: same as above.
  5. Open a regular (non-draft) PR.

Environment variables:
  PUSH_TO_GITHUB  — must be "true" to do anything
  GITHUB_REPO     — target repo "owner/name" (required, e.g. vllm-project/vllm-ascend)
  HEAD_FORK       — fork to push to (optional, e.g. vllm-ascend-ci/vllm-ascend)
  GH_TOKEN        — GitHub Personal Access Token (required in CI;
                    also used by git push via credential helper)
  PR_LABELS       — comma-separated labels to add (default: "ready,ready-for-test")
  PR_DRAFT        — "true" (default) or "false"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from main2main_flow.utils import run_git, ts_print

DEFAULT_WORKSPACE_DIR = Path(__file__).parent.parent.parent / "workspace"
_PR_URL_FILE = "/tmp/main2main/pr_url.txt"


def _run_format(repo: Path) -> None:
    """Run format.sh if available to fix lint issues before commit."""
    fmt_script = repo / "format.sh"
    if fmt_script.exists():
        subprocess.run(["bash", str(fmt_script)], cwd=str(repo), capture_output=True)


def _wait_for_fork_ref(head_fork: str, branch: str, expected_head: str,
                        timeout: int = 30) -> None:
    """Wait for the pushed branch to be visible on GitHub.

    After ``git push``, GitHub may take a moment to reflect the new ref.
    This polls ``git ls-remote`` until the fork branch matches the expected HEAD.
    """
    fork_url = f"https://github.com/{head_fork}.git"
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            ["git", "ls-remote", fork_url, f"refs/heads/{branch}"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            remote_sha = r.stdout.strip().split()[0]
            if remote_sha == expected_head:
                ts_print(f"[push] Fork ref confirmed: {remote_sha[:8]}")
                return
        time.sleep(2)
    ts_print(f"[push] Warning: fork ref not confirmed within {timeout}s, proceeding anyway")


def _print_diff_diagnostics(ascend_path: Path, branch: str) -> None:
    """Print git diff and log for pre-push diagnostics."""
    r = subprocess.run(
        ["git", "diff", "--stat", "HEAD"],
        cwd=str(ascend_path), capture_output=True, text=True,
    )
    ts_print(f"[push] git diff --stat HEAD:\n{r.stdout.strip() or '(empty)'}")
    r = subprocess.run(
        ["git", "log", "--oneline", "-10"],
        cwd=str(ascend_path), capture_output=True, text=True,
    )
    ts_print(f"[push] git log --oneline -10:\n{r.stdout.strip()}")
    # Compare against upstream/main (vllm-project/vllm-ascend), which is the real base
    base_ref = _resolve_upstream_base(ascend_path, "main")
    if base_ref:
        r = subprocess.run(
            ["git", "rev-list", "--count", f"{base_ref}..{branch}"],
            cwd=str(ascend_path), capture_output=True, text=True,
        )
        count = r.stdout.strip()
        r2 = subprocess.run(
            ["git", "log", "--oneline", f"{base_ref}..{branch}"],
            cwd=str(ascend_path), capture_output=True, text=True,
        )
        ts_print(f"[push] Commits on {branch} not on upstream/main ({base_ref[:8]}): {count} commit(s)\n{r2.stdout.strip() or '(none)'}")
    else:
        ts_print("[push] Could not resolve upstream/main for comparison")


def _resolve_upstream_base(ascend_path: Path, base_branch: str) -> str:
    """Find the upstream base commit for comparison.

    Tries upstream/main, then origin/main, returns empty on failure.
    """
    for ref in (f"upstream/{base_branch}", f"origin/{base_branch}"):
        r = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            cwd=str(ascend_path), capture_output=True, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    # Fallback: try ls-remote against the known upstream repo
    r = subprocess.run(
        ["git", "ls-remote", "https://github.com/vllm-project/vllm-ascend.git",
         f"refs/heads/{base_branch}"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split()[0]
    return ""


def _has_divergent_commits(ascend_path: Path, branch: str, base_sha: str) -> bool:
    """Check whether *branch* has commits that are not in *base_sha*."""
    r = subprocess.run(
        ["git", "rev-list", "--count", f"{base_sha}..{branch}"],
        cwd=str(ascend_path), capture_output=True, text=True,
    )
    count = int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
    return count > 0


def _detect_default_branch(repo: Path | str, remote: str = "origin") -> str:
    try:
        r = subprocess.run(
            ["git", "symbolic-ref", f"refs/remotes/{remote}/HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        return r.stdout.strip().rsplit("/", 1)[-1]
    except subprocess.CalledProcessError:
        return "main"


def _git_push(ascend_path: Path, branch: str) -> None:
    """Push branch to origin with token-based auth.

    Uses GH_TOKEN / GITHUB_TOKEN via GIT_ASKPASS when available (bypasses
    ``gh auth git-credential`` which can fail when git URL rewrites are active).
    Otherwise falls back to ``gh auth git-credential`` for local / logged-in use.
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if token:
        askpass = ascend_path / ".git" / "push-askpass.sh"
        try:
            askpass.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "    *Username*) echo \"x-access-token\" ;;\n"
                "    *Password*) echo \"$GIT_PUSH_TOKEN\" ;;\n"
                "esac\n"
            )
            askpass.chmod(0o700)
            env = os.environ.copy()
            env["GIT_PUSH_TOKEN"] = token
            env["GIT_ASKPASS"] = str(askpass)
            subprocess.run(
                ["git", "push", "--force-with-lease", "origin", branch],
                cwd=str(ascend_path), capture_output=True, text=True, check=True,
                env=env,
            )
        finally:
            askpass.unlink(missing_ok=True)
    else:
        run_git(ascend_path, "push", "--force-with-lease", "origin", branch)


def _add_labels(github_repo: str, pr_number: str, labels: list[str]) -> None:
    if not labels:
        return
    result = subprocess.run(
        ["gh", "api", "--method", "POST",
         "-H", "Accept: application/vnd.github+json",
         f"/repos/{github_repo}/issues/{pr_number}/labels"],
        input=json.dumps({"labels": labels}),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        ts_print(f"[push] Warning: Failed to add labels {labels}: {result.stderr.strip()}")
    else:
        ts_print(f"[push] Labels added: {labels}")


def push_and_create_pr(
    ascend_path: Path,
    github_repo: str,
    patch_path: Path | None = None,
    summary_path: Path | None = None,
    workspace_dir: Path = DEFAULT_WORKSPACE_DIR,
    old_commit: str = "",
    new_commit: str = "",
    head_fork: str = "",
    draft: bool = True,
    labels: list[str] | None = None,
    branch_name: str = "",
) -> str:
    """Create a branch (or reuse current), push to fork, and open a GitHub PR.

    Returns the PR URL, or "" when preconditions are not met.
    Raises subprocess.CalledProcessError on git/gh failure.
    """
    if not github_repo:
        ts_print("[push] GITHUB_REPO is empty, cannot create PR.", file=sys.stderr)
        return ""

    summary_file = summary_path or workspace_dir / "final_summary.md"
    if not summary_file.exists():
        ts_print(f"[push] Summary file not found: {summary_file}, using empty description.", file=sys.stderr)
        pr_description = ""
    else:
        pr_description = summary_file.read_text(encoding="utf-8")

    # ---- branch ----
    current_branch = run_git(ascend_path, "branch", "--show-current").strip()
    is_detached = not current_branch

    patch_file = patch_path.resolve() if patch_path else None
    has_patch = patch_file and patch_file.exists()

    if is_detached and not has_patch:
        ts_print("[push] Detached HEAD and no patch to apply, cannot push.", file=sys.stderr)
        return ""

    # Save current origin URL so we can restore it after push
    _saved_origin_url = run_git(ascend_path, "remote", "get-url", "origin").strip()

    try:
        # Decide branch and apply patch
        keep_branch = os.getenv("MAIN2MAIN_KEEP_BRANCH", "false").lower() == "true"
        if has_patch:
            if keep_branch and not is_detached:
                # Reuse existing branch, but still apply the cumulative patch
                # and commit — otherwise the push would send an empty branch.
                branch = current_branch
                ts_print(f"[push] Reusing branch '{branch}', committing working tree changes")
                _run_format(ascend_path)
                run_git(ascend_path, "add", "-A")
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                commit_msg = _build_commit_msg(old_commit, new_commit, ts)
                run_git(ascend_path, "commit", "-s", "-m", commit_msg)
                ts_print(f"[push] Committed as '{commit_msg}'.")
            else:
                # Create fresh branch and apply patch
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                branch = branch_name or f"update/main2main-{ts}"
                run_git(ascend_path, "checkout", "-b", branch)
                ts_print(f"[push] Created branch '{branch}', applying patch: {patch_file}")
                run_git(ascend_path, "apply", str(patch_file))
                _run_format(ascend_path)
                run_git(ascend_path, "add", "-A")
                commit_msg = _build_commit_msg(old_commit, new_commit, ts)
                run_git(ascend_path, "commit", "-s", "-m", commit_msg)
                ts_print(f"[push] Committed as '{commit_msg}'.")
        elif is_detached:
            branch = branch_name or f"update/main2main-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            run_git(ascend_path, "checkout", "-b", branch)
            ts_print(f"[push] Created branch '{branch}' from detached HEAD.")
        else:
            # Reuse current branch, no patch to apply
            branch = current_branch
            ts_print(f"[push] Reusing current branch '{branch}' (already has step commits).")

            # ---- diagnostics before push ----
        _print_diff_diagnostics(ascend_path, branch)
        if patch_file and patch_file.exists():
            content = patch_file.read_text(encoding="utf-8")
            ts_print(f"[push] final_target.patch ({len(content)} bytes):\n{content[:3000]}")
        else:
            ts_print("[push] No final_target.patch found, using branch commits directly.")

        # ---- push ----
        if head_fork:
            # Switch origin to the target fork repo (bypass mirror proxy for push),
            # push, then restore the original origin URL.
            fork_url = f"https://github.com/{head_fork}.git"
            run_git(ascend_path, "remote", "set-url", "origin", fork_url)
            ts_print(f"[push] Set origin to {fork_url}")
            _git_push(ascend_path, branch)
            head_ref = f"{head_fork.split('/')[0]}:{branch}"
            ts_print(f"[push] Pushed to {fork_url}")
            run_git(ascend_path, "remote", "set-url", "origin", _saved_origin_url)
        else:
            run_git(ascend_path, "push", "origin", branch)
            head_ref = branch
            ts_print(f"[push] Pushed branch '{branch}'.")

        # ---- PR ----
        base_branch = _detect_default_branch(ascend_path, remote="origin")
        local_head = run_git(ascend_path, "rev-parse", "HEAD").strip()
        ts_print(f"[push] Creating PR: head={head_ref} base={base_branch} repo={github_repo} local_head={local_head[:8]}")

        # Check if branch has commits that differ from the upstream base
        upstream_base = _resolve_upstream_base(ascend_path, base_branch)
        if upstream_base and not _has_divergent_commits(ascend_path, branch, upstream_base):
            ts_print(f"[push] Branch {branch} is at same commit as {base_branch} ({upstream_base[:8]}), skipping PR.")
            return ""

        # Verify the fork branch is visible on GitHub before PR creation
        if head_fork:
            _wait_for_fork_ref(head_fork, branch, local_head)

        pr_title = _build_pr_title(old_commit, new_commit)
        gh_cmd = [
            "gh", "pr", "create",
            "--title", pr_title,
            "--body", pr_description,
            "--head", head_ref,
            "--base", base_branch,
            "--repo", github_repo,
        ]
        if draft:
            gh_cmd.append("--draft")

        result = subprocess.run(
            gh_cmd, capture_output=True, text=True, cwd=str(ascend_path),
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            ts_print(f"[push] PR create FAILED: {err}", flush=True)
            ts_print(f"[push] gh stdout: {result.stdout.strip()}", flush=True)
            if "No commits between" in err:
                ts_print("[push] No new commits to create PR for, skipping.")
                return ""
            result.check_returncode()
        pr_url = result.stdout.strip()
        ts_print(f"[push] PR created: {pr_url}")

        # ---- labels ----
        pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
        if pr_number.isdigit():
            if labels is None:
                labels = ["ready", "ready-for-test"]
            _add_labels(github_repo, pr_number, labels)

        # ---- persist PR URL ----
        Path("/tmp/main2main").mkdir(parents=True, exist_ok=True)
        Path(_PR_URL_FILE).write_text(pr_url + "\n")
        ts_print(f"[push] PR URL written to {_PR_URL_FILE}")

    finally:
        # Only restore if we created a new branch from a different starting point
        if has_patch:
            run_git(ascend_path, "checkout", current_branch if not is_detached else "HEAD")
            ts_print(f"[push] Restored original ref.")

    return pr_url


def _build_commit_msg(old_commit: str, new_commit: str, ts: str) -> str:
    if old_commit and new_commit:
        short_old = old_commit[:8]
        short_new = new_commit[:8]
        return f"main2main: sync vllm upstream ({short_old}...{short_new}) [{ts}]"
    return f"main2main: sync vllm upstream ({ts})"


def _build_pr_title(old_commit: str, new_commit: str) -> str:
    if new_commit:
        return f"[Misc]feat: adapt to vLLM main ({new_commit[:8]})"
    return "main2main: sync vllm upstream"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the main2main final patch to a new branch and open a GitHub PR."
    )
    parser.add_argument("--ascend-path", type=Path, required=True,
                        help="Local vllm-ascend repository path.")
    parser.add_argument("--patch-path", type=Path, default=None,
                        help="Path to final_target.patch (default: workspace/final_target.patch).")
    parser.add_argument("--summary-path", type=Path, default=None,
                        help="Markdown file used as PR description (default: workspace/final_summary.md).")
    parser.add_argument("--workspace-dir", type=Path, default=DEFAULT_WORKSPACE_DIR,
                        help="Workspace directory containing final_target.patch and final_summary.md.")
    parser.add_argument("--github-repo", default=os.getenv("GITHUB_REPO"),
                        required=not os.getenv("GITHUB_REPO"),
                        help="Target repo in owner/name form, e.g. vllm-project/vllm-ascend (or set $GITHUB_REPO).")
    parser.add_argument("--old-commit", default="",
                        help="Old vLLM commit for PR title (first 8 chars used).")
    parser.add_argument("--new-commit", default="",
                        help="New vLLM commit for PR title (first 8 chars used).")
    parser.add_argument("--head-fork", default=os.getenv("HEAD_FORK", ""),
                        help="Fork repo to push to, e.g. vllm-ascend-ci/vllm-ascend.")
    parser.add_argument("--draft", action="store_true",
                        default=os.getenv("PR_DRAFT", "true").lower() == "true",
                        help="Create as draft PR (default: true).")
    parser.add_argument("--labels", default=os.getenv("PR_LABELS", "ready,ready-for-test"),
                        help="Comma-separated labels to add to the PR.")
    parser.add_argument("--branch-name", default="",
                        help="Branch name (auto-generated if empty).")
    parser.add_argument("--push", action="store_true",
                        default=os.getenv("PUSH_TO_GITHUB", "false").lower() == "true",
                        help="Actually push and create PR (default: $PUSH_TO_GITHUB).")
    args = parser.parse_args()

    if not args.push:
        ts_print("[push] PUSH_TO_GITHUB is not true, skipping.", file=sys.stderr)
        sys.exit(0)

    label_list = [lbl.strip() for lbl in args.labels.split(",") if lbl.strip()] if args.labels else []

    push_and_create_pr(
        ascend_path=args.ascend_path,
        patch_path=args.patch_path,
        summary_path=args.summary_path,
        workspace_dir=args.workspace_dir,
        github_repo=args.github_repo,
        old_commit=args.old_commit,
        new_commit=args.new_commit,
        head_fork=args.head_fork,
        draft=args.draft,
        labels=label_list,
        branch_name=args.branch_name,
    )


if __name__ == "__main__":
    main()
