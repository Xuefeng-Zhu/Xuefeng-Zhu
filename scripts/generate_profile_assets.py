#!/usr/bin/env python3
"""Generate self-hosted profile graphics from GitHub's visible account data."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


GENERATED_SVGS = (
    "followers-badge.svg", "repos-badge.svg", "stats-light.svg", "stats-dark.svg",
    "activity-light.svg", "activity-dark.svg",
)
BUNDLE_SVGS = GENERATED_SVGS + (
    "github-snake.svg", "github-snake-dark.svg", "github-snake-static.svg", "github-snake-dark-static.svg",
)
PALETTES = {
    "dark": {"bg": "#0a1020", "panel": "#101a30", "line": "#263754", "text": "#edf4ff",
             "muted": "#9dafcb", "cyan": "#67e8f9", "violet": "#bba4ff", "empty": "#182640"},
    "light": {"bg": "#f5f8ff", "panel": "#ffffff", "line": "#cdd7ea", "text": "#152344",
              "muted": "#4c607f", "cyan": "#087e97", "violet": "#7254bd", "empty": "#e1e8f4"},
}


class DataError(ValueError):
    """The API did not return a complete, valid public-data snapshot."""


def nonnegative(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise DataError(f"Invalid {label}: expected a nonnegative integer")
    return value


def date_window(today: dt.date) -> tuple[dt.date, dt.date]:
    """A rolling calendar year, inclusive of today (leap years remain correct)."""
    try:
        anniversary = today.replace(year=today.year - 1)
    except ValueError:  # February 29 follows February 28 in the preceding year.
        anniversary = today.replace(year=today.year - 1, day=28)
    return anniversary + dt.timedelta(days=1), today


class GitHub:
    def __init__(self, token: str):
        if not token:
            raise DataError("GH_TOKEN is required; no unauthenticated fallback is used")
        self.token = token

    def request(self, endpoint: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            "https://api.github.com/" + endpoint,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json", "User-Agent": "self-hosted-profile-assets"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise DataError(f"GitHub request failed with HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise DataError(f"GitHub request failed ({type(error).__name__})") from None
        if not isinstance(result, dict) or result.get("errors"):
            raise DataError("GitHub returned an incomplete or unsuccessful response")
        return result

    def snapshot(self, username: str, today: dt.date) -> dict:
        profile = self.request("users/" + username)
        if str(profile.get("login", "")).lower() != username.lower():
            raise DataError("GitHub returned a different user")
        followers = nonnegative(profile.get("followers"), "follower count")
        public_repos = nonnegative(profile.get("public_repos"), "public repository count")
        start, end = date_window(today)
        variables = {"login": username, "from": f"{start}T00:00:00Z",
                     "to": f"{end}T23:59:59Z", "cursor": None}
        query = """query($login: String!, $from: DateTime!, $to: DateTime!, $cursor: String) {
          user(login: $login) {
            login
            contributionsCollection(from: $from, to: $to) {
              contributionCalendar { weeks { contributionDays { date contributionCount } } }
            }
            repositories(first: 100, after: $cursor, privacy: PUBLIC, isFork: false,
                         ownerAffiliations: OWNER, orderBy: {field: NAME, direction: ASC}) {
              nodes { nameWithOwner isPrivate isFork owner { login } stargazerCount }
              pageInfo { hasNextPage endCursor }
            }
          }
        }"""
        stars, seen_repos, seen_cursors, calendar = 0, set(), set(), None
        while True:
            response = self.request("graphql", {"query": query, "variables": variables})
            try:
                user = response["data"]["user"]
                if user["login"].lower() != username.lower():
                    raise DataError("GraphQL returned a different user")
                if calendar is None:
                    calendar = user["contributionsCollection"]["contributionCalendar"]["weeks"]
                repositories = user["repositories"]
                nodes, page = repositories["nodes"], repositories["pageInfo"]
                if not isinstance(nodes, list) or type(page["hasNextPage"]) is not bool:
                    raise DataError("Invalid repository pagination")
                for repo in nodes:
                    name = repo["nameWithOwner"]
                    if (not isinstance(name, str) or name in seen_repos or
                            repo["isPrivate"] is not False or repo["isFork"] is not False or
                            repo["owner"]["login"].lower() != username.lower()):
                        raise DataError("Unexpected repository in owned-public-project query")
                    seen_repos.add(name)
                    stars += nonnegative(repo["stargazerCount"], "repository stars")
                if not page["hasNextPage"]:
                    break
                cursor = page["endCursor"]
                if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                    raise DataError("Invalid or repeated repository cursor")
                seen_cursors.add(cursor)
                variables["cursor"] = cursor
            except (KeyError, TypeError, AttributeError):
                raise DataError("GitHub omitted required profile fields") from None
        days = normalize_calendar(calendar, start, end)
        return {"username": username, "generated_date": str(today), "window_start": str(start),
                "window_end": str(end), "followers": followers, "public_repos": public_repos,
                "project_stars": stars, "visible_contributions": sum(day["count"] for day in days),
                "days": days}


def normalize_calendar(weeks: Any, start: dt.date, end: dt.date) -> list[dict]:
    if not isinstance(weeks, list):
        raise DataError("Invalid contribution calendar")
    counts: dict[dt.date, int] = {}
    try:
        for week in weeks:
            if not isinstance(week["contributionDays"], list):
                raise DataError("Invalid contribution days")
            for day in week["contributionDays"]:
                date = dt.date.fromisoformat(day["date"])
                count = nonnegative(day["contributionCount"], "daily contribution count")
                if date in counts:
                    raise DataError("Duplicate contribution date")
                counts[date] = count
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, DataError):
            raise
        raise DataError("Invalid contribution date or shape") from None
    # Even zero-activity accounts return a complete range of zero-valued date cells.
    # Missing API dates must never become fabricated zero activity.
    dates = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    if any(date not in counts for date in dates):
        raise DataError("Contribution calendar is missing dates in the requested window")
    return [{"date": str(date), "count": counts[date]} for date in dates]


def svg_document(width: int, height: int, title: str, description: str, content: str) -> str:
    escape = html.escape
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">'
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>'
            '<style>text{font-family:Menlo,Consolas,Liberation Mono,monospace}</style>'
            + content + '</svg>\n')


def text(x: float, y: float, value: str, color: str, size: int = 14, extra: str = "") -> str:
    return (f'<text x="{x:g}" y="{y:g}" fill="{color}" font-size="{size}" {extra}>'
            f'{html.escape(value)}</text>')


def badge(label: str, value: int, accent: str, generated: str) -> str:
    left, right, height = 18 + len(label) * 7, max(40, len(str(value)) * 8 + 22), 30
    width = left + right
    content = (f'<rect x=".5" y=".5" width="{width - 1}" height="29" rx="7" fill="#111a30" stroke="#344261"/>'
               f'<path d="M{left} 1v28" stroke="#344261"/>'
               + text(left / 2, 20, label, "#e5edff", 12, 'text-anchor="middle"')
               + text(left + right / 2, 20, str(value), accent, 12, 'text-anchor="middle" font-weight="700"'))
    return svg_document(width, height, f"{value:,} {label}", f"GitHub data refreshed {generated} UTC.", content)


def stats(snapshot: dict, theme: str) -> str:
    p = PALETTES[theme]
    content = (f'<rect x=".5" y=".5" width="599" height="399" rx="18" fill="{p["bg"]}" stroke="{p["line"]}"/>'
               + text(24, 35, "GITHUB / AT A GLANCE", p["cyan"], 17, 'letter-spacing="1"'))
    metrics = [("Public repos", snapshot["public_repos"]), ("Project stars", snapshot["project_stars"]),
               ("Followers", snapshot["followers"]), ("Contributions", snapshot["visible_contributions"])]
    for i, (label, number) in enumerate(metrics):
        x, y = 24 + (i % 2) * 284, 58 + (i // 2) * 132
        content += (f'<rect x="{x}" y="{y}" width="268" height="116" rx="12" fill="{p["panel"]}" stroke="{p["line"]}"/>'
                    + text(x + 20, y + 54, f"{number:,}", p["cyan"] if i % 2 == 0 else p["violet"], 44, 'font-weight="700"')
                    + text(x + 20, y + 91, label, p["muted"], 22))
    content += text(24, 334, "Stars: owned public projects, excluding forks.", p["muted"], 13)
    content += text(24, 356, "Visible contributions: " + snapshot["window_start"] + " — " + snapshot["window_end"], p["muted"], 13)
    content += text(24, 382, "REFRESHED " + snapshot["generated_date"] + " UTC", p["muted"], 12)
    return svg_document(600, 400, f'{snapshot["username"]} GitHub statistics',
                        "Public repositories include forks. Stars exclude forks. Contributions are publicly visible profile calendar counts, "
                        f'from {snapshot["window_start"]} through {snapshot["window_end"]}.', content)


def activity(snapshot: dict, theme: str) -> str:
    p = PALETTES[theme]
    width, height, left, top, chart_width, chart_height = 900, 270, 44, 82, 812, 125
    days = snapshot["days"]
    maximum = max((day["count"] for day in days), default=0)
    scale_max = max(1, maximum)
    content = (f'<rect x=".5" y=".5" width="899" height="269" rx="18" fill="{p["bg"]}" stroke="{p["line"]}"/>'
               '<defs><linearGradient id="area" x1="0" y1="0" x2="0" y2="1">'
               f'<stop stop-color="{p["cyan"]}" stop-opacity=".3"/><stop offset="1" stop-color="{p["violet"]}" stop-opacity=".02"/>'
               '</linearGradient></defs>'
               + text(28, 35, "CONTRIBUTION SIGNAL", p["cyan"], 14, 'letter-spacing="2"')
               + text(28, 58, f'{snapshot["visible_contributions"]:,} visible contributions · {snapshot["window_start"]} — {snapshot["window_end"]}', p["muted"], 12))
    for fraction in (0, .5, 1):
        y = top + chart_height * (1 - fraction)
        content += (f'<path d="M{left} {y:g}H{left + chart_width}" stroke="{p["line"]}" stroke-dasharray="3 6"/>'
                    + text(left - 10, y + 4, str(round(scale_max * fraction)), p["muted"], 10, 'text-anchor="end"'))
    points = [(left + i * chart_width / max(1, len(days) - 1), top + chart_height * (1 - day["count"] / scale_max))
              for i, day in enumerate(days)]
    line = " ".join(("M" if i == 0 else "L") + f"{x:.2f} {y:.2f}" for i, (x, y) in enumerate(points))
    content += (f'<path d="{line} L{left + chart_width} {top + chart_height} L{left} {top + chart_height}Z" fill="url(#area)"/>'
                f'<path d="{line}" fill="none" stroke="{p["cyan"]}" stroke-width="1.8" stroke-linejoin="round"/>')
    previous_label_x = -100.0
    for i, day in enumerate(days):
        date = dt.date.fromisoformat(day["date"])
        x = left + i * chart_width / max(1, len(days) - 1)
        if (i == 0 or date.day == 1) and x - previous_label_x >= 45:
            content += text(x, 230, date.strftime("%b"), p["muted"], 10)
            previous_label_x = x
    if maximum == 0:
        content += text(450, 150, "No visible contributions in this period", p["muted"], 14, 'text-anchor="middle"')
    content += text(872, 254, "REFRESHED " + snapshot["generated_date"] + " UTC", p["muted"], 10, 'text-anchor="end"')
    return svg_document(width, height, "Daily GitHub contribution activity",
                        "Chronological daily counts from GitHub's publicly visible contribution calendar. "
                        "Anonymous private activity may appear if enabled on the GitHub profile. No private repository details are requested.", content)


def render(snapshot: dict) -> dict[str, str]:
    outputs = {"followers-badge.svg": badge("Followers", snapshot["followers"], "#67e8f9", snapshot["generated_date"]),
               "repos-badge.svg": badge("Public repos", snapshot["public_repos"], "#bba4ff", snapshot["generated_date"])}
    for theme in PALETTES:
        outputs[f"stats-{theme}.svg"] = stats(snapshot, theme)
        outputs[f"activity-{theme}.svg"] = activity(snapshot, theme)
    outputs["profile-data.json"] = json.dumps(snapshot, indent=2, ensure_ascii=True) + "\n"
    return outputs


def validate_svg(path: Path) -> None:
    if path.stat().st_size < 100 or path.stat().st_size > 3_000_000:
        raise DataError(f"Unexpected SVG size: {path.name}")
    source = path.read_text(encoding="utf-8")
    if re.search(r"<!DOCTYPE|<!ENTITY|@import", source, re.I):
        raise DataError(f"Unsafe external content in {path.name}")
    try:
        root = ET.fromstring(source)
    except ET.ParseError:
        raise DataError(f"Malformed SVG: {path.name}") from None
    if root.tag != "{http://www.w3.org/2000/svg}svg" or not root.get("viewBox"):
        raise DataError(f"Invalid SVG root: {path.name}")
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] in {"script", "foreignObject", "iframe", "image"}:
            raise DataError(f"Unsupported SVG content: {path.name}")
        for attribute, value in element.attrib.items():
            name = attribute.rsplit("}", 1)[-1].lower()
            if name.startswith("on") or (name == "href" and not value.startswith("#")):
                raise DataError(f"Unsafe SVG reference: {path.name}")
    if re.search(r"url\(\s*['\"]?(?!#)[^)]+\)", source, re.I):
        raise DataError(f"External SVG resource: {path.name}")


def validate_bundle(path: Path, require_snake: bool = True) -> None:
    expected = set(BUNDLE_SVGS if require_snake else GENERATED_SVGS) | {"profile-data.json"}
    actual = {file.name for file in path.iterdir()}
    if actual != expected or any(not (path / name).is_file() or (path / name).is_symlink() for name in expected):
        raise DataError("The asset bundle is incomplete or contains unexpected files")
    for name in expected - {"profile-data.json"}:
        validate_svg(path / name)
    try:
        snapshot = json.loads((path / "profile-data.json").read_text(encoding="utf-8"))
        start, end = dt.date.fromisoformat(snapshot["window_start"]), dt.date.fromisoformat(snapshot["window_end"])
        if date_window(end) != (start, end) or dt.date.fromisoformat(snapshot["generated_date"]) != end:
            raise DataError("Invalid asset date range")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", snapshot["username"]):
            raise DataError("Invalid GitHub username")
        for key in ("followers", "public_repos", "project_stars", "visible_contributions"):
            nonnegative(snapshot[key], key)
        expected_days = normalize_calendar([{"contributionDays": [
            {"date": day["date"], "contributionCount": day["count"]} for day in snapshot["days"]]}], start, end)
        if snapshot["days"] != expected_days or sum(day["count"] for day in expected_days) != snapshot["visible_contributions"]:
            raise DataError("Calendar totals or ordering do not match")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, DataError):
            raise
        raise DataError("Malformed profile data") from None


def strip_keyframes(css: str) -> str:
    """Remove balanced CSS keyframe blocks, retaining the pinned snake's initial state."""
    pattern = re.compile(r"@(?:-webkit-)?keyframes\s+[^\{]+\{", re.I)
    while match := pattern.search(css):
        depth, cursor = 1, match.end()
        while cursor < len(css) and depth:
            depth += (css[cursor] == "{") - (css[cursor] == "}")
            cursor += 1
        if depth:
            raise DataError("Unbalanced snake animation CSS")
        css = css[:match.start()] + css[cursor:]
    return re.sub(r"(?<![\w-])(?:-webkit-)?animation(?:-[\w-]+)?\s*:[^;}]*;?", "", css, flags=re.I)


