#!/usr/bin/env python3
"""Compare two repository working trees while ignoring .git and timestamps."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Dict, List


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def should_skip(relative_path: Path) -> bool:
    return relative_path.name == ".git" or ".git" in relative_path.parts


def snapshot_tree(root: Path) -> Dict[str, dict]:
    snapshot: Dict[str, dict] = {}
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if should_skip(relative_path):
            continue

        entry = {"kind": "other"}
        stat_result = path.lstat()
        mode = stat.S_IMODE(stat_result.st_mode)

        if path.is_symlink():
            entry = {
                "kind": "symlink",
                "mode": mode,
                "target": os.readlink(path),
            }
        elif path.is_dir():
            entry = {"kind": "dir"}
        elif path.is_file():
            entry = {
                "kind": "file",
                "mode": mode,
                "size": stat_result.st_size,
                "sha256": file_digest(path),
            }

        snapshot[str(relative_path)] = entry
    return snapshot


def compare_repositories(left_root: Path, right_root: Path) -> List[str]:
    left_snapshot = snapshot_tree(left_root)
    right_snapshot = snapshot_tree(right_root)

    differences: List[str] = []
    for relative_path in sorted(set(left_snapshot) | set(right_snapshot)):
        left_entry = left_snapshot.get(relative_path)
        right_entry = right_snapshot.get(relative_path)

        if left_entry is None:
            differences.append(f"Missing from left: {relative_path}")
            continue
        if right_entry is None:
            differences.append(f"Missing from right: {relative_path}")
            continue
        if left_entry["kind"] != right_entry["kind"]:
            differences.append(
                f"Type mismatch for {relative_path}: {left_entry['kind']} != {right_entry['kind']}"
            )
            continue
        if left_entry["kind"] == "file":
            if left_entry["mode"] != right_entry["mode"]:
                differences.append(
                    f"Mode mismatch for {relative_path}: {left_entry['mode']:o} != {right_entry['mode']:o}"
                )
            if left_entry["size"] != right_entry["size"]:
                differences.append(
                    f"Size mismatch for {relative_path}: {left_entry['size']} != {right_entry['size']}"
                )
            if left_entry["sha256"] != right_entry["sha256"]:
                differences.append(f"Content mismatch for {relative_path}")
        elif left_entry["kind"] == "symlink":
            if left_entry["mode"] != right_entry["mode"]:
                differences.append(
                    f"Symlink mode mismatch for {relative_path}: "
                    f"{left_entry['mode']:o} != {right_entry['mode']:o}"
                )
            if left_entry["target"] != right_entry["target"]:
                differences.append(
                    f"Symlink target mismatch for {relative_path}: "
                    f"{left_entry['target']} != {right_entry['target']}"
                )
    return differences


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two repository trees while ignoring .git and timestamps."
    )
    parser.add_argument("left_repo", help="First repository path.")
    parser.add_argument("right_repo", help="Second repository path.")
    args = parser.parse_args(argv)

    differences = compare_repositories(Path(args.left_repo), Path(args.right_repo))
    if differences:
        print("Repository trees differ:")
        for difference in differences:
            print(difference)
        return 1

    print("Repository trees match outside .git.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
