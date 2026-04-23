#!/usr/bin/env python3
"""
Replay commits from one Git repository into another while rewriting identities.

If Repo B is empty, the script replays the full reachable non-merge history from
Repo A. If Repo B already shares history with Repo A, the script replays only
the source-only non-merge commits reachable from an inclusive source stop point.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

DEFAULT_AUTHOR_NAME = os.getenv("GIT_AUTHOR_NAME", "New Author")
DEFAULT_AUTHOR_EMAIL = os.getenv("GIT_AUTHOR_EMAIL", "author@example.com")
EXAMPLES = """Examples:
  Full migration from Repo A into a fresh empty Repo B
    ./migrate_commits.py /path/to/repo-a /path/to/repo-b \\
      --author-name "Open Source Maintainer" \\
      --author-email "oss@example.com"

  Stop at a specific source commit, inclusive
    ./migrate_commits.py /path/to/repo-a /path/to/repo-b \\
      --until 4f3c2b1 \\
      --author-name "Open Source Maintainer" \\
      --author-email "oss@example.com"

  Preview every step without changing Repo B
    ./migrate_commits.py /path/to/repo-a /path/to/repo-b \\
      --dry-run \\
      --author-name "Open Source Maintainer" \\
      --author-email "oss@example.com"

  Print help
    ./migrate_commits.py --help
