# Profile asset refresh

The `Refresh profile assets` workflow runs on source-branch pushes, daily at 07:17 UTC, and manual dispatch. It does not run when `profile-assets` is pushed. No personal access token or third-party widget service is used.

The generation job has `contents: read`. Its repository `GITHUB_TOKEN` requests public repository and follower totals from the user REST endpoint and uses GraphQL to sum stars on owned public repositories excluding forks. The contribution graph uses the publicly visible profile calendar for the rolling calendar year ending on the UTC refresh date. If the profile displays anonymous private activity, GitHub may include those counts; no private repository names or details are requested. Repository totals include forks; the stars total does not.

The six custom SVG graphics include accessible descriptions and a UTC refresh date. The SHA-pinned `Platane/snk/svg-only` action creates light and dark snake animations with the same repository token. Two additional SVGs preserve the snake's initial calendar colors and geometry while removing CSS keyframes, animation declarations, and SMIL animation for reduced-motion readers.

After all ten SVGs and `profile-data.json` pass validation, one `profile-assets` artifact is uploaded. Feature-branch runs stop there. Only runs from `main` enter the publication job with `contents: write`. Publication builds a Git tree from the entire validated artifact and pushes a single commit to `profile-assets`. It never changes the source checkout, skips identical bundles, and uses a normal non-forced push. A generation failure, incomplete artifact, or rejected push leaves the previously published bundle intact. Publication is serialized across workflow runs.

## Local checks

```sh
python3 -m unittest discover -s tests -v
```

Generate with an existing short-lived GitHub token supplied through `GH_TOKEN` (never commit it):

```sh
python3 scripts/generate_profile_assets.py --username Xuefeng-Zhu --output build/profile-assets
```

After the pinned action has added `github-snake.svg` and `github-snake-dark.svg` to that directory:

```sh
python3 scripts/generate_profile_assets.py --make-static-snakes build/profile-assets
python3 scripts/generate_profile_assets.py --validate-bundle build/profile-assets
```

## First preview bundle

Before opening the redesign PR, download the first successful feature-branch workflow's `profile-assets` artifact, run the complete-bundle validation above, and seed the dedicated `profile-assets` branch from exactly that artifact:

```sh
python3 scripts/publish_profile_assets.py --bundle /path/to/downloaded-artifact --initialize-from-artifact
```

This explicit initializer only works locally, outside GitHub Actions, and refuses to replace an existing `profile-assets` branch. The normal publisher remains restricted to `main`. The one-time seed makes the new README's raw GitHub asset links work before merge. Subsequent `main` workflow runs publish automatically. Record the successful source run URL and seed commit in the PR verification notes.

All workflow actions are pinned to full commit SHAs. Update pins after inspecting the corresponding upstream release and rerun the complete workflow before merging an update.
