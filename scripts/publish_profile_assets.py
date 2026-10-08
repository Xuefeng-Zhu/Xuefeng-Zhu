#!/usr/bin/env python3
"""Publish one validated bundle as one commit, without altering the source checkout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile

from generate_profile_assets import validate_bundle


def publish(repository: Path, bundle: Path, initialize: bool = False) -> bool:
    validate_bundle(bundle)  # Incomplete/failed generation must never reach Git.
    if initialize and os.environ.get("GITHUB_ACTIONS"):
        raise RuntimeError("The one-time artifact initializer is only allowed outside GitHub Actions")
    if not initialize and os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise RuntimeError("Only runs from main may publish profile assets")
    env = dict(os.environ, GIT_AUTHOR_NAME="github-actions[bot]", GIT_COMMITTER_NAME="github-actions[bot]",
               GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
               GIT_COMMITTER_EMAIL="41898282+github-actions[bot]@users.noreply.github.com")

    def git(*args: str, data: str | None = None) -> str:
        return subprocess.run(["git", "-C", str(repository), *args], input=data, text=True,
                              capture_output=True, check=True, env=env).stdout.strip()

    remote_ref = "refs/heads/profile-assets"
    exists = bool(git("ls-remote", "--heads", "origin", remote_ref))
    if initialize and exists:
        raise RuntimeError("The profile-assets branch already exists; initialization cannot replace it")
    parent = None
    if exists:
        git("fetch", "--no-tags", "origin", remote_ref)
        parent = git("rev-parse", "FETCH_HEAD")
    with tempfile.TemporaryDirectory(prefix="profile-assets-index-") as temporary:
        env["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
        git("read-tree", "--empty")
        for path in sorted(bundle.iterdir()):
            blob = git("hash-object", "-w", "--stdin", data=path.read_text(encoding="utf-8"))
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path.name}")
        tree = git("write-tree")
        if parent and tree == git("rev-parse", parent + "^{tree}"):
            print("Profile assets are unchanged; no commit created.")
            return False
        commit_args = ["commit-tree", tree] + (["-p", parent] if parent else [])
        commit = git(*commit_args, data="Refresh complete profile asset bundle\n")
        # A normal, non-forced push rejects concurrent changes and preserves the old bundle.
        git("push", "origin", commit + ":" + remote_ref)
        print("Published one complete profile asset bundle.")
        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--initialize-from-artifact", action="store_true",
                        help="One-time local seed of a nonexistent asset branch from a verified workflow artifact")
    args = parser.parse_args()
    publish(args.repository.resolve(), args.bundle.resolve(), initialize=args.initialize_from_artifact)
