#!/usr/bin/env python3
"""Local GraphQL proxy that adds archived contributions to the 3D contribution chart.

github-profile-3d-contrib reads a single account from GitHub, and the previous account's
history can no longer be fetched. data/contributions-archive.json keeps that history and
this proxy adds it to the live response; point the action's GITHUB_ENDPOINT here to use it.
Archived days leave the chart as they leave GitHub's rolling one-year window.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from statistics import quantiles
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = "https://api.github.com/graphql"
GITLAB_CALENDAR = "https://gitlab.com/users/mekhrojmajidov/calendar.json"
LEVELS = ("NONE", "FIRST_QUARTILE", "SECOND_QUARTILE", "THIRD_QUARTILE", "FOURTH_QUARTILE")
TOTAL_FIELDS = {
    "totalCommitContributions": "commits",
    "totalIssueContributions": "issues",
    "totalPullRequestContributions": "pullRequests",
    "totalPullRequestReviewContributions": "reviews",
    "totalRepositoryContributions": "repositories",
}


def level_step(active):
    # Equal steps up to the 99th percentile of active days: GitHub does not publish its rule,
    # but this reproduces every day of the archived chart.
    cap = quantiles(active, n=100, method="inclusive")[98] if len(active) > 1 else sum(active)
    return cap / 4


def level(count, step):
    if not count:
        return LEVELS[0]
    return LEVELS[min(4, 1 + sum(count > step * multiple for multiple in (1, 2, 3)))]


def merge(payload, archive, gitlab=None):
    """Add the archive to a first-page response; any other response is returned unchanged."""
    user = (payload.get("data") or {}).get("user") or {}
    collection = user.get("contributionsCollection")
    if not collection:
        return payload

    archived_days = archive["calendar"]
    calendar = collection["contributionCalendar"]
    days = [day for week in calendar["weeks"] for day in week["contributionDays"]]
    in_window = 0
    for day in days:
        date = day["date"][:10]
        archived = archived_days.get(date, 0)
        in_window += archived
        # Backfilled days cover the move between accounts: they count on the calendar only,
        # because the archive's yearly totals and languages were recorded without them.
        day["contributionCount"] += archived + archive["backfill"].get(date, 0)
        # The same work can appear on both sites, so a day takes the higher count, not the sum.
        if date >= archive["gitlabFrom"]:
            day["contributionCount"] = max(day["contributionCount"], (gitlab or {}).get(date, 0))
    step = level_step([day["contributionCount"] for day in days if day["contributionCount"]])
    for day in days:
        day["contributionLevel"] = level(day["contributionCount"], step)
    calendar["totalContributions"] = sum(day["contributionCount"] for day in days)

    # The archive has yearly totals only, so they shrink in step with its days leaving the window.
    share = in_window / (sum(archived_days.values()) or 1)
    for field, key in TOTAL_FIELDS.items():
        collection[field] += round(archive["totals"][key] * share)
    collection["commitContributionsByRepository"] += [
        {"contributions": {"totalCount": round(language["count"] * share)},
         "repository": {"primaryLanguage": {"name": language["name"], "color": language["color"]}}}
        for language in archive["languages"]
    ]
    user["repositories"]["nodes"].append({"forkCount": archive["forks"], "stargazerCount": archive["stars"]})
    return payload


def gitlab_days():
    # GitLab only adds to the chart: when it is unavailable, render from GitHub alone.
    try:
        with urlopen(Request(GITLAB_CALENDAR, headers={"User-Agent": "mehrojmajidov-profile"}), timeout=30) as response:
            days = json.load(response)
    except (OSError, ValueError):
        return {}
    if not isinstance(days, dict):
        return {}
    return {date: count for date, count in days.items() if isinstance(count, int)}


def fetch(body, token):
    headers = {"User-Agent": "mehrojmajidov-profile", "Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    with urlopen(Request(UPSTREAM, body, headers), timeout=60) as response:
        return json.load(response)


def handler(archive, token):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.respond(200 if self.path == "/health" else 404, {})

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                self.respond(200, merge(fetch(body, token), archive, gitlab_days()))
            except (HTTPError, URLError, ValueError, KeyError, TypeError):
                # A failed render keeps the published chart. Details stay out of the public logs.
                self.respond(502, {"errors": [{"message": "Contribution data unavailable"}]})

        def log_message(self, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--archive", type=Path, default=ROOT / "data" / "contributions-archive.json")
    args = parser.parse_args()
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        parser.error("Set GITHUB_TOKEN; the GraphQL API requires a token")
    archive = json.loads(args.archive.read_text(encoding="utf-8"))
    ThreadingHTTPServer(("127.0.0.1", args.port), handler(archive, token)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
