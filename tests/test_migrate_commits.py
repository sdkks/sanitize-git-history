from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.compare_repo_trees import compare_repositories

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures"
ARCHIVE_ROOT = FIXTURE_ROOT / "archives"
METADATA_PATH = FIXTURE_ROOT / "metadata.json"
BUILD_FIXTURES_SCRIPT = ROOT / "tests" / "build_fixtures.py"
MIGRATE_SCRIPT = ROOT / "migrate_commits.py"
COMPARE_SCRIPT = ROOT / "tests" / "compare_repo_trees.py"


def ensure_fixtures() -> None:
    required_archives = [
        ARCHIVE_ROOT / "repo_a_initial.tar.gz",
        ARCHIVE_ROOT / "repo_b_initial.tar.gz",
        ARCHIVE_ROOT / "repo_a_completed.tar.gz",
        ARCHIVE_ROOT / "repo_b_completed.tar.gz",
    ]
    if METADATA_PATH.exists() and all(path.exists() for path in required_archives):
        return

    completed = subprocess.run(
        ["python3", str(BUILD_FIXTURES_SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Fixture build failed:\n{completed.stdout}\n{completed.stderr}"
        )


ensure_fixtures()
METADATA = json.loads(METADATA_PATH.read_text())


def run_command(
    args: list[str],
    cwd: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, input=input_text, capture_output=True)


def run_git_raw(args: list[str], cwd: Path) -> str:
    completed = run_command(["git", *args], cwd)
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}:\n{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout


def run_git(args: list[str], cwd: Path) -> str:
    return run_git_raw(args, cwd).strip()


def read_commit_message(commit: str, repo_path: Path) -> str:
    return run_git_raw(["show", "-s", "--format=%B", commit], repo_path)


def remote_config_lines(repo_path: Path) -> list[str]:
    completed = run_command(
        ["git", "config", "--get-regexp", r"^remote\."],
        repo_path,
    )
    if completed.returncode == 1:
        return []
    if completed.returncode != 0:
        raise AssertionError(
            f"git config --get-regexp '^remote\\.' failed in {repo_path}:\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return sorted(completed.stdout.strip().splitlines())


def extract_archive(archive_path: Path, destination: Path) -> Path:
    with tarfile.open(archive_path, "r:gz") as archive:
        roots = {
            Path(member.name).parts[0]
            for member in archive.getmembers()
            if member.name and member.name != "."
        }
        archive.extractall(destination)
    if len(roots) != 1:
        raise AssertionError(f"Archive {archive_path} should contain a single top-level directory.")
    return destination / roots.pop()


def migration_commits_after(shared_head: str, repo_path: Path) -> list[str]:
    output = run_git(["rev-list", "--reverse", "HEAD", f"^{shared_head}"], repo_path)
    return [line for line in output.splitlines() if line]


def source_replay_commits(start: str, stop: str, repo_path: Path) -> list[str]:
    output = run_git(
        ["rev-list", "--reverse", "--topo-order", "--no-merges", f"{start}..{stop}"],
        repo_path,
    )
    return [line for line in output.splitlines() if line]


def source_full_replay_commits(stop: str, repo_path: Path) -> list[str]:
    output = run_git(
        ["rev-list", "--reverse", "--topo-order", "--no-merges", stop],
        repo_path,
    )
    return [line for line in output.splitlines() if line]


def init_empty_repo(path: Path, branch: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    completed = run_command(["git", "init", "-b", branch, str(path)], ROOT)
    if completed.returncode != 0:
        raise AssertionError(
            f"git init failed in {path}:\n{completed.stdout}\n{completed.stderr}"
        )


def init_bare_repo(path: Path) -> None:
    completed = run_command(["git", "init", "--bare", str(path)], ROOT)
    if completed.returncode != 0:
        raise AssertionError(
            f"git init --bare failed in {path}:\n{completed.stdout}\n{completed.stderr}"
        )


class MigrateCommitsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def restore_pair(self, state: str, container_name: str) -> tuple[Path, Path]:
        destination = self.temp_path / container_name
        destination.mkdir(parents=True, exist_ok=True)
        repo_a = extract_archive(
            ARCHIVE_ROOT / METADATA["archives"][state]["repo_a"],
            destination,
        )
        repo_b = extract_archive(
            ARCHIVE_ROOT / METADATA["archives"][state]["repo_b"],
            destination,
        )
        return repo_a, repo_b

    def restore_repo_a(self, state: str, container_name: str) -> Path:
        destination = self.temp_path / container_name
        destination.mkdir(parents=True, exist_ok=True)
        return extract_archive(
            ARCHIVE_ROOT / METADATA["archives"][state]["repo_a"],
            destination,
        )

    def run_migration(
        self,
        repo_a: Path,
        repo_b: Path,
        *extra_args: str,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if input_text is None and "--dry-run" not in extra_args:
            input_text = "y\n"
        return run_command(
            [
                "python3",
                str(MIGRATE_SCRIPT),
                str(repo_a),
                str(repo_b),
                "--author-name",
                METADATA["migration_author"]["name"],
                "--author-email",
                METADATA["migration_author"]["email"],
                *extra_args,
            ],
            ROOT,
            input_text=input_text,
        )

    def add_source_remotes(self, repo_a: Path) -> None:
        origin = self.temp_path / f"{repo_a.name}_origin.git"
        backup = self.temp_path / f"{repo_a.name}_backup.git"
        init_bare_repo(origin)
        init_bare_repo(backup)

        for name, remote_path in (("origin", origin), ("backup", backup)):
            completed = run_command(["git", "remote", "add", name, str(remote_path)], repo_a)
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            )

        completed = run_command(
            ["git", "config", "--add", "remote.origin.fetch", "+refs/changes/*:refs/remotes/origin/changes/*"],
            repo_a,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )

    def test_non_dry_run_requires_confirmation(self) -> None:
        repo_a, repo_b = self.restore_pair("initial", "confirm_abort")
        initial_head = run_git(["rev-parse", "HEAD"], repo_b)
        initial_remotes = run_git(["remote"], repo_b)

        result = self.run_migration(repo_a, repo_b, input_text="n\n")
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn(
            "Dry mode is false. This will modify target. Are you sure?(y/N)",
            result.stdout,
        )
        self.assertIn("Aborted. No changes were made.", result.stdout)
        self.assertEqual(initial_head, run_git(["rev-parse", "HEAD"], repo_b))
        self.assertEqual(initial_remotes, run_git(["remote"], repo_b))

    def test_no_args_prints_help_and_examples(self) -> None:
        result = run_command(["python3", str(MIGRATE_SCRIPT)], ROOT)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("usage:", result.stdout)
        self.assertIn("Examples:", result.stdout)
        self.assertIn("--dry-run", result.stdout)
        self.assertIn("--until", result.stdout)
        self.assertEqual("", result.stderr)

    def test_dry_run_prints_plan_and_leaves_repo_b_unchanged(self) -> None:
        repo_a, repo_b = self.restore_pair("initial", "dry_run")
        initial_head = run_git(["rev-parse", "HEAD"], repo_b)
        initial_remotes = run_git(["remote"], repo_b)
        shared_head = METADATA["shared_head"]
        expected_commits = source_replay_commits(shared_head, METADATA["source_head"], repo_a)

        result = self.run_migration(repo_a, repo_b, "--dry-run")
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

        self.assertIn("[dry-run] Dry run mode enabled.", result.stdout)
        self.assertIn("Would add remote", result.stdout)
        self.assertIn("Would fetch source refs", result.stdout)
        self.assertIn("Would cherry-pick", result.stdout)
        self.assertIn("Dry run successful. No changes were written", result.stdout)
        self.assertEqual(len(expected_commits), result.stdout.count("Would cherry-pick"))

        self.assertEqual(initial_head, run_git(["rev-parse", "HEAD"], repo_b))
        self.assertEqual(initial_remotes, run_git(["remote"], repo_b))
        self.assertEqual([], migration_commits_after(shared_head, repo_b))
        self.assertTrue(compare_repositories(repo_a, repo_b))

    def test_empty_target_replays_full_history_and_matches_source_branch(self) -> None:
        repo_a = self.restore_repo_a("initial", "empty_target_full_source")
        repo_b = self.temp_path / "empty_target_full_repo_b"
        init_empty_repo(repo_b, "main")
        self.add_source_remotes(repo_a)

        result = self.run_migration(repo_a, repo_b)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

        self.assertIn("Source default branch is 'master'.", result.stdout)
        self.assertIn("Replaying full history from root onto empty target branch 'master'.", result.stdout)
        self.assertIn("Repointing empty target branch from 'main' to 'master'.", result.stdout)
        self.assertIn("Copying 2 source remote(s)", result.stdout)
        self.assertEqual("master", run_git(["symbolic-ref", "--short", "HEAD"], repo_b))
        self.assertEqual([], compare_repositories(repo_a, repo_b))
        self.assertEqual(remote_config_lines(repo_a), remote_config_lines(repo_b))

        source_commits = source_full_replay_commits(METADATA["source_head"], repo_a)
        target_commits = run_git(["rev-list", "--reverse", "HEAD"], repo_b).splitlines()
        self.assertEqual(len(source_commits), len(target_commits))

        for source_commit, target_commit in zip(source_commits, target_commits):
            source_message = read_commit_message(source_commit, repo_a)
            target_message = read_commit_message(target_commit, repo_b)
            self.assertEqual(source_message, target_message)

            target_author = run_git(["show", "-s", "--format=%an|%ae", target_commit], repo_b)
            self.assertEqual(
                target_author,
                f"{METADATA['migration_author']['name']}|{METADATA['migration_author']['email']}",
            )

            source_dates = run_git(["show", "-s", "--format=%aI|%cI", source_commit], repo_a)
            target_dates = run_git(["show", "-s", "--format=%aI|%cI", target_commit], repo_b)
            self.assertEqual(source_dates, target_dates)

    def test_empty_target_dry_run_preserves_unborn_branch(self) -> None:
        repo_a = self.restore_repo_a("initial", "empty_target_dry_source")
        repo_b = self.temp_path / "empty_target_dry_repo_b"
        init_empty_repo(repo_b, "main")
        self.add_source_remotes(repo_a)

        result = self.run_migration(repo_a, repo_b, "--dry-run")
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

        self.assertIn("[dry-run] Source default branch is 'master'.", result.stdout)
        self.assertIn("Would replay full history from root onto empty target branch 'master'.", result.stdout)
        self.assertIn("Would repoint empty target branch from 'main' to 'master'.", result.stdout)
        self.assertIn("Would copy 2 source remote(s)", result.stdout)
        self.assertEqual("main", run_git(["symbolic-ref", "--short", "HEAD"], repo_b))
        self.assertEqual("", run_git(["remote"], repo_b))

        head_check = run_command(["git", "rev-parse", "--verify", "HEAD^{commit}"], repo_b)
        self.assertNotEqual(0, head_check.returncode)

    def test_full_migration_matches_source_tree_and_rewrites_metadata(self) -> None:
        repo_a, repo_b = self.restore_pair("initial", "full")

        result = self.run_migration(repo_a, repo_b)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

        differences = compare_repositories(repo_a, repo_b)
        self.assertEqual([], differences)

        shared_head = METADATA["shared_head"]
        source_commits = source_replay_commits(shared_head, METADATA["source_head"], repo_a)
        target_commits = migration_commits_after(shared_head, repo_b)
        self.assertEqual(len(source_commits), len(target_commits))

        for source_commit, target_commit in zip(source_commits, target_commits):
            source_message = read_commit_message(source_commit, repo_a)
            target_message = read_commit_message(target_commit, repo_b)
            self.assertEqual(source_message, target_message)

            target_author = run_git(["show", "-s", "--format=%an|%ae", target_commit], repo_b)
            self.assertEqual(
                target_author,
                f"{METADATA['migration_author']['name']}|{METADATA['migration_author']['email']}",
            )

            source_dates = run_git(["show", "-s", "--format=%aI|%cI", source_commit], repo_a)
            target_dates = run_git(["show", "-s", "--format=%aI|%cI", target_commit], repo_b)
            self.assertEqual(source_dates, target_dates)

        multiline_commit = next(
            commit for commit in METADATA["source_only_commits"] if "\n" in commit["message"]
        )
        source_message = read_commit_message(multiline_commit["hash"], repo_a)
        target_commit = next(
            commit
            for source_commit, commit in zip(source_commits, target_commits)
            if source_commit == multiline_commit["hash"]
        )
        self.assertIn("\n\n", source_message)
        self.assertEqual(source_message, read_commit_message(target_commit, repo_b))

    def test_until_hash_is_inclusive_and_matches_source_tree_at_that_commit(self) -> None:
        repo_a, repo_b = self.restore_pair("initial", "partial")
        stop_hash = METADATA["stop_points"]["master_03"]

        result = self.run_migration(repo_a, repo_b, "--until", stop_hash)
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

        expected_worktree = self.temp_path / "expected_source_at_stop"
        worktree_add = run_command(
            ["git", "worktree", "add", "--detach", str(expected_worktree), stop_hash],
            repo_a,
        )
        self.assertEqual(
            worktree_add.returncode,
            0,
            msg=f"stdout:\n{worktree_add.stdout}\nstderr:\n{worktree_add.stderr}",
        )

        try:
            differences = compare_repositories(expected_worktree, repo_b)
            self.assertEqual([], differences)
        finally:
            run_command(["git", "worktree", "remove", "--force", str(expected_worktree)], repo_a)

        shared_head = METADATA["shared_head"]
        expected_commits = source_replay_commits(shared_head, stop_hash, repo_a)
        target_commits = migration_commits_after(shared_head, repo_b)
        self.assertEqual(len(expected_commits), len(target_commits))

        head_message = read_commit_message("HEAD", repo_b)
        stop_message = read_commit_message(stop_hash, repo_a)
        self.assertEqual(stop_message, head_message)

    def test_incremental_migration_skips_already_applied_commits(self) -> None:
        repo_a, repo_b = self.restore_pair("initial", "incremental")
        shared_head = METADATA["shared_head"]
        first_stop = METADATA["stop_points"]["master_03"]

        first_run = self.run_migration(repo_a, repo_b, "--until", first_stop)
        self.assertEqual(
            first_run.returncode,
            0,
            msg=f"stdout:\n{first_run.stdout}\nstderr:\n{first_run.stderr}",
        )
        first_count = len(migration_commits_after(shared_head, repo_b))

        second_run = self.run_migration(repo_a, repo_b, "--until", METADATA["source_head"])
        self.assertEqual(
            second_run.returncode,
            0,
            msg=f"stdout:\n{second_run.stdout}\nstderr:\n{second_run.stderr}",
        )
        second_count = len(migration_commits_after(shared_head, repo_b))

        full_expected_count = len(source_replay_commits(shared_head, METADATA["source_head"], repo_a))
        self.assertLess(first_count, full_expected_count)
        self.assertEqual(full_expected_count, second_count)
        self.assertEqual([], compare_repositories(repo_a, repo_b))

    def test_archived_completed_repos_match_and_initial_repos_do_not(self) -> None:
        initial_a, initial_b = self.restore_pair("initial", "initial_archives")
        self.assertTrue(compare_repositories(initial_a, initial_b))

        completed_a, completed_b = self.restore_pair("completed", "completed_archives")
        cli_result = run_command(
            ["python3", str(COMPARE_SCRIPT), str(completed_a), str(completed_b)],
            ROOT,
        )
        self.assertEqual(
            cli_result.returncode,
            0,
            msg=f"stdout:\n{cli_result.stdout}\nstderr:\n{cli_result.stderr}",
        )
        self.assertEqual([], compare_repositories(completed_a, completed_b))


if __name__ == "__main__":
    unittest.main()