"""


class GitCommandError(RuntimeError):
    """Raised when a Git subprocess fails."""

    def __init__(self, args: Sequence[str], cwd: Path, output: str) -> None:
        command = "git " + " ".join(args)
        super().__init__(f"{command} failed in {cwd}:\n{output.rstrip()}")
        self.command = command
        self.cwd = cwd
        self.output = output


@dataclass(frozen=True)
class CommitMetadata:
    commit: str
    author_date: str
    committer_date: str
    message: bytes


def print_step(message: str, dry_run: bool = False) -> None:
    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}{message}")


def run_git(
    args: Sequence[str],
    cwd: Path,
    env: Optional[dict[str, str]] = None,
    input_text: Optional[str] = None,
) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise GitCommandError(args, cwd, completed.stdout + completed.stderr)
    return completed.stdout


def run_git_bytes(
    args: Sequence[str],
    cwd: Path,
    env: Optional[dict[str, str]] = None,
    input_bytes: Optional[bytes] = None,
) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        input=input_bytes,
        capture_output=True,
    )
    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr).decode("utf-8", "replace")
        raise GitCommandError(args, cwd, output)
    return completed.stdout


def git_config_matches(path: Path, pattern: str) -> list[tuple[str, str]]:
    completed = subprocess.run(
        ["git", "config", "--get-regexp", pattern],
        cwd=path,
        text=True,
        capture_output=True,
    )
    if completed.returncode == 1:
        return []
    if completed.returncode != 0:
        raise GitCommandError(
            ["config", "--get-regexp", pattern],
            path,
            completed.stdout + completed.stderr,
        )

    entries: list[tuple[str, str]] = []
    for line in completed.stdout.splitlines():
        key, value = line.split(None, 1)
        entries.append((key, value))
    return entries


def require_repository(path: Path, label: str) -> None:
    if not path.exists():
        raise ValueError(f"{label} '{path}' does not exist.")
    if not path.is_dir():
        raise ValueError(f"{label} '{path}' is not a directory.")
    try:
        run_git(["rev-parse", "--git-dir"], path)
    except GitCommandError as exc:
        raise ValueError(f"{label} '{path}' is not a Git repository.") from exc


def require_head(path: Path, label: str) -> None:
    try:
        run_git(["rev-parse", "--verify", "HEAD^{commit}"], path)
    except GitCommandError as exc:
        raise ValueError(f"{label} '{path}' does not have any commits.") from exc


def repository_has_head(path: Path) -> bool:
    try:
        run_git(["rev-parse", "--verify", "HEAD^{commit}"], path)
        return True
    except GitCommandError:
        return False


def require_clean_worktree(path: Path, label: str) -> None:
    status = run_git(["status", "--porcelain"], path).strip()
    if status:
        raise ValueError(f"{label} '{path}' has uncommitted changes.")


def resolve_commit(path: Path, revision: str) -> str:
    return run_git(["rev-parse", "--verify", f"{revision}^{{commit}}"], path).strip()


def current_branch(path: Path, label: str) -> str:
    try:
        return run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], path).strip()
    except GitCommandError as exc:
        raise ValueError(f"{label} '{path}' does not have a symbolic HEAD branch.") from exc


def source_default_branch(source: Path) -> str:
    try:
        return current_branch(source, "Source repository")
    except ValueError:
        for candidate in ("main", "master"):
            try:
                run_git(["rev-parse", "--verify", f"refs/heads/{candidate}"], source)
                return candidate
            except GitCommandError:
                continue
    raise ValueError(
        "Could not determine the source default branch. Check out the source branch and retry."
    )


def next_remote_name(target: Path) -> str:
    existing = set(run_git(["remote"], target).split())
    candidate = "migration_source"
    suffix = 1
    while candidate in existing:
        suffix += 1
        candidate = f"migration_source_{suffix}"
    return candidate


def add_remote(target: Path, source: Path) -> str:
    remote_name = next_remote_name(target)
    run_git(["remote", "add", remote_name, str(source)], target)
    return remote_name


def fetch_remote(target: Path, remote_name: str) -> None:
    run_git(["fetch", "--quiet", "--tags", remote_name], target)


def remove_remote(target: Path, remote_name: str) -> None:
    try:
        run_git(["remote", "remove", remote_name], target)
    except GitCommandError:
        pass


def copy_source_remotes(source: Path, target: Path, display_target: Path, dry_run: bool) -> None:
    remote_names = run_git(["remote"], source).split()
    if not remote_names:
        print_step("No source remotes to copy.", dry_run=dry_run)
        return

    print_step(
        f"{'Would copy' if dry_run else 'Copying'} {len(remote_names)} source remote(s) "
        f"into {display_target}: {', '.join(remote_names)}.",
        dry_run=dry_run,
    )
    target_remote_names = set(run_git(["remote"], target).split())

    for remote_name in remote_names:
        remote_entries = git_config_matches(source, rf"^remote\.{re.escape(remote_name)}\.")
        action = "replace" if remote_name in target_remote_names else "add"
        print_step(
            f"{'Would ' if dry_run else ''}{action} remote '{remote_name}'.",
            dry_run=dry_run,
        )
        if remote_name in target_remote_names:
            remove_remote(target, remote_name)

        for key, value in remote_entries:
            if dry_run:
                print_step(f"Would set {key} {value}", dry_run=True)
            run_git(["config", "--add", key, value], target)


def subject_line(message: bytes) -> str:
    text = message.decode("utf-8", "replace")
    lines = text.splitlines()
    return lines[0] if lines else "<empty commit message>"


def clone_target_for_dry_run(target: Path) -> tuple[tempfile.TemporaryDirectory, Path]:
    temp_dir = tempfile.TemporaryDirectory(prefix="migrate-commits-dry-run-")
    clone_path = Path(temp_dir.name) / "target"
    completed = subprocess.run(
        ["git", "clone", "--quiet", "--shared", str(target), str(clone_path)],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        temp_dir.cleanup()
        raise RuntimeError(
            f"Unable to prepare dry-run clone for '{target}':\n"
            f"{completed.stdout}{completed.stderr}"
        )

    return temp_dir, clone_path


def ensure_common_history(target: Path, source_commit: str) -> None:
    try:
        merge_base = run_git(["merge-base", "HEAD", source_commit], target).strip()
    except GitCommandError as exc:
        raise ValueError(
            "The target repository does not share history with the selected source commit."
        ) from exc
    if not merge_base:
        raise ValueError(
            "The target repository does not share history with the selected source commit."
        )


def list_pending_commits(target: Path, source_commit: str) -> list[str]:
    output = run_git(
        [
            "rev-list",
            "--reverse",
            "--topo-order",
            "--no-merges",
            "--right-only",
            "--cherry-pick",
            f"HEAD...{source_commit}",
        ],
        target,
    ).strip()
    return [line for line in output.splitlines() if line]


def list_full_history_commits(target: Path, source_commit: str) -> list[str]:
    output = run_git(
        [
            "rev-list",
            "--reverse",
            "--topo-order",
            "--no-merges",
            source_commit,
        ],
        target,
    ).strip()
    return [line for line in output.splitlines() if line]


def read_commit_metadata(source: Path, commit: str) -> CommitMetadata:
    output = run_git(["show", "-s", "--format=%aI%x00%cI", commit], source).strip()
    author_date, committer_date = output.split("\x00")
    raw_commit = run_git_bytes(["cat-file", "commit", commit], source)
    _headers, message = raw_commit.split(b"\n\n", 1)
    return CommitMetadata(
        commit=commit,
        author_date=author_date,
        committer_date=committer_date,
        message=message,
    )


def cherry_pick_commit(
    target: Path,
    metadata: CommitMetadata,
    author_name: str,
    author_email: str,
) -> None:
    try:
        run_git(["cherry-pick", "--no-commit", metadata.commit], target)
    except GitCommandError as exc:
        try:
            run_git(["cherry-pick", "--abort"], target)
        except GitCommandError:
            pass
        raise RuntimeError(f"Cherry-pick failed for {metadata.commit[:12]}. {exc}") from exc

    env = os.environ.copy()
    env.update(
        {
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
            "GIT_COMMITTER_DATE": metadata.committer_date,
        }
    )
    run_git_bytes(
        [
            "commit",
            f"--author={author_name} <{author_email}>",
            f"--date={metadata.author_date}",
            "--cleanup=verbatim",
            "--file=-",
            "--allow-empty",
        ],
        target,
        env=env,
        input_bytes=metadata.message,
    )


def align_empty_target_branch(target: Path, source_branch: str, dry_run: bool) -> None:
    target_branch = current_branch(target, "Target repository")
    if target_branch == source_branch:
        print_step(
            f"Empty target is already using branch '{source_branch}'.",
            dry_run=dry_run,
        )
        return

    print_step(
        f"{'Would repoint' if dry_run else 'Repointing'} empty target branch "
        f"from '{target_branch}' to '{source_branch}'.",
        dry_run=dry_run,
    )
    run_git(["symbolic-ref", "HEAD", f"refs/heads/{source_branch}"], target)


def confirm_target_modification() -> bool:
    response = input("Dry mode is false. This will modify target. Are you sure?(y/N) ")
    return response.strip().lower() == "y"


def execute_migration(
    source: Path,
    working_target: Path,
    display_target: Path,
    source_stop: str,
    source_branch: str,
    target_has_head: bool,
    author_name: str,
    author_email: str,
    dry_run: bool,
) -> int:
    remote_name = next_remote_name(working_target)
    if dry_run:
        print_step(f"Would add remote '{remote_name}' -> {source}", dry_run=True)
    run_git(["remote", "add", remote_name, str(source)], working_target)
    try:
        if dry_run:
            print_step(f"Would fetch source refs from remote '{remote_name}'", dry_run=True)
        fetch_remote(working_target, remote_name)
        try:
            resolve_commit(working_target, source_stop)
        except GitCommandError as exc:
            raise ValueError(
                "The selected source stop point is not reachable from any fetched source ref."
            ) from exc

        if target_has_head:
            ensure_common_history(working_target, source_stop)
            commits = list_pending_commits(working_target, source_stop)
        else:
            print_step(
                f"{'Would replay' if dry_run else 'Replaying'} full history from root "
                f"onto empty target branch '{source_branch}'.",
                dry_run=dry_run,
            )
            align_empty_target_branch(working_target, source_branch, dry_run)
            commits = list_full_history_commits(working_target, source_stop)

        if not commits:
            copy_source_remotes(source, working_target, display_target, dry_run)
            if dry_run:
                print_step(
                    f"Repo B is already up to date through {source_stop[:12]}.",
                    dry_run=True,
                )
                print_step(
                    f"Dry run successful. No changes were written to {display_target}.",
                    dry_run=True,
                )
            else:
                print("No commits to migrate.")
            return 0

        print_step(
            f"{'Would migrate' if dry_run else 'Migrating'} {len(commits)} commit(s) "
            f"from {source} to {display_target} up to {source_stop[:12]}.",
            dry_run=dry_run,
        )
        for index, commit in enumerate(commits, start=1):
            metadata = read_commit_metadata(source, commit)
            if dry_run:
                print_step(
                    f"[{index}/{len(commits)}] Would cherry-pick {commit[:12]}: "
                    f"{subject_line(metadata.message)}",
                    dry_run=True,
                )
                print_step(
                    f"[{index}/{len(commits)}] Would commit as "
                    f"{author_name} <{author_email}> with author date "
                    f"{metadata.author_date} and committer date {metadata.committer_date}.",
                    dry_run=True,
                )
            cherry_pick_commit(working_target, metadata, author_name, author_email)
            if not dry_run:
                print(f"[{index}/{len(commits)}] Cherry-picked {commit[:12]}")

        copy_source_remotes(source, working_target, display_target, dry_run)

        if dry_run:
            print_step(
                f"Dry run successful. No changes were written to {display_target}.",
                dry_run=True,
            )
        else:
            print("Migration successful.")
        return 0
    finally:
        if dry_run:
            print_step(f"Would remove temporary remote '{remote_name}'", dry_run=True)
        remove_remote(working_target, remote_name)


def migrate_commits(
    source_repo: str,
    target_repo: str,
    until: str,
    author_name: str,
    author_email: str,
    dry_run: bool,
) -> int:
    source = Path(source_repo).resolve()
    target = Path(target_repo).resolve()

    if source == target:
        raise ValueError("Source and target repositories must be different paths.")

    require_repository(source, "Source repository")
    require_repository(target, "Target repository")
    require_head(source, "Source repository")
    require_clean_worktree(target, "Target repository")

    target_has_head = repository_has_head(target)
    source_branch = source_default_branch(source)
    source_stop = resolve_commit(source, until)
    if dry_run:
        print_step(
            f"Dry run mode enabled. Repo B will not be modified: {target}",
            dry_run=True,
        )
        print_step(f"Source default branch is '{source_branch}'.", dry_run=True)
        print_step(f"Resolved source stop point to {source_stop[:12]}.", dry_run=True)
        temp_dir, simulation_target = clone_target_for_dry_run(target)
        try:
            print_step(
                f"Using temporary simulation clone at {simulation_target}.",
                dry_run=True,
            )
            return execute_migration(
                source=source,
                working_target=simulation_target,
                display_target=target,
                source_stop=source_stop,
                source_branch=source_branch,
                target_has_head=target_has_head,
                author_name=author_name,
                author_email=author_email,
                dry_run=True,
            )
        finally:
            temp_dir.cleanup()

    if not confirm_target_modification():
        print("Aborted. No changes were made.")
        return 0

    if not target_has_head:
        print_step(f"Source default branch is '{source_branch}'.")

    return execute_migration(
        source=source,
        working_target=target,
        display_target=target,
        source_stop=source_stop,
        source_branch=source_branch,
        target_has_head=target_has_head,
        author_name=author_name,
        author_email=author_email,
        dry_run=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay commits from Repo A into Repo B while rewriting author metadata."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source_repo", help="Path to Repo A.")
    parser.add_argument("target_repo", help="Path to Repo B.")
    parser.add_argument(
        "--until",
        default="HEAD",
        help="Inclusive source commit-ish to migrate up to. Defaults to HEAD.",
    )
    parser.add_argument(
        "--author-name",
        default=DEFAULT_AUTHOR_NAME,
        help=f"Author name for migrated commits. Defaults to '{DEFAULT_AUTHOR_NAME}'.",
    )
    parser.add_argument(
        "--author-email",
        default=DEFAULT_AUTHOR_EMAIL,
        help=(
            "Author email for migrated commits. Defaults to "
            f"'{DEFAULT_AUTHOR_EMAIL}'."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the migration on a temporary clone of Repo B and print each step.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    if not argv:
        parser.print_help(sys.stdout)
        return 0
    args = parser.parse_args(argv)
    try:
        return migrate_commits(
            source_repo=args.source_repo,
            target_repo=args.target_repo,
            until=args.until,
            author_name=args.author_name,
            author_email=args.author_email,
            dry_run=args.dry_run,
        )
    except (GitCommandError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
