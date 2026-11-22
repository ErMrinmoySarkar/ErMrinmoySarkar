#!/usr/bin/env python3
"""Generate live GitHub profile assets from the GitHub GraphQL API.

Outputs (written to OUTPUT_DIR, default "dist"):

    github-stats.svg      totals: contributions, commits, PRs, issues, stars, repos
    github-streak.svg     total contributions, current streak, longest streak
    github-languages.svg  top languages by code size across public, non-fork repos
    github-fish.svg       contribution graph with an animated fish swimming through it
    profile-stats.json    the raw numbers used, for debugging

Nothing is hardcoded: every number comes from the account named in PROFILE_USER.

Environment:
    GITHUB_TOKEN   token for api.github.com (the workflow's built-in GITHUB_TOKEN is enough)
    PROFILE_USER   GitHub login to read (the workflow passes the repository owner)
    OUTPUT_DIR     where to write files (default: dist)

Only the Python standard library is used.

Note on scope: with the built-in GITHUB_TOKEN, GitHub reports public contribution
activity. Private contributions are only included when the account has enabled
"Include private contributions on my profile" and the token can see them.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

GRAPHQL_URL = "https://api.github.com/graphql"

# Palette: navy background, blue/cyan accents, subtle violet.
BG = "#0B1020"
BORDER = "#1e293b"
TITLE = "#38bdf8"
TEXT = "#cbd5e1"
STRONG = "#e2e8f0"
MUTED = "#64748b"
ACCENT = "#818cf8"
GLOW = "#22d3ee"
LEVEL_COLORS = ["#121c33", "#0c4a6e", "#0369a1", "#0ea5e9", "#38bdf8"]
LEVELS = {
    "NONE": 0,
    "FIRST_QUARTILE": 1,
    "SECOND_QUARTILE": 2,
    "THIRD_QUARTILE": 3,
    "FOURTH_QUARTILE": 4,
}
FONT = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #

def graphql(token: str, query: str, variables: dict) -> dict:
    """POST a GraphQL query. Retries transient failures, fails fast on real errors."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    headers = {
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "profile-assets-generator",
    }
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(GRAPHQL_URL, data=body, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                detail = exc.read().decode(errors="replace")[:300]
                raise RuntimeError(f"GitHub API returned HTTP {exc.code}: {detail}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        else:
            if payload.get("errors"):
                raise RuntimeError(f"GraphQL errors: {json.dumps(payload['errors'])[:500]}")
            return payload["data"]
        time.sleep(2 ** attempt)
    raise RuntimeError(f"GitHub API unreachable after retries: {last_error}")


PROFILE_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    login
    name
    createdAt
    pullRequests { totalCount }
    issues { totalCount }
    publicRepos: repositories(ownerAffiliations: OWNER, privacy: PUBLIC) { totalCount }
    repositories(ownerAffiliations: OWNER, privacy: PUBLIC, isFork: false, first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        stargazerCount
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

YEAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""

LAST_YEAR_QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays { date contributionCount contributionLevel weekday }
        }
      }
    }
  }
}
"""


def fetch_profile(token: str, login: str) -> dict:
    """Account facts plus stars and language bytes, following repository pagination."""
    cursor = None
    profile: dict = {}
    stars = 0
    languages: dict[str, int] = {}
    colors: dict[str, str] = {}
    while True:
        user = graphql(token, PROFILE_QUERY, {"login": login, "cursor": cursor})["user"]
        if user is None:
            raise RuntimeError(f"GitHub user '{login}' was not found")
        if not profile:
            profile = {
                "login": user["login"],
                "name": user["name"],
                "createdAt": user["createdAt"],
                "pullRequests": user["pullRequests"]["totalCount"],
                "issues": user["issues"]["totalCount"],
                "publicRepos": user["publicRepos"]["totalCount"],
            }
        repos = user["repositories"]
        for repo in repos["nodes"]:
            stars += repo["stargazerCount"]
            for edge in repo["languages"]["edges"]:
                name = edge["node"]["name"]
                languages[name] = languages.get(name, 0) + edge["size"]
                colors.setdefault(name, edge["node"]["color"] or MUTED)
        if not repos["pageInfo"]["hasNextPage"]:
            break
        cursor = repos["pageInfo"]["endCursor"]
    profile["stars"] = stars
    profile["languages"] = languages
    profile["languageColors"] = colors
    return profile


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def year_ranges(created: datetime, now: datetime):
    """One (start, end) window per calendar year; the API allows at most one year per query."""
    for year in range(created.year, now.year + 1):
        start = max(created, datetime(year, 1, 1, tzinfo=timezone.utc))
        end = min(datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc), now)
        if start <= end:
            yield start, end


def fetch_history(token: str, login: str, created: datetime, now: datetime):
    """All-time daily counts, total commit contributions and total contributions."""
    days: dict[str, int] = {}
    commits = 0
    total = 0
    for start, end in year_ranges(created, now):
        collection = graphql(
            token,
            YEAR_QUERY,
            {
                "login": login,
                "from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )["user"]["contributionsCollection"]
        commits += collection["totalCommitContributions"]
        calendar = collection["contributionCalendar"]
        total += calendar["totalContributions"]
        for week in calendar["weeks"]:
            for day in week["contributionDays"]:
                # max() guards against zero-padded days at window edges overwriting real counts
                days[day["date"]] = max(days.get(day["date"], 0), day["contributionCount"])
    return days, commits, total


def fetch_last_year(token: str, login: str):
    calendar = graphql(token, LAST_YEAR_QUERY, {"login": login})["user"][
        "contributionsCollection"
    ]["contributionCalendar"]
    return calendar["weeks"], calendar["totalContributions"]


# --------------------------------------------------------------------------- #
# Streaks
# --------------------------------------------------------------------------- #

def compute_streaks(days: dict[str, int]):
    """Return ((length, start, end) for longest, same for current) from daily counts."""
    ordered = sorted(days.items())
    longest = (0, None, None)
    run_len = 0
    run_start = None
    previous: date | None = None
    for day, count in ordered:
        current = date.fromisoformat(day)
        if previous is not None and (current - previous).days != 1:
            run_len = 0
        previous = current
        if count > 0:
            if run_len == 0:
                run_start = day
            run_len += 1
            if run_len > longest[0]:
                longest = (run_len, run_start, day)
        else:
            run_len = 0

    current_streak = (0, None, None)
    i = len(ordered) - 1
    if i >= 0 and ordered[i][1] == 0:
        i -= 1  # today has no activity yet; the streak is still alive until the day ends
    if i >= 0 and ordered[i][1] > 0:
        end = ordered[i][0]
        start = end
        length = 0
        later = None
        while i >= 0 and ordered[i][1] > 0:
            this = date.fromisoformat(ordered[i][0])
            if later is not None and (later - this).days != 1:
                break
            length += 1
            start = ordered[i][0]
            later = this
            i -= 1
        current_streak = (length, start, end)
    return longest, current_streak


# --------------------------------------------------------------------------- #
# SVG helpers
# --------------------------------------------------------------------------- #

def fmt(number: int) -> str:
    return f"{number:,}"


def pretty_date(value: str | None) -> str:
    if not value:
        return "-"
    parsed = date.fromisoformat(value)
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"


def svg_open(width: int, height: int, title: str, description: str, css: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d">\n'
        f'<title id="t">{escape(title)}</title>\n<desc id="d">{escape(description)}</desc>\n'
        f"<style>\n{css}\n</style>\n"
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10" '
        f'fill="{BG}" stroke="{BORDER}"/>\n'
    )


CARD_CSS = (
    "text{font-family:__FONT__}\n"
    ".h{font-size:15px;font-weight:700;fill:__TITLE__}\n"
    ".sub{font-size:10px;fill:__MUTED__}\n"
    ".n{font-size:24px;font-weight:700;fill:__STRONG__}\n"
    ".l{font-size:11px;fill:__TEXT__}\n"
    ".big{font-size:30px;font-weight:700;fill:__STRONG__}\n"
    ".d{font-size:10px;fill:__MUTED__}\n"
).replace("__FONT__", FONT).replace("__TITLE__", TITLE).replace("__MUTED__", MUTED) \
 .replace("__STRONG__", STRONG).replace("__TEXT__", TEXT)


def render_stats(display_name: str, values: list[tuple[str, int]]) -> str:
    width, height = 440, 215
    out = svg_open(
        width,
        height,
        f"{display_name} GitHub stats",
        "Total contributions, commits, pull requests, issues, stars earned and public repositories.",
        CARD_CSS,
    )
    out += f'<text class="h" x="24" y="36">{escape(display_name)} / GitHub Stats</text>\n'
    for index, (label, value) in enumerate(values):
        column, row = divmod(index, 3)
        x = 24 + column * 208
        y = 84 + row * 50
        out += f'<text class="n" x="{x}" y="{y}">{fmt(value)}</text>\n'
        out += f'<text class="l" x="{x}" y="{y + 17}">{escape(label)}</text>\n'
    return out + "</svg>\n"


def render_streak(total: int, current, longest) -> str:
    width, height = 440, 215
    out = svg_open(
        width,
        height,
        "GitHub contribution streak",
        "Total contributions, current streak and longest streak in days.",
        CARD_CSS,
    )
    out += '<text class="h" x="24" y="36">Contribution Streak</text>\n'
    def span(streak) -> list[str]:
        if not streak[0]:
            return ["-"]
        if streak[1] == streak[2]:
            return [pretty_date(streak[1])]
        return [f"{pretty_date(streak[1])} -", pretty_date(streak[2])]

    columns = [
        (73, fmt(total), "Contributions", ["all time"]),
        (220, str(current[0]), "Current streak", span(current) if current[0] else ["no active streak"]),
        (367, str(longest[0]), "Longest streak", span(longest)),
    ]
    for index, (cx, number, label, detail_lines) in enumerate(columns):
        if index == 1:
            out += f'<circle cx="{cx}" cy="108" r="40" fill="none" stroke="{TITLE}" stroke-width="3"/>\n'
        out += f'<text class="big" x="{cx}" y="118" text-anchor="middle">{escape(number)}</text>\n'
        out += f'<text class="l" x="{cx}" y="170" text-anchor="middle">{escape(label)}</text>\n'
        for line_no, line in enumerate(detail_lines):
            out += f'<text class="d" x="{cx}" y="{188 + line_no * 13}" text-anchor="middle">{escape(line)}</text>\n'
    return out + "</svg>\n"


def render_languages(languages: dict[str, int], colors: dict[str, str]) -> str | None:
    total = sum(languages.values())
    if total <= 0:
        return None
    ranked = sorted(languages.items(), key=lambda item: -item[1])
    top = ranked[:6]
    rest = total - sum(size for _, size in top)
    entries = [(name, size / total * 100, colors.get(name, MUTED)) for name, size in top]
    if rest > 0:
        entries.append(("Other", rest / total * 100, MUTED))

    width, height, pad = 900, 150, 28
    bar_width, bar_y = width - 2 * pad, 62
    out = svg_open(
        width,
        height,
        "Top languages",
        "Share of code by language across public, non-fork repositories.",
        CARD_CSS,
    )
    out += '<text class="h" x="28" y="36">Top Languages</text>\n'
    out += f'<text class="sub" x="{width - pad}" y="36" text-anchor="end">by code size, public repos</text>\n'
    out += f'<defs><clipPath id="bar"><rect x="{pad}" y="{bar_y}" width="{bar_width}" height="10" rx="5"/></clipPath></defs>\n'
    out += '<g clip-path="url(#bar)">\n'
    x = float(pad)
    for name, percent, color in entries:
        segment = bar_width * percent / 100
        out += f'<rect x="{x:.2f}" y="{bar_y}" width="{segment:.2f}" height="10" fill="{color}"/>\n'
        x += segment
    out += "</g>\n"
    column_width = bar_width / len(entries)
    for index, (name, percent, color) in enumerate(entries):
        lx = pad + index * column_width
        label = name if len(name) <= 13 else name[:12] + "..."
        out += f'<circle cx="{lx + 5:.1f}" cy="106" r="5" fill="{color}"/>\n'
        out += f'<text class="l" x="{lx + 16:.1f}" y="110">{escape(label)}</text>\n'
        out += f'<text class="d" x="{lx + 16:.1f}" y="128">{percent:.1f}%</text>\n'
    return out + "</svg>\n"


# --------------------------------------------------------------------------- #
# Fish contribution graph
# --------------------------------------------------------------------------- #

FISH_CSS = """
text{font-family:__FONT__}
.h{font-size:15px;font-weight:700;fill:__TITLE__}
.sub{font-size:11px;fill:__MUTED__}
.fish{animation:swim __T__s linear infinite}
.tail{transform-box:fill-box;transform-origin:100% 50%;animation:wag .55s ease-in-out infinite alternate}
.fin{transform-box:fill-box;transform-origin:100% 0%;animation:flap .9s ease-in-out infinite alternate}
.gl{fill:__GLOW__;opacity:0;animation:glow __T__s linear infinite}
.gl.s{animation-name:glowsoft}
.b{fill:none;stroke:#67e8f9;stroke-width:1;opacity:0;animation:rise 1.8s ease-out infinite}
@keyframes wag{from{transform:rotate(-16deg)}to{transform:rotate(16deg)}}
@keyframes flap{from{transform:rotate(-10deg)}to{transform:rotate(12deg)}}
@keyframes glow{0%{opacity:0}3%{opacity:.85}14%{opacity:0}100%{opacity:0}}
@keyframes glowsoft{0%{opacity:0}3%{opacity:.35}14%{opacity:0}100%{opacity:0}}
@keyframes rise{0%{opacity:0;transform:translate(0,0)}15%{opacity:.9}100%{opacity:0;transform:translate(-4px,-13px)}}
@media (prefers-reduced-motion:reduce){.fish,.tail,.fin,.gl,.b{animation:none}}
__SWIM__
"""

FISH_SHAPE = f"""<g class="fish" transform="translate(__RX__ __RY__)">
  <path class="tail" d="M-12 0 L-31 -11 C-27 -4 -27 4 -31 11 Z" fill="{ACCENT}" opacity="0.9"/>
  <path d="M-4 -8.5 C-2 -17 6 -16 8 -9 Z" fill="{ACCENT}" opacity="0.9"/>
  <path d="M22 0 C22 -7 10 -11 -2 -9 C-8 -8 -12 -4 -14 -2 L-14 2 C-12 4 -8 8 -2 9 C10 11 22 7 22 0 Z" fill="url(#fg)"/>
  <path d="M9 -6 C12 -2 12 2 9 6" fill="none" stroke="#0b2a5b" stroke-width="1" opacity="0.45"/>
  <path class="fin" d="M4 3 C1 10 -5 10 -7 7 C-3 6 1 5 4 3 Z" fill="{ACCENT}"/>
  <circle cx="15" cy="-2.5" r="2.3" fill="{BG}"/>
  <circle cx="15.6" cy="-3.1" r="0.8" fill="#ffffff"/>
  <circle class="b" cx="25" cy="-4" r="1.6" style="animation-delay:0s"/>
  <circle class="b" cx="28" cy="-8" r="1.2" style="animation-delay:.6s"/>
  <circle class="b" cx="24" cy="-10" r="1.0" style="animation-delay:1.2s"/>
</g>"""


def render_fish(weeks: list, total_last_year: int, updated: str) -> str:
    pitch, cell, pad_x, top = 16, 12, 28, 60
    cols = len(weeks)
    grid_w = cols * pitch - (pitch - cell)
    grid_h = 7 * pitch - (pitch - cell)
    width = max(pad_x * 2 + grid_w, 560)
    height = top + grid_h + 56
    x0 = (width - grid_w) / 2

    cycle = 22.0          # seconds for one full loop
    swim_fraction = 0.92  # share of the loop spent swimming; the rest is off-screen
    x_start, x_end = -50.0, width + 50.0
    y_mid = top + grid_h / 2
    amplitude, wavelength = 38.0, 430.0

    def fish_y(x: float) -> float:
        return y_mid + amplitude * math.sin(2 * math.pi * (x - x_start) / wavelength)

    def fish_angle(x: float) -> float:
        slope = amplitude * 2 * math.pi / wavelength * math.cos(2 * math.pi * (x - x_start) / wavelength)
        return math.degrees(math.atan(slope))

    def time_at(x: float) -> float:
        return (x - x_start) / (x_end - x_start) * swim_fraction * cycle

    # Swim keyframes: the fish follows a sine wave across the grid, then waits off-screen.
    steps = int((x_end - x_start) / 10)
    frames = []
    for i in range(steps + 1):
        x = x_start + i * (x_end - x_start) / steps
        percent = swim_fraction * 100 * i / steps
        extra = ";animation-timing-function:step-end" if i == steps else ""
        frames.append(
            f"{percent:.2f}%{{transform:translate({x:.1f}px,{fish_y(x):.1f}px) rotate({fish_angle(x):.1f}deg){extra}}}"
        )
    first_x = x_start
    frames.append(
        f"100%{{transform:translate({first_x:.1f}px,{fish_y(first_x):.1f}px) rotate({fish_angle(first_x):.1f}deg)}}"
    )
    swim_css = "@keyframes swim{" + "".join(frames) + "}"

    css = (
        FISH_CSS.replace("__FONT__", FONT)
        .replace("__TITLE__", TITLE)
        .replace("__MUTED__", MUTED)
        .replace("__GLOW__", GLOW)
        .replace("__T__", f"{cycle:g}")
        .replace("__SWIM__", swim_css)
    )

    out = svg_open(
        width,
        height,
        "Contribution graph with a swimming fish",
        f"{total_last_year} contributions in the last year. A fish swims across the grid and lights up the cells it passes.",
        css,
    )
    out += (
        '<defs><linearGradient id="fg" x1="1" y1="0" x2="0" y2="0">'
        '<stop offset="0" stop-color="#67e8f9"/><stop offset="0.6" stop-color="#38bdf8"/>'
        '<stop offset="1" stop-color="#818cf8"/></linearGradient></defs>\n'
    )
    out += f'<text class="h" x="{pad_x}" y="34">Contribution Graph</text>\n'
    out += (
        f'<text class="sub" x="{width - pad_x}" y="34" text-anchor="end">'
        f"{fmt(total_last_year)} contributions in the last year</text>\n"
    )

    cells = []
    for col, week in enumerate(weeks):
        for day in week["contributionDays"]:
            level = LEVELS.get(day.get("contributionLevel", "NONE"), 0)
            cells.append((col, day["weekday"], level, day["contributionCount"], day["date"]))

    out += "<g>\n"
    for col, row, level, count, day in cells:
        x = x0 + col * pitch
        y = top + row * pitch
        out += (
            f'<rect x="{x:.1f}" y="{y}" width="{cell}" height="{cell}" rx="2" fill="{LEVEL_COLORS[level]}">'
            f"<title>{count} contributions on {day}</title></rect>\n"
        )
    out += "</g>\n"

    # Glow overlays: cells near the fish's path light up the moment it passes.
    out += "<g>\n"
    for col, row, _level, _count, _day in cells:
        x = x0 + col * pitch
        y = top + row * pitch
        cx, cy = x + cell / 2, y + cell / 2
        distance = abs(cy - fish_y(cx))
        if distance <= pitch * 1.1:
            soft = ""
        elif distance <= pitch * 2.1:
            soft = " s"
        else:
            continue
        delay = time_at(cx) - 0.03 * cycle - 0.2
        out += (
            f'<rect class="gl{soft}" x="{x:.1f}" y="{y}" width="{cell}" height="{cell}" rx="2" '
            f'style="animation-delay:{delay:.2f}s"/>\n'
        )
    out += "</g>\n"

    rest_x = width * 0.35
    fish = FISH_SHAPE.replace("__RX__", f"{rest_x:.1f}").replace("__RY__", f"{fish_y(rest_x):.1f}")
    out += fish + "\n"

    # Footer: freshness note and legend.
    footer_y = top + grid_h + 32
    out += f'<text class="sub" x="{pad_x}" y="{footer_y}">Updated {escape(updated)} UTC from live GitHub data</text>\n'
    legend_right = width - pad_x
    squares_end = legend_right - 27 - 8
    squares_start = squares_end - (5 * 16 - 4)
    out += f'<text class="sub" x="{squares_start - 8}" y="{footer_y}" text-anchor="end">Less</text>\n'
    for i, color in enumerate(LEVEL_COLORS):
        out += f'<rect x="{squares_start + i * 16}" y="{footer_y - 10}" width="12" height="12" rx="2" fill="{color}"/>\n'
    out += f'<text class="sub" x="{legend_right}" y="{footer_y}" text-anchor="end">More</text>\n'
    return out + "</svg>\n"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    login = os.environ.get("PROFILE_USER") or os.environ.get("GITHUB_REPOSITORY_OWNER")
    if not token:
        print("error: set GITHUB_TOKEN", file=sys.stderr)
        return 1
    if not login:
        print("error: set PROFILE_USER (the GitHub login to read)", file=sys.stderr)
        return 1
    out_dir = Path(os.environ.get("OUTPUT_DIR", "dist"))
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    profile = fetch_profile(token, login)
    created = parse_time(profile["createdAt"])
    days, commits, total_contributions = fetch_history(token, login, created, now)
    weeks, total_last_year = fetch_last_year(token, login)
    longest, current = compute_streaks(days)

    display_name = profile["name"] or profile["login"]
    stats = {
        "login": profile["login"],
        "totalContributions": total_contributions,
        "totalCommits": commits,
        "pullRequests": profile["pullRequests"],
        "issues": profile["issues"],
        "starsEarned": profile["stars"],
        "publicRepos": profile["publicRepos"],
        "currentStreak": {"days": current[0], "start": current[1], "end": current[2]},
        "longestStreak": {"days": longest[0], "start": longest[1], "end": longest[2]},
        "contributionsLastYear": total_last_year,
        "languages": profile["languages"],
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (out_dir / "profile-stats.json").write_text(json.dumps(stats, indent=2) + "\n")

    (out_dir / "github-stats.svg").write_text(
        render_stats(
            display_name,
            [
                ("Total contributions", total_contributions),
                ("Total commits", commits),
                ("Pull requests", profile["pullRequests"]),
                ("Issues", profile["issues"]),
                ("Stars earned", profile["stars"]),
                ("Public repos", profile["publicRepos"]),
            ],
        )
    )
    (out_dir / "github-streak.svg").write_text(render_streak(total_contributions, current, longest))
    languages_svg = render_languages(profile["languages"], profile["languageColors"])
    if languages_svg:
        (out_dir / "github-languages.svg").write_text(languages_svg)
    else:
        print("note: no language data yet, skipping github-languages.svg")
    (out_dir / "github-fish.svg").write_text(
        render_fish(weeks, total_last_year, now.strftime("%Y-%m-%d %H:%M"))
    )
    print(f"Wrote assets for {profile['login']} to {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

