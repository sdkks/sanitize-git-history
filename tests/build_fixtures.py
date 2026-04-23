#!/usr/bin/env python3
"""Build tar.gz fixtures for migrate_commits.py tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Sequence

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures"
ARCHIVE_ROOT = FIXTURE_ROOT / "archives"
METADATA_PATH = FIXTURE_ROOT / "metadata.json"
MIGRATE_SCRIPT = ROOT / "migrate_commits.py"
FIXTURE_AUTHOR_NAME = "Fixture Author"
FIXTURE_AUTHOR_EMAIL = "fixture-author@example.com"
FIXTURE_COMMITTER_NAME = "Fixture Committer"
FIXTURE_COMMITTER_EMAIL = "fixture-committer@example.com"
MIGRATED_AUTHOR_NAME = "Migrated Author"
MIGRATED_AUTHOR_EMAIL = "migrated@example.com"


@dataclass(frozen=True)
class CommitSpec:
    label: str
    message: str
    timestamp: str
    is_merge: bool = False


def run_command(
    args: Sequence[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> str:
    completed = subprocess.run(
        list(args),
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed in {cwd}:\n{completed.stdout}{completed.stderr}"
        )
    return completed.stdout


def run_git(
    args: Sequence[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> str:
    return run_command(["git", *args], cwd, env, input_text)


def write_text(path: Path, content: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mode is not None:
        path.chmod(mode)


def append_text(path: Path, content: str) -> None:
    with path.open("a") as handle:
        handle.write(content)


def commit_env(timestamp: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": FIXTURE_AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": FIXTURE_AUTHOR_EMAIL,
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_NAME": FIXTURE_COMMITTER_NAME,
            "GIT_COMMITTER_EMAIL": FIXTURE_COMMITTER_EMAIL,
            "GIT_COMMITTER_DATE": timestamp,
        }
    )
    return env


def configure_repo(repo: Path) -> None:
    run_git(["config", "user.name", FIXTURE_COMMITTER_NAME], repo)
    run_git(["config", "user.email", FIXTURE_COMMITTER_EMAIL], repo)


def record_commit(repo: Path, spec: CommitSpec, registry: List[dict]) -> str:
    commit_hash = run_git(["rev-parse", "HEAD"], repo).strip()
    registry.append(
        {
            "label": spec.label,
            "message": spec.message,
            "timestamp": spec.timestamp,
            "hash": commit_hash,
            "is_merge": spec.is_merge,
        }
    )
    return commit_hash


def create_commit(
    repo: Path,
    spec: CommitSpec,
    mutate: Callable[[], None],
    registry: List[dict],
) -> str:
    mutate()
    run_git(["add", "-A"], repo)
    run_git(
        ["commit", "--cleanup=verbatim", "--file=-"],
        repo,
        env=commit_env(spec.timestamp),
        input_text=spec.message,
    )
    return record_commit(repo, spec, registry)


def create_merge(repo: Path, branch: str, spec: CommitSpec, registry: List[dict]) -> str:
    run_git(
        ["merge", "--no-ff", "--no-edit", "-m", spec.message, branch],
        repo,
        env=commit_env(spec.timestamp),
    )
    return record_commit(repo, spec, registry)


def archive_directory(source_dir: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source_dir, arcname=source_dir.name)


def build_repos(build_root: Path) -> Dict[str, object]:
    repo_a = build_root / "repo_a"
    repo_b = build_root / "repo_b"
    shared_commits: List[dict] = []
    source_only_commits: List[dict] = []

    repo_a.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-b", "master"], repo_a)
    configure_repo(repo_a)

    create_commit(
        repo_a,
        CommitSpec("shared_01", "shared 01 create README", "2024-01-01T00:00:00+00:00"),
        lambda: write_text(repo_a / "README.md", "linear-history test fixture\n"),
        shared_commits,
    )
    create_commit(
        repo_a,
        CommitSpec("shared_02", "shared 02 add base file", "2024-01-02T00:00:00+00:00"),
        lambda: write_text(repo_a / "shared/base.txt", "shared baseline\n"),
        shared_commits,
    )
    create_commit(
        repo_a,
        CommitSpec("shared_03", "shared 03 add tool script", "2024-01-03T00:00:00+00:00"),
        lambda: write_text(repo_a / "bin/tool.sh", "#!/bin/sh\necho baseline\n", mode=0o644),
        shared_commits,
    )

    shared_head = run_git(["rev-parse", "HEAD"], repo_a).strip()
    shutil.copytree(repo_a, repo_b)

    create_commit(
        repo_a,
        CommitSpec(
            "master_01",
            "master 01 add app file\n\nIntroduce the first source-only file.\nThis commit should replay verbatim.",
            "2024-02-01T00:00:00+00:00",
        ),
        lambda: write_text(repo_a / "app/master_01.txt", "master commit 01\n"),
        source_only_commits,
    )
    run_git(["checkout", "-b", "x"], repo_a)
    create_commit(
        repo_a,
        CommitSpec("branch_x_01", "branch x 01 add feature file", "2024-02-02T00:00:00+00:00"),
        lambda: write_text(repo_a / "branch/x_01.txt", "branch x commit 01\n"),
        source_only_commits,
    )
    create_commit(
        repo_a,
        CommitSpec("branch_x_02", "branch x 02 add second feature file", "2024-02-03T00:00:00+00:00"),
        lambda: write_text(repo_a / "branch/x_02.txt", "branch x commit 02\n"),
        source_only_commits,
    )
    run_git(["checkout", "master"], repo_a)
    create_commit(
        repo_a,
        CommitSpec("master_02", "master 02 add second app file", "2024-02-04T00:00:00+00:00"),
        lambda: write_text(repo_a / "app/master_02.txt", "master commit 02\n"),
        source_only_commits,
    )
    create_merge(
        repo_a,
        "x",
        CommitSpec("merge_x_01", "merge x into master 1", "2024-02-05T00:00:00+00:00", is_merge=True),
        source_only_commits,
    )
    create_commit(
        repo_a,
        CommitSpec(
            "master_03",
            "master 03 add docs file\n\nDocument the branch merge outcome.\n\n- keeps the tree stable\n- exercises multiline messages",
            "2024-02-06T00:00:00+00:00",
        ),
        lambda: write_text(repo_a / "docs/master_03.md", "master commit 03\n"),
        source_only_commits,
    )
    run_git(["checkout", "x"], repo_a)
    create_commit(
        repo_a,
        CommitSpec("branch_x_03", "branch x 03 add third feature file", "2024-02-07T00:00:00+00:00"),
        lambda: write_text(repo_a / "branch/x_03.txt", "branch x commit 03\n"),
        source_only_commits,
    )
    create_commit(
        repo_a,
        CommitSpec("branch_x_04", "branch x 04 chmod tool", "2024-02-08T00:00:00+00:00"),
        lambda: (
            append_text(repo_a / "bin/tool.sh", "echo branch-x\n"),
            (repo_a / "bin/tool.sh").chmod(0o755),
        ),
        source_only_commits,
    )
    run_git(["checkout", "master"], repo_a)
    create_commit(
        repo_a,
        CommitSpec("master_04", "master 04 add final app file", "2024-02-09T00:00:00+00:00"),
        lambda: write_text(repo_a / "app/master_04.txt", "master commit 04\n"),
        source_only_commits,
    )
    create_merge(
        repo_a,
        "x",
        CommitSpec("merge_x_02", "merge x into master 2", "2024-02-10T00:00:00+00:00", is_merge=True),
        source_only_commits,
    )

    source_head = run_git(["rev-parse", "HEAD"], repo_a).strip()

    return {
        "repo_a": repo_a,
        "repo_b": repo_b,
        "shared_commits": shared_commits,
        "source_only_commits": source_only_commits,
        "shared_head": shared_head,
        "source_head": source_head,
    }


def build_completed_archives(initial_repo_a: Path, initial_repo_b: Path, build_root: Path) -> Dict[str, str]:
    completed_root = build_root / "completed"
    repo_a_completed = completed_root / "repo_a"
    repo_b_completed = completed_root / "repo_b"
    completed_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(initial_repo_a, repo_a_completed)
    shutil.copytree(initial_repo_b, repo_b_completed)
    run_command(
        [
            "python3",
            str(MIGRATE_SCRIPT),
            str(repo_a_completed),
            str(repo_b_completed),
            "--author-name",
            MIGRATED_AUTHOR_NAME,
            "--author-email",
            MIGRATED_AUTHOR_EMAIL,
        ],
        ROOT,
        input_text="y\n",
    )
    return {
        "repo_a": str(repo_a_completed),
        "repo_b": str(repo_b_completed),
    }


def main() -> int:
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary_dir:
        build_root = Path(temporary_dir)
        repo_data = build_repos(build_root)
        initial_repo_a = repo_data["repo_a"]
        initial_repo_b = repo_data["repo_b"]

        archive_directory(initial_repo_a, ARCHIVE_ROOT / "repo_a_initial.tar.gz")
        archive_directory(initial_repo_b, ARCHIVE_ROOT / "repo_b_initial.tar.gz")

        completed_paths = build_completed_archives(initial_repo_a, initial_repo_b, build_root)
        archive_directory(Path(completed_paths["repo_a"]), ARCHIVE_ROOT / "repo_a_completed.tar.gz")
        archive_directory(Path(completed_paths["repo_b"]), ARCHIVE_ROOT / "repo_b_completed.tar.gz")

        metadata = {
            "branch": "master",
            "shared_commit_count": len(repo_data["shared_commits"]),
            "shared_head": repo_data["shared_head"],
            "source_head": repo_data["source_head"],
            "shared_commits": repo_data["shared_commits"],
            "source_only_commits": repo_data["source_only_commits"],
            "stop_points": {
                item["label"]: item["hash"] for item in repo_data["source_only_commits"]
            },
            "migration_author": {
                "name": MIGRATED_AUTHOR_NAME,
                "email": MIGRATED_AUTHOR_EMAIL,
            },
            "archives": {
                "initial": {
                    "repo_a": "repo_a_initial.tar.gz",
                    "repo_b": "repo_b_initial.tar.gz",
                },
                "completed": {
                    "repo_a": "repo_a_completed.tar.gz",
                    "repo_b": "repo_b_completed.tar.gz",
                },
            },
        }
        METADATA_PATH.write_text(json.dumps(metadata, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
