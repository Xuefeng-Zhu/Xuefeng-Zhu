"""Tests for data truthfulness, accessible SVGs, and atomic asset publication."""

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_profile_assets as assets
import publish_profile_assets as publisher


TODAY = dt.date(2026, 10, 8)
SNAKE = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" width="40" height="40">
<style>:root{--ce:#eee;--c1:#087e97;--cs:#7254bd}.c{fill:var(--ce);width:12px;height:12px;animation:none 10s linear infinite}.c.c1{fill:var(--c1);animation-name:c1}@keyframes c1{0%{fill:var(--c1)}100%{fill:var(--ce)}}.s{fill:var(--cs);animation:none 10s infinite}.s.s0{transform:translate(0px,16px);animation-name:s0}@keyframes s0{0%{transform:translate(0px,16px)}100%{transform:translate(16px,16px)}}</style>
<rect class="c c1" x="0" y="0"/><rect class="s s0" x="0" y="0" width="12" height="12"/></svg>'''


def snapshot():
    start, end = assets.date_window(TODAY)
    return {"username": "Xuefeng-Zhu", "generated_date": str(TODAY), "window_start": str(start),
            "window_end": str(end), "followers": 0, "public_repos": 0, "project_stars": 0,
            "visible_contributions": 0, "days": assets.normalize_calendar([], start, end)}


def response(nodes=None, has_next=False, cursor=None):
    data = snapshot()
    return {"data": {"user": {"login": "Xuefeng-Zhu", "contributionsCollection": {
        "contributionCalendar": {"weeks": [{"contributionDays": [
            {"date": day["date"], "contributionCount": day["count"]} for day in data["days"]]}]}},
        "repositories": {"nodes": nodes or [], "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}}}}


def repository(name, stars):
    return {"nameWithOwner": "Xuefeng-Zhu/" + name, "isPrivate": False, "isFork": False,
            "owner": {"login": "Xuefeng-Zhu"}, "stargazerCount": stars}


def bundle_at(path):
    path.mkdir()
    for name, content in assets.render(snapshot()).items():
        (path / name).write_text(content, encoding="utf-8")
    for name in ("github-snake.svg", "github-snake-dark.svg"):
        (path / name).write_text(SNAKE, encoding="utf-8")
    assets.make_static_snakes(path)
    assets.validate_bundle(path)
    return path


class FakeGitHub(assets.GitHub):
    def __init__(self, responses):
        super().__init__("test-token-never-displayed")
        self.responses = list(responses)

    def request(self, endpoint, body=None):
        return self.responses.pop(0)


class CalendarTests(unittest.TestCase):
    def test_empty_calendar_is_an_explicit_zero_year(self):
        start, end = assets.date_window(TODAY)
        days = assets.normalize_calendar([], start, end)
        self.assertEqual(len(days), 365)
        self.assertTrue(all(day["count"] == 0 for day in days))

    def test_rolling_year_handles_february_29(self):
        self.assertEqual(assets.date_window(dt.date(2024, 2, 29)),
                         (dt.date(2023, 3, 1), dt.date(2024, 2, 29)))
        self.assertEqual(assets.date_window(dt.date(2025, 2, 28)),
                         (dt.date(2024, 2, 29), dt.date(2025, 2, 28)))

    def test_week_padding_is_filtered_and_order_is_chronological(self):
        start, end = dt.date(2026, 1, 1), dt.date(2026, 1, 2)
        days = assets.normalize_calendar([{"contributionDays": [
            {"date": "2026-01-03", "contributionCount": 99},
            {"date": "2026-01-02", "contributionCount": 2},
            {"date": "2025-12-31", "contributionCount": 99},
            {"date": "2026-01-01", "contributionCount": 1}]}], start, end)
        self.assertEqual(days, [{"date": "2026-01-01", "count": 1}, {"date": "2026-01-02", "count": 2}])

    def test_partial_calendar_fails_instead_of_filling_zero_days(self):
        with self.assertRaisesRegex(assets.DataError, "missing dates"):
            assets.normalize_calendar([{"contributionDays": [{"date": "2026-01-01", "contributionCount": 1}]}],
                                      dt.date(2026, 1, 1), dt.date(2026, 1, 2))

    def test_invalid_duplicate_and_negative_days_fail(self):
        for entries in ([{"date": "2026-01-01", "contributionCount": -1}],
                        [{"date": "2026-99-01", "contributionCount": 0}],
                        [{"date": "2026-01-01", "contributionCount": 0}] * 2):
            with self.subTest(entries=entries), self.assertRaises(assets.DataError):
                assets.normalize_calendar([{"contributionDays": entries}], dt.date(2026, 1, 1), dt.date(2026, 1, 1))


class APIAndGenerationTests(unittest.TestCase):
    def test_repositories_include_rest_public_forks_but_stars_paginate_owned_nonforks(self):
        client = FakeGitHub([{"login": "Xuefeng-Zhu", "followers": 7, "public_repos": 101},
                             response([repository("a", 2)], True, "next"), response([repository("b", 5)])])
        result = client.snapshot("Xuefeng-Zhu", TODAY)
        self.assertEqual((result["followers"], result["public_repos"], result["project_stars"]), (7, 101, 7))

    def test_repeated_cursor_or_repository_fails(self):
        for pages in ([response([], True, "same"), response([], True, "same")],
                      [response([repository("a", 1)], True, "next"), response([repository("a", 1)])]):
            with self.subTest(pages=pages), self.assertRaises(assets.DataError):
                FakeGitHub([{"login": "Xuefeng-Zhu", "followers": 0, "public_repos": 0}, *pages]).snapshot("Xuefeng-Zhu", TODAY)

    def test_nonpublic_repository_is_rejected(self):
        repo = repository("a", 3)
        repo["isPrivate"] = True
        with self.assertRaises(assets.DataError):
            FakeGitHub([{"login": "Xuefeng-Zhu", "followers": 0, "public_repos": 0}, response([repo])]).snapshot("Xuefeng-Zhu", TODAY)

    def test_bad_or_unsuccessful_api_responses_fail(self):
        for payload in ({"errors": [{"message": "denied"}]}, {"data": {"user": None}}):
            with self.subTest(payload=payload):
                with patch("urllib.request.urlopen") as opening:
                    opening.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                    if payload.get("errors"):
                        with self.assertRaises(assets.DataError):
                            assets.GitHub("test").request("graphql", {})
                if not payload.get("errors"):
                    with self.assertRaises(assets.DataError):
                        FakeGitHub([{"login": "Xuefeng-Zhu", "followers": 0, "public_repos": 0}, payload]).snapshot("Xuefeng-Zhu", TODAY)

    def test_network_failure_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "published"
            output.mkdir()
            sentinel = output / "previous.svg"
            sentinel.write_text("previous complete bundle", encoding="utf-8")
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
                with self.assertRaises(assets.DataError):
                    assets.generate("Xuefeng-Zhu", output, assets.GitHub("test"), TODAY)
            self.assertEqual(sentinel.read_text(), "previous complete bundle")
            self.assertEqual(list(output.iterdir()), [sentinel])

    def test_zero_snapshot_renders_without_nan_and_writes_validated_output(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeGitHub([{"login": "Xuefeng-Zhu", "followers": 0, "public_repos": 0}, response()])
            output = Path(directory) / "bundle"
            result = assets.generate("Xuefeng-Zhu", output, client, TODAY)
            self.assertEqual(result["generated_date"], "2026-10-08")
            assets.validate_bundle(output, require_snake=False)
            self.assertIn("No visible contributions", (output / "activity-dark.svg").read_text())
            self.assertNotIn("NaN", (output / "activity-dark.svg").read_text())

    def test_svg_text_is_escaped(self):
        data = snapshot()
        data["username"] = 'A<&>"'
        graphic = assets.stats(data, "dark")
        root = ET.fromstring(graphic)
        self.assertEqual(root.find("{http://www.w3.org/2000/svg}title").text, 'A<&>" GitHub statistics')


class BundleTests(unittest.TestCase):
    def test_complete_bundle_and_reduced_motion_preserve_initial_colors_and_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = bundle_at(Path(directory) / "bundle")
            static = (bundle / "github-snake-static.svg").read_text()
            self.assertNotIn("@keyframes", static)
            self.assertNotIn("animation:", static)
            self.assertNotIn("animation-name:", static)
            self.assertIn("fill:var(--c1)", static)
            self.assertIn("transform:translate(0px,16px)", static)
            self.assertIn("Static contribution calendar", static)

    def test_static_snake_removes_smil_as_well(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = bundle_at(Path(directory) / "bundle")
            source = bundle / "github-snake.svg"
            source.write_text(SNAKE.replace("</svg>", '<animate attributeName="x" from="0" to="10" dur="1s"/></svg>'))
            assets.make_static_snakes(bundle)
            self.assertNotIn("<animate", (bundle / "github-snake-static.svg").read_text())

    def test_incomplete_or_extra_bundle_files_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = bundle_at(Path(directory) / "bundle")
            (bundle / "surprise.txt").write_text("unmanaged")
            with self.assertRaises(assets.DataError):
                assets.validate_bundle(bundle)
            (bundle / "surprise.txt").unlink()
            (bundle / "stats-dark.svg").unlink()
            with self.assertRaises(assets.DataError):
                assets.validate_bundle(bundle)

    def test_svg_external_resources_or_scripts_are_rejected(self):
        for fragment in ('<script>alert(1)</script>', '<image href="https://example.com/x.png"/>',
                         '<rect onclick="x()"/>', '<style>.c{fill:url(https://example.com/a)}</style>'):
            with self.subTest(fragment=fragment), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bad.svg"
                path.write_text(SNAKE.replace("</svg>", fragment + "</svg>"))
                with self.assertRaises(assets.DataError):
                    assets.validate_svg(path)

    def test_mismatched_totals_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = bundle_at(Path(directory) / "bundle")
            data = snapshot()
            data["visible_contributions"] = 1
            (bundle / "profile-data.json").write_text(json.dumps(data))
            with self.assertRaises(assets.DataError):
                assets.validate_bundle(bundle)


class PublicationTests(unittest.TestCase):
    def test_feature_branches_cannot_publish(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GITHUB_REF": "refs/heads/feature"}):
            bundle = bundle_at(Path(directory) / "bundle")
            with patch.object(publisher.subprocess, "run") as git, self.assertRaisesRegex(RuntimeError, "Only runs from main"):
                publisher.publish(Path(directory), bundle)
            git.assert_not_called()

    def test_incomplete_bundle_never_reaches_git(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle"
            bundle.mkdir()
            with patch.object(publisher.subprocess, "run") as git, self.assertRaises(assets.DataError):
                publisher.publish(Path(directory), bundle)
            git.assert_not_called()

    def test_initializer_is_blocked_inside_github_actions(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            bundle = bundle_at(Path(directory) / "bundle")
            with patch.object(publisher.subprocess, "run") as git, self.assertRaisesRegex(RuntimeError, "outside GitHub Actions"):
                publisher.publish(Path(directory), bundle, initialize=True)
            git.assert_not_called()

    def test_local_git_publication_is_complete_preserves_source_and_skips_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GITHUB_REF": "refs/heads/main"}):
            base = Path(directory)
            remote, source, bundle = base / "remote.git", base / "source", bundle_at(base / "bundle")

            def git(*args, cwd=None):
                return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()

            git("init", "--bare", str(remote))
            git("init", "-b", "main", str(source))
            git("config", "user.name", "Test", cwd=source)
            git("config", "user.email", "test@example.invalid", cwd=source)
            (source / "README.md").write_text("source stays intact\n")
            git("add", "README.md", cwd=source)
            git("commit", "-m", "Initial source", cwd=source)
            git("remote", "add", "origin", str(remote), cwd=source)
            git("push", "origin", "main", cwd=source)
            before = git("rev-parse", "HEAD", cwd=source)
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "", "GITHUB_REF": "refs/heads/feature"}):
                self.assertTrue(publisher.publish(source, bundle, initialize=True))
            first = git("rev-parse", "refs/heads/profile-assets", cwd=remote)
            tree = git("ls-tree", "--name-only", first, cwd=remote).splitlines()
            self.assertEqual(set(tree), set(assets.BUNDLE_SVGS) | {"profile-data.json"})
            with patch.dict(os.environ, {"GITHUB_ACTIONS": ""}):
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    publisher.publish(source, bundle, initialize=True)
            self.assertFalse(publisher.publish(source, bundle))
            self.assertEqual(first, git("rev-parse", "refs/heads/profile-assets", cwd=remote))
            self.assertEqual(before, git("rev-parse", "HEAD", cwd=source))
            self.assertEqual(git("status", "--porcelain", cwd=source), "")
            self.assertEqual((source / "README.md").read_text(), "source stays intact\n")
            (bundle / "activity-dark.svg").unlink()
            with self.assertRaises(assets.DataError):
                publisher.publish(source, bundle)
            self.assertEqual(first, git("rev-parse", "refs/heads/profile-assets", cwd=remote))


if __name__ == "__main__":
    unittest.main()
