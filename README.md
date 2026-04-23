# `migrate_commits.py`

`migrate_commits.py` replays commits from Repo A into Repo B with sanitized author and committer identities.

Primary use case: sanitize a private repository before publishing it. The script keeps commit order, dates, and commit messages, but rewrites the human identity fields so original author names and email addresses are not exposed in the public history.

You can change your author name to something like `sdkks <sdkks@users.noreply.github.com>`. This way your email won't be public and you won't unsolicited emails and spam. Of course, please change your github username to your own one.

## What It Does

- Replays the full sanitized history from Repo A into a freshly initialized empty Repo B.
- Cherry-picks only source-only commits when Repo B already shares a baseline with Repo A.
- Rewrites both author and committer name/email to the values you provide.
- Copies source `remote.*` configuration into Repo B.
- Preserves author date and committer date.
- Preserves the exact original commit message, including multiline bodies.
- Flattens merges by replaying reachable non-merge commits in topological order.
- Can stop at a specific source commit with `--until`, inclusive.
- Supports `--dry-run` to simulate the migration and print each step without changing Repo B.
- Skips already-applied patches on reruns, so you can continue incrementally.

## Design Objectives

- Protect original contributor identities when preparing a public version of a repository.
- Support a fresh empty public-facing Repo B so no original source objects or author identities need to be carried forward.
- Keep the final working tree identical to Repo A outside `.git`.
- Preserve useful history signals:
  commit order, commit message text, author date, and committer date.
- Be repeatable:
  rerunning against the same Repo B should only pick up remaining commits.
- Fail early if the repositories do not share the expected baseline history.

## Supported Target Modes

### Empty Repo B, recommended for sanitization

Repo B may be a freshly initialized repository with zero commits. In that mode, the script replays the full reachable non-merge history from Repo A starting at the root commit.

If Repo B was initialized with a different default branch name, the script repoints the unborn target branch to match Repo A before writing the first commit. For example, if Repo A uses `master` and Repo B was initialized as `main`, Repo B will be switched to `master`.

This is the recommended mode when you want to sanitize a private repository before publishing it.

### Shared-baseline Repo B

Repo B may also already contain the shared baseline history. In that mode, the script replays only the commits that are present in Repo A but not yet applied in Repo B.

## Recommended Flow For Public Sanitization

1. Create a new empty Repo B.
2. Initialize it with any default branch name:

```bash
git init -b main /path/to/repo-b
```

3. Replay the sanitized history from Repo A into Repo B.

## Shared-Baseline Flow

If you intentionally want incremental replay instead of a clean-room public repository:

1. Create Repo A and commit the shared baseline.
2. Copy Repo A to create Repo B.
3. Add the private/source-only commits to Repo A.
4. Run `migrate_commits.py` to replay those commits into Repo B with new identities.

## Usage

```bash
./migrate_commits.py /path/to/repo-a /path/to/repo-b \
  --author-name "Open Source Maintainer" \
  --author-email "oss@example.com"
```

Fresh empty Repo B (create the directory and run `git init`), full replay:

```bash
git init -b main /path/to/repo-b
./migrate_commits.py /path/to/repo-a /path/to/repo-b \
  --author-name "Open Source Maintainer" \
  --author-email "oss@example.com"
```

Stop at a specific source commit, inclusive:

```bash
./migrate_commits.py /path/to/repo-a /path/to/repo-b \
  --until 4f3c2b1 \
  --author-name "Open Source Maintainer" \
  --author-email "oss@example.com"
```

Show help and examples:

```bash
./migrate_commits.py
```

If you run without `--dry-run`, the script asks for confirmation before it changes Repo B:

```text
Dry mode is false. This will modify target. Are you sure?(y/N)
```

Any answer other than `y` aborts the run without changing Repo B.

Preview every step without changing Repo B:

```bash
./migrate_commits.py /path/to/repo-a /path/to/repo-b \
  --dry-run \
  --author-name "Open Source Maintainer" \
  --author-email "oss@example.com"
```

In dry-run mode, the script validates the real repositories, creates a temporary simulation clone of Repo B, and runs the same replay logic there. The real Repo B is not modified. The dry-run output also includes the source remote configuration that would be copied into Repo B.

## Configure Repo B For Future Commits

If you plan to keep working in Repo B after the sanitized replay, set the identity at the repository level so new commits do not fall back to your global Git config.

```bash
git -C /path/to/repo-b config user.name "Open Source Maintainer"
git -C /path/to/repo-b config user.email "oss@example.com"
```

That writes the values into Repo B's local `.git/config`, which overrides the global `~/.gitconfig` values for commits created in that repository.

You can verify the local values with:

```bash
git -C /path/to/repo-b config --local --get user.name
git -C /path/to/repo-b config --local --get user.email
```

## How Repo B Returns To Baseline

### If Repo B started empty

The simplest reset is to delete Repo B and initialize it again:

```bash
rm -rf /path/to/repo-b
git init -b main /path/to/repo-b
```

If you want to return Repo B to an unborn branch in place:

```bash
git -C /path/to/repo-b update-ref -d HEAD
git -C /path/to/repo-b reflog expire --expire=now --all
git -C /path/to/repo-b gc --prune=now
```

That deletes the current branch tip and prunes unreachable objects. The repository remains initialized but has no commits again.

### If Repo B started from a shared baseline copy

Because Repo B starts as a copy of Repo A, “baseline” is the last shared commit before sanitized commits were replayed. It is not an empty repository.

The safest reset flow is:

1. Record the baseline commit before the first migration run.

```bash
BASELINE=$(git -C /path/to/repo-b rev-parse HEAD)
```

2. Run the migration.
3. If you need to discard the sanitized commits and go back to the shared baseline:

```bash
git -C /path/to/repo-b reset --hard "$BASELINE"
```

That removes the migrated commits from the current branch tip.

If you want the local object store cleaned up as well:

```bash
git -C /path/to/repo-b reflog expire --expire=now --all
git -C /path/to/repo-b gc --prune=now
```

If you prefer a disposable workflow, do not reset in place. Delete Repo B and recreate it from the baseline copy or from an archived baseline tarball.

## Current Behavior Around Merges

Source merge commits are not recreated as merge commits in Repo B. Instead, the script replays the source-only non-merge commits needed to reach the same final tree state.

That means the resulting history is sanitized and linearized rather than graph-identical to Repo A.

## Test Fixtures

The test suite includes archived fixtures under `tests/fixtures/archives/`:

- `repo_a_initial.tar.gz`
- `repo_b_initial.tar.gz`
- `repo_a_completed.tar.gz`
- `repo_b_completed.tar.gz`

Those fixtures are used to verify:

- full migration
- partial migration with `--until`
- incremental continuation
- exact commit message preservation, including multiline messages
- final tree equality outside `.git`