def make_static_snakes(bundle: Path) -> None:
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    for name in ("github-snake.svg", "github-snake-dark.svg"):
        source = bundle / name
        validate_svg(source)
        root = ET.fromstring(source.read_text(encoding="utf-8"))
        for parent in root.iter():
            for child in list(parent):
                local_name = child.tag.rsplit("}", 1)[-1]
                if local_name in {"animate", "animateTransform", "animateMotion", "set"}:
                    parent.remove(child)
                elif local_name == "style":
                    child.text = strip_keyframes(child.text or "")
            if "style" in parent.attrib:
                parent.attrib["style"] = strip_keyframes(parent.attrib["style"])
        title = ET.Element("{http://www.w3.org/2000/svg}title")
        title.text = "Static contribution calendar and snake (reduced motion)"
        root.insert(0, title)
        destination = bundle / name.replace(".svg", "-static.svg")
        destination.write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")
        validate_svg(destination)


def generate(username: str, output: Path, client: GitHub, today: dt.date) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", username):
        raise DataError("Invalid GitHub username")
    snapshot = client.snapshot(username, today)  # Fetch and validate before touching prior output.
    outputs = render(snapshot)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".profile-assets-", dir=output.parent) as temporary:
        staged = Path(temporary) / "bundle"
        staged.mkdir()
        for name, content in outputs.items():
            (staged / name).write_text(content, encoding="utf-8")
        validate_bundle(staged, require_snake=False)
        previous = Path(temporary) / "previous"
        if output.exists():
            output.rename(previous)
        try:
            staged.rename(output)
        except OSError:
            if previous.exists():
                previous.rename(output)
            raise
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "Xuefeng-Zhu"))
    parser.add_argument("--output", type=Path, default=Path("build/profile-assets"))
    parser.add_argument("--validate-bundle", type=Path)
    parser.add_argument("--make-static-snakes", type=Path)
    args = parser.parse_args()
    try:
        if args.make_static_snakes:
            make_static_snakes(args.make_static_snakes)
            print("Generated two reduced-motion snake variants.")
        elif args.validate_bundle:
            validate_bundle(args.validate_bundle)
            print("Validated complete profile asset bundle.")
        else:
            snapshot = generate(args.username, args.output, GitHub(os.environ.get("GH_TOKEN", "")),
                                dt.datetime.now(dt.timezone.utc).date())
            print(f'Generated six profile graphics from GitHub data refreshed {snapshot["generated_date"]} UTC.')
    except (DataError, OSError) as error:
        print(f"Profile asset generation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
