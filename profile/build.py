"""Render the profile README, images and markdown alike, from profile.toml.

Copy comes from profile.toml; numbers come from the GitHub GraphQL API. Each
image is written in a light and a dark variant, with subsetted Google Sans
fonts embedded and a one-shot entrance animation, so nothing depends on a
third-party image server. The markdown is generated too, so adding a project
or a link means editing profile.toml, never README.md.

    python profile/build.py [--out DIR] [--markdown FILE --img-base URL_OR_PATH]
"""

import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import tomllib
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from fontTools import subset
from fontTools.ttLib import TTFont

HERE = Path(__file__).parent
CONFIG = tomllib.loads((HERE / "profile.toml").read_text())
ICONS = json.loads((HERE / "icons.json").read_text())  # Octicons, MIT (see icons.LICENSE)

# Base palette mirrors personal-website (paper, ink, navy). The hues are GitHub's
# Primer colors, used for icons and chart marks; green carries the commit graph.
THEMES = {
    "light": {
        "bg": "#f4f1ec", "border": "rgba(26,26,26,0.14)", "ink": "#1a1a1a",
        "muted": "rgba(26,26,26,0.62)", "faint": "rgba(26,26,26,0.40)", "accent": "#1f3a5f",
        "blue": "#0969da", "green": "#1a7f37", "purple": "#8250df",
        "amber": "#bf8700", "orange": "#bc4c00", "red": "#cf222e",
        "graph": ["rgba(26,26,26,0.07)", "#c3dabc", "#8cbd85", "#4f9a5a", "#2c6e3c"],
    },
    "dark": {
        "bg": "#151515", "border": "rgba(244,241,236,0.12)", "ink": "#f4f1ec",
        "muted": "rgba(244,241,236,0.62)", "faint": "rgba(244,241,236,0.38)", "accent": "#a9bfdc",
        "blue": "#4493f8", "green": "#3fb950", "purple": "#a371f7",
        "amber": "#d29922", "orange": "#db6d28", "red": "#f85149",
        "graph": ["rgba(244,241,236,0.07)", "#1f3d28", "#2b603a", "#3f8b50", "#62b86f"],
    },
}

# Widths fit GitHub's ~848px profile README column: two cards and a space per row.
CARD_W, WIDE_W = 401, 806


# ---------------------------------------------------------------- data

BASE_QUERY = """
query($login: String!) {
  user(login: $login) {
    createdAt
    repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC, first: 100) {
      nodes { stargazerCount }
    }
    repositoriesContributedTo(first: 1, includeUserRepositories: true,
                              contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]) { totalCount }
    contributionsCollection {
      contributionYears
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount weekday } }
      }
      commitContributionsByRepository(maxRepositories: 100) {
        contributions { totalCount }
        repository {
          isPrivate isFork
          languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
            edges { size node { name color } }
          }
        }
      }
    }
  }
}"""

DETAIL_FRAGMENTS = """
fragment Year on ContributionsCollection {
  totalCommitContributions totalPullRequestContributions totalIssueContributions
  contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } }
}
fragment Repo on Repository {
  name description url stargazerCount forkCount pushedAt
  licenseInfo { spdxId } primaryLanguage { name color }
}"""


def github_token():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()


def gql(query, **variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request("https://api.github.com/graphql", data=body, headers={
        "Authorization": f"bearer {github_token()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.load(r)
    # A repo the token can't see resolves to null; everything else is fatal.
    errors = [e for e in payload.get("errors", []) if e.get("type") != "NOT_FOUND"]
    if errors or not payload.get("data"):
        raise SystemExit(f"GraphQL error: {errors or payload}")
    return payload["data"]


def project_repos():
    return [r["repo"] for section in ("open_source", "personal") for r in CONFIG[section]["repos"]]


def fetch():
    login = CONFIG["login"]
    base = gql(BASE_QUERY, login=login)["user"]
    now = datetime.now(timezone.utc)
    years = []
    for y in sorted(base["contributionsCollection"]["contributionYears"]):
        to = now if y == now.year else datetime(y, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        years.append(f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", '
                     f'to: "{to:%Y-%m-%dT%H:%M:%SZ}") {{ ...Year }}')
    repos = [f'r{i}: repository(owner: $login, name: {json.dumps(name)}) {{ ...Repo }}'
             for i, name in enumerate(project_repos())]
    detail = gql(f'query($login: String!) {{ user(login: $login) {{ {" ".join(years)} }} '
                 f'{" ".join(repos)} }} {DETAIL_FRAGMENTS}', login=login)
    return {"base": base, "years": detail["user"],
            "repos": {name: detail.get(f"r{i}") for i, name in enumerate(project_repos())}}


def streaks(counts):
    """Current and longest runs of active days, with their date ranges, over sorted (date, n)."""
    longest, run_start, best = 0, None, (0, None, None)
    for day, n in counts:
        if n:
            run_start = run_start or day
            length = (day - run_start).days + 1
            if length > best[0]:
                best = (length, run_start, day)
        else:
            run_start = None
    # Today is still in progress, so an empty today doesn't break the streak.
    tail = counts[:-1] if counts and counts[-1][1] == 0 else counts
    current, end = 0, None
    for day, n in reversed(tail):
        if not n:
            break
        current, end = current + 1, end or day
    start = end - timedelta(days=current - 1) if current else None
    return (current, start, end), best


def summarize(data):
    base, years = data["base"], data["years"]
    cc = base["contributionsCollection"]
    recent = [d for w in cc["contributionCalendar"]["weeks"] for d in w["contributionDays"]]

    joined = datetime.fromisoformat(base["createdAt"]).date()
    today = date.fromisoformat(recent[-1]["date"])
    daily = {}
    for y in years.values():
        for w in y["contributionCalendar"]["weeks"]:
            for d in w["contributionDays"]:
                day = date.fromisoformat(d["date"])
                if joined <= day <= today:
                    daily[day] = d["contributionCount"]
    current, longest = streaks(sorted(daily.items()))

    # Languages weighted by the last year's commits, split by each repo's byte share.
    # Public repos only, so a local run and the CI run (default token) agree.
    skip = set(CONFIG["languages"]["skip"])
    langs, colors, public_repos = defaultdict(float), {}, 0
    for entry in cc["commitContributionsByRepository"]:
        repo = entry["repository"]
        if repo["isPrivate"] or repo["isFork"]:
            continue
        edges = [e for e in repo["languages"]["edges"] if e["node"]["name"] not in skip]
        size = sum(e["size"] for e in edges)
        if not size:
            continue
        public_repos += 1
        for e in edges:
            langs[e["node"]["name"]] += entry["contributions"]["totalCount"] * e["size"] / size
            colors[e["node"]["name"]] = e["node"]["color"] or "#8b949e"

    return {
        "recent": recent,
        "joined": joined,
        "total": sum(y["contributionCalendar"]["totalContributions"] for y in years.values()),
        "commits": sum(y["totalCommitContributions"] for y in years.values()),
        "prs": sum(y["totalPullRequestContributions"] for y in years.values()),
        "issues": sum(y["totalIssueContributions"] for y in years.values()),
        "stars": sum(r["stargazerCount"] for r in base["repositories"]["nodes"]),
        "contributed": base["repositoriesContributedTo"]["totalCount"],
        "current": current,
        "longest": longest,
        "langs": sorted(langs.items(), key=lambda kv: -kv[1]),
        "lang_colors": colors,
        "lang_repos": public_repos,
        "repos": data["repos"],
        "built": datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------- fonts

FONT_FILES = {"GS": HERE / "fonts/GoogleSans.woff2", "GSC": HERE / "fonts/GoogleSansCode.woff2"}
FONTS = {k: TTFont(v) for k, v in FONT_FILES.items()}
_face_cache = {}


def measure(font, text, size):
    """Advance width of text at the default instance; close enough for layout."""
    f = FONTS[font]
    cmap, hmtx, upm = f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm
    return sum(hmtx[cmap.get(ord(ch), ".notdef")][0] for ch in text) * size / upm


def fit(font, text, size, width):
    """Truncate text with an ellipsis so it fits width."""
    if measure(font, text, size) <= width:
        return text
    while text and measure(font, text + "…", size) > width:
        text = text[:-1]
    return text.rstrip(" ,.;:—-") + "…"


def wrap(font, text, size, width, lines):
    """Greedy word wrap into at most `lines` lines; the last one is ellipsized if needed."""
    out, words = [], text.split()
    while words and len(out) < lines:
        line = words.pop(0)
        while words and measure(font, f"{line} {words[0]}", size) <= width:
            line += " " + words.pop(0)
        out.append(line)
    if words:
        out[-1] = fit(font, out[-1] + " " + " ".join(words), size, width)
    return out


def font_face(family, text):
    """Subset a font to just the glyphs used and return an embeddable @font-face rule."""
    key = (family, frozenset(text))
    if key not in _face_cache:
        font = TTFont(FONT_FILES[family])
        opts = subset.Options()
        opts.flavor = "woff2"
        opts.layout_features = ["kern", "liga", "calt", "tnum"]
        sub = subset.Subsetter(opts)
        sub.populate(text=text + " …")
        sub.subset(font)
        buf = io.BytesIO()
        font.save(buf)
        b64 = base64.b64encode(buf.getvalue()).decode()
        _face_cache[key] = (f"@font-face{{font-family:'{family}';font-weight:300 800;"
                            f"src:url(data:font/woff2;base64,{b64}) format('woff2')}}")
    return _face_cache[key]


# ---------------------------------------------------------------- drawing

# Entrance motion: every element plays once on load, then holds. Nothing loops.
MOTION = """
@keyframes up{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes gx{from{transform:scaleX(0)}to{transform:none}}
@keyframes gy{from{transform:scaleY(0)}to{transform:none}}
@keyframes pop{from{opacity:0;transform:scale(.3)}to{opacity:1;transform:none}}
@keyframes draw{from{stroke-dashoffset:1}to{stroke-dashoffset:0}}
.up,.fade,.gx,.gy,.pop,.draw,.arc{animation-duration:.7s;
  animation-timing-function:cubic-bezier(.2,.8,.2,1);animation-fill-mode:both}
.up,.gx,.gy,.pop{transform-box:fill-box}
.up{animation-name:up}.fade{animation-name:fade}
.gx{animation-name:gx;transform-origin:left center}
.gy{animation-name:gy;transform-origin:center bottom}
.pop{animation-name:pop;transform-origin:center;animation-duration:.4s}
.draw{animation-name:draw;stroke-dasharray:1;animation-duration:.6s}
.arc{animation-duration:.9s}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""


class Canvas:
    """Collects SVG elements and the text drawn in each font, for subsetting."""

    def __init__(self, w, h, theme):
        self.w, self.h, self.t = w, h, THEMES[theme]
        self.parts, self.css = [], []
        self.text = {"GS": "", "GSC": ""}

    def add(self, s):
        self.parts.append(s)

    def color(self, name):
        return self.t.get(name, name)

    @staticmethod
    def _motion(anim, delay, style=""):
        cls = f' class="{anim}"' if anim else ""
        if anim and delay:
            style += f"animation-delay:{delay:.3f}s;"
        return cls + (f' style="{style}"' if style else "")

    def label(self, x, y, s, *, font="GSC", size=11, weight=400, fill="muted",
              anchor="start", spacing=0, upper=False, anim=None, delay=0):
        s = s.upper() if upper else s
        self.text[font] += s
        fam = "'GS',sans-serif" if font == "GS" else "'GSC',monospace"
        ls = f' letter-spacing="{spacing}"' if spacing else ""
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{fam}" font-size="{size}" '
                 f'font-weight="{weight}" fill="{self.color(fill)}" text-anchor="{anchor}"{ls}'
                 f'{self._motion(anim, delay, "font-variant-numeric:tabular-nums;")}>{escape(s)}</text>')

    def rect(self, x, y, w, h, fill, *, rx=0, opacity=None, stroke=None, anim=None, delay=0):
        # fill-opacity, not opacity: the entrance keyframes animate opacity and would override it.
        op = f' fill-opacity="{opacity}"' if opacity is not None else ""
        st = f' stroke="{self.color(stroke)}"' if stroke else ""
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" '
                 f'fill="{self.color(fill)}"{op}{st}{self._motion(anim, delay)}/>')

    def icon(self, name, x, y, size, fill, *, anim="pop", delay=0):
        spec = ICONS[name]
        paths = "".join(f'<path d="{d}"/>' for d in spec["paths"])
        # Outer group animates; inner group holds the placement transform, so they don't clash.
        self.add(f'<g{self._motion(anim, delay)}><g transform="translate({x:.1f} {y:.1f}) '
                 f'scale({size / spec["box"]:.4f})" fill="{self.color(fill)}">{paths}</g></g>')

    def line(self, d, stroke, *, width=1.25, anim="draw", delay=0, arrow=False):
        marker = ' marker-end="url(#arrow)"' if arrow else ""
        self.add(f'<path d="{d}" pathLength="1" fill="none" stroke="{self.color(stroke)}" '
                 f'stroke-width="{width}"{marker}{self._motion(anim, delay)}/>')

    def panel(self):
        self.parts.insert(0, f'<rect x="0.5" y="0.5" width="{self.w-1}" height="{self.h-1}" rx="8" '
                             f'fill="{self.t["bg"]}" stroke="{self.t["border"]}"/>')

    def eyebrow(self, x, y, s):
        self.label(x, y, s, size=10.5, fill="accent", spacing=1.6, upper=True, weight=500, anim="up")

    def svg(self, title):
        faces = "".join(font_face(f, s) for f, s in self.text.items() if s)
        defs = (f'<defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" '
                f'markerHeight="7" orient="auto-start-reverse"><path d="M0 0L8 4L0 8z" '
                f'fill="{self.t["faint"]}"/></marker></defs>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label={quoteattr(title)}>'
                f'<title>{escape(title)}</title><style>{faces}{MOTION}{"".join(self.css)}</style>'
                f'{defs}{"".join(self.parts)}</svg>')


def date_range(start, end, open_ended=False):
    if not start:
        return "—"

    def fmt(d):
        return d.strftime("%b %-d") + ("" if d.year == date.today().year else d.strftime(", %Y"))

    if open_ended:
        return f"{fmt(start)} – Present"
    return fmt(start) if start == end else f"{fmt(start)} – {fmt(end)}"


# ---------------------------------------------------------------- figures

def hero(theme):
    h = CONFIG["hero"]
    c = Canvas(880, 140, theme)
    cx = c.w / 2
    c.label(cx, 22, h["eyebrow"], size=11, fill="accent", spacing=2, upper=True, weight=500,
            anchor="middle", anim="up")
    c.label(cx, 76, h["name"], font="GS", size=50, fill="ink", anchor="middle", spacing=-1,
            anim="up", delay=0.12)
    c.label(cx, 104, h["line"], font="GS", size=15, fill="muted", anchor="middle", anim="up", delay=0.28)
    c.label(cx, 130, "   ·   ".join(h["principles"]), size=10.5, fill="faint", anchor="middle",
            spacing=0.4, anim="fade", delay=0.48)
    return c.svg(f'{h["name"]} — {h["eyebrow"]}')


def section(theme, key, count=None):
    s = CONFIG[key]
    c = Canvas(WIDE_W, 44, theme)
    c.label(4, 30, s["title"], font="GS", size=21, fill="ink", spacing=-0.3, anim="up")
    right = s["caption"] + (f" · {count}" if count is not None else "")
    c.label(WIDE_W - 4, 30, right, size=10.5, fill="muted", anchor="end", anim="fade", delay=0.15)
    return c.svg(s["title"])


def stats_card(theme, s):
    c = Canvas(CARD_W, 200, theme)
    c.panel()
    c.eyebrow(22, 32, "GitHub stats")
    rows = [("star", "amber", "Total stars", s["stars"]),
            ("git-commit", "green", "Total commits", s["commits"]),
            ("git-pull-request", "purple", "Pull requests", s["prs"]),
            ("issue-opened", "orange", "Issues", s["issues"]),
            ("repo", "blue", "Contributed to", s["contributed"])]
    for i, (icon, hue, label, value) in enumerate(rows):
        y, d = 66 + i * 27, 0.15 + i * 0.07
        c.icon(icon, 22, y - 11, 14, hue, delay=d)
        c.label(44, y, label, font="GS", size=12.5, fill="ink", anim="up", delay=d)
        c.label(222, y, f"{value:,}", font="GS", size=13.5, weight=500, fill="ink", anchor="end",
                anim="up", delay=d + 0.05)
    # The GitHub mark as a filled disc, after the old stats card.
    c.icon("mark-github", 256, 58, 112, "purple", delay=0.3)
    return c.svg(f'{s["stars"]} stars, {s["commits"]:,} commits, {s["prs"]} pull requests')


def languages_card(theme, s):
    c = Canvas(CARD_W, 200, theme)
    c.panel()
    c.eyebrow(22, 32, "Top languages")
    langs = s["langs"]
    total = sum(v for _, v in langs) or 1
    top = [(k, v) for k, v in langs[:5] if v / total >= 0.01]
    rest = total - sum(v for _, v in top)
    rows = [(k, v, s["lang_colors"][k]) for k, v in top]
    if rest / total >= 0.01:
        rows.append(("Other", rest, c.t["faint"]))

    # Donut: each arc grows from where the previous one ends, one after another.
    cx, cy, r, sw = 318, 112, 48, 15
    circ = 2 * 3.14159265 * r
    start, arcs = 0.0, []
    for i, (name, v, color) in enumerate(rows):
        length = v / total * circ
        visible = max(length - 2, 0.5)
        c.css.append(f"@keyframes a{i}{{from{{stroke-dasharray:0 {circ:.1f}}}"
                     f"to{{stroke-dasharray:{visible:.1f} {circ:.1f}}}}}")
        arcs.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="{sw}" '
                    f'stroke-dasharray="{visible:.1f} {circ:.1f}" stroke-dashoffset="{-start:.1f}" class="arc" '
                    f'style="animation-name:a{i};animation-delay:{0.2 + i * 0.18:.2f}s"/>')
        start += length
    # Rotate the group, not the arcs, so the arcs' own animation can't disturb the placement.
    c.add(f'<g transform="rotate(-90 {cx} {cy})">{"".join(arcs)}</g>')
    c.label(cx, cy + 5, f"{rows[0][1] / total * 100:.0f}%" if rows else "", font="GS", size=16,
            weight=500, fill="ink", anchor="middle", anim="fade", delay=0.6)

    for i, (name, v, color) in enumerate(rows):
        y, d = 66 + i * 22, 0.2 + i * 0.07
        c.rect(22, y - 9, 9, 9, color, rx=2, anim="pop", delay=d)
        c.label(38, y, name, font="GS", size=12.5, fill="ink", anim="up", delay=d)
        c.label(222, y, f"{v / total * 100:.0f}%", size=10.5, fill="muted", anchor="end", anim="fade", delay=d)
    c.label(22, 186, f'by commits · {s["lang_repos"]} public repos', size=9, fill="faint",
            anim="fade", delay=0.7)
    return c.svg("Top languages by commits")


def commits_card(theme, s):
    days, greens = s["recent"], THEMES[theme]["graph"]
    first = date.fromisoformat(days[0]["date"])
    offset = timedelta(days=first.isoweekday() % 7)
    n_cols = (date.fromisoformat(days[-1]["date"]) - first + offset).days // 7 + 1
    step = (WIDE_W - 44) / n_cols
    cell, gx, gy = step * 0.8, 22, 144

    c = Canvas(WIDE_W, round(gy + 7 * step + 8 + cell + 20), theme)
    c.panel()
    c.eyebrow(22, 32, "Commit graph")

    # Streak strip, after the old streak card: total, current (with flame), longest.
    cur, cur_start, cur_end = s["current"]
    best, best_start, best_end = s["longest"]
    blocks = [(f'{s["total"]:,}', "Total contributions", date_range(s["joined"], None, True), None),
              (f"{cur}", "Current streak", date_range(cur_start, cur_end), "flame"),
              (f"{best}", "Longest streak", date_range(best_start, best_end), "trophy")]
    for i, (num, label, when, icon) in enumerate(blocks):
        x, d = 22 + i * 262, 0.15 + i * 0.1
        if icon:
            c.icon(icon, x, 56, 18, "orange" if icon == "flame" else "amber", delay=d)
            nx = x + 26
        else:
            nx = x
        c.label(nx, 74, num, font="GS", size=28, fill="ink", spacing=-0.5, anim="up", delay=d)
        c.label(x, 96, label, size=10, fill="muted", anim="fade", delay=d + 0.05)
        c.label(x, 111, when, size=9.5, fill="faint", anim="fade", delay=d + 0.1)

    nonzero = sorted(d["contributionCount"] for d in days if d["contributionCount"])
    # Quartile buckets, the same scheme GitHub uses for its own graph.
    qs = [nonzero[int(len(nonzero) * q)] for q in (0.25, 0.5, 0.75)] if nonzero else [1, 2, 3]

    def level(n):
        return 0 if n == 0 else 1 + sum(n > q for q in qs)

    col, last_month = 0, None
    for d in days:
        day = date.fromisoformat(d["date"])
        col = (day - first + offset).days // 7
        row = d["weekday"]
        # Cells cascade in diagonally, left to right, then hold.
        c.rect(gx + col * step, gy + row * step, cell, cell, greens[level(d["contributionCount"])],
               rx=2.5, anim="pop", delay=0.3 + col * 0.014 + row * 0.03)
        if day.day <= 7 and row == 0 and day.month != last_month:
            c.label(gx + col * step, gy - 8, day.strftime("%b"), size=9.5, fill="faint",
                    anim="fade", delay=0.3 + col * 0.014)
            last_month = day.month
    grid_right = gx + col * step + cell
    done = 0.3 + col * 0.014 + 0.3

    ly = gy + 7 * step + 8
    c.label(gx, ly + cell * 0.8, f'last 12 months · refreshed {s["built"]:%-d %b %Y}',
            size=9, fill="muted", anim="fade", delay=done)
    lx = grid_right - 5 * step + (step - cell) - measure("GSC", "more", 9) - 6
    c.label(lx - 6, ly + cell * 0.8, "less", size=9, fill="faint", anchor="end", anim="fade", delay=done)
    for i, g in enumerate(greens):
        c.rect(lx + i * step, ly, cell, cell, g, rx=2.5, anim="pop", delay=done + i * 0.05)
    c.label(grid_right, ly + cell * 0.8, "more", size=9, fill="faint", anchor="end", anim="fade", delay=done)
    return c.svg(f'{s["total"]:,} contributions, current streak {cur} days, longest {best} days')


def project_card(theme, entry, repo):
    """A pinned-repo card: the repo describes itself unless profile.toml overrides it."""
    c = Canvas(CARD_W, 128, theme)
    c.panel()
    name = entry["repo"]
    desc = entry.get("description") or (repo or {}).get("description") or ""
    c.icon("repo", 22, 21, 14, "muted", delay=0.05)
    c.label(44, 33, fit("GS", name, 15.5, 270), font="GS", size=15.5, weight=500, fill="ink", anim="up", delay=0.08)
    license_id = ((repo or {}).get("licenseInfo") or {}).get("spdxId")
    if license_id and license_id != "NOASSERTION":
        c.label(CARD_W - 22, 32, license_id, size=9.5, fill="faint", anchor="end", anim="fade", delay=0.2)
    for i, line in enumerate(wrap("GS", desc, 12.5, CARD_W - 44, 2)):
        c.label(22, 60 + i * 18, line, font="GS", size=12.5, fill="muted", anim="up", delay=0.16 + i * 0.06)

    if repo:
        y, x = 108, 22
        lang = repo.get("primaryLanguage")
        if lang:
            c.add(f'<circle cx="{x + 5}" cy="{y - 4}" r="5" fill="{lang["color"] or "#8b949e"}" '
                  f'class="pop" style="animation-delay:.3s"/>')
            c.label(x + 15, y, lang["name"], size=10.5, fill="muted", anim="fade", delay=0.3)
            x += 26 + measure("GSC", lang["name"], 10.5)
        for icon, hue, value in [("star", "amber", repo["stargazerCount"]),
                                 ("repo-forked", "muted", repo["forkCount"])]:
            c.icon(icon, x, y - 11, 13, hue, delay=0.35)
            c.label(x + 18, y, f"{value:,}", size=10.5, fill="muted", anim="fade", delay=0.35)
            x += 30 + measure("GSC", f"{value:,}", 10.5)
        pushed = datetime.fromisoformat(repo["pushedAt"])
        c.label(CARD_W - 22, y, f"updated {pushed:%b %Y}", size=9.5, fill="faint", anchor="end",
                anim="fade", delay=0.4)
    return c.svg(f"{name}: {desc}")


def startup_card(theme):
    st = CONFIG["startup"]
    c = Canvas(WIDE_W, 214, theme)
    c.panel()
    c.eyebrow(22, 32, st["role"])
    c.label(22, 66, st["name"], font="GS", size=24, weight=500, fill="ink", spacing=-0.4, anim="up", delay=0.1)
    c.label(22 + measure("GS", st["name"], 24) + 14, 65, st["line"], font="GS", size=13, fill="muted",
            anim="up", delay=0.2)

    # One core, many surfaces: each client funnels into a single gateway.
    hues = ["blue", "purple", "green", "orange", "red"]
    ys = [112 + i * 32 for i in range(len(st["surfaces"]))]
    mid = (ys[0] + ys[-1]) / 2 + 12
    for i, (name, y) in enumerate(zip(st["surfaces"], ys)):
        d = 0.3 + i * 0.08
        c.rect(22, y, 96, 24, "bg", rx=12, stroke=hues[i % len(hues)], anim="pop", delay=d)
        c.rect(34, y + 9, 6, 6, hues[i % len(hues)], rx=3, anim="pop", delay=d)
        c.label(76, y + 16.5, name, font="GS", size=12, fill="ink", anchor="middle", anim="fade", delay=d)
        c.line(f"M118 {y + 12} C 160 {y + 12}, 160 {mid}, 198 {mid}", "faint", anim="draw", delay=d + 0.15)

    # Core pills size to their text, then share the remaining width as equal gaps.
    widths = [max(measure("GS", n["label"], 13), measure("GSC", n["caption"], 9.5)) + 28 for n in st["core"]]
    left, right = 200, WIDE_W - 22
    gap = (right - left - sum(widths)) / max(len(widths) - 1, 1)
    xs = [left + sum(widths[:i]) + gap * i for i in range(len(widths))]
    for i, (node, x, w) in enumerate(zip(st["core"], xs, widths)):
        d = 0.6 + i * 0.2
        c.rect(x, mid - 24, w, 48, "bg", rx=8, stroke="border", anim="pop", delay=d)
        c.label(x + 14, mid - 3, node["label"], font="GS", size=13, weight=500, fill="ink", anim="fade", delay=d)
        c.label(x + 14, mid + 14, node["caption"], size=9.5, fill="muted", anim="fade", delay=d + 0.05)
    # Requests flow gateway -> backend; the corpus is published into the backend.
    c.line(f"M{xs[0] + widths[0]} {mid} L{xs[1] - 3} {mid}", "faint", arrow=True, delay=0.8)
    c.line(f"M{xs[2]} {mid} L{xs[1] + widths[1] + 3} {mid}", "faint", arrow=True, delay=1.0)
    return c.svg(f'{st["name"]}: {st["line"]}')


def publication_card(theme, i, pub):
    c = Canvas(WIDE_W, 62, theme)
    c.panel()
    d = i * 0.1
    c.icon("book", 22, 16, 16, "purple", delay=d)
    c.label(48, 28, fit("GS", pub["title"], 14, WIDE_W - 150), font="GS", size=14, weight=500,
            fill="ink", anim="up", delay=d + 0.05)
    c.label(48, 46, pub["venue"], size=10, fill="muted", anim="fade", delay=d + 0.12)
    c.label(WIDE_W - 22, 38, str(pub["year"]), font="GS", size=18, fill="faint", anchor="end",
            anim="fade", delay=d + 0.15)
    return c.svg(f'{pub["title"]} — {pub["venue"]}, {pub["year"]}')


def link_card(theme, i, entry):
    w = (WIDE_W - 8) / 3
    c = Canvas(round(w), 56, theme)
    c.panel()
    d = i * 0.1
    c.icon(entry["icon"], 18, 20, 16, ["blue", "red", "green"][i % 3], delay=d)
    c.label(46, 24, entry["label"], size=9, fill="accent", spacing=1.5, upper=True, weight=500,
            anim="up", delay=d + 0.05)
    size = min(13.0, (w - 62) / max(measure("GS", entry["value"], 1), 1))
    c.label(46, 42, entry["value"], font="GS", size=round(size, 2), fill="ink", anim="up", delay=d + 0.1)
    return c.svg(f'{entry["label"]}: {entry["value"]}')


# ---------------------------------------------------------------- markdown

# Maps "name-theme" to the file actually written; filenames carry a content hash
# when --versioned is set, so previews can never show a stale cached copy.
FILENAMES = {}


def picture(base, name, width, alt, href=None):
    dark, light = FILENAMES[f"{name}-dark"], FILENAMES[f"{name}-light"]
    img = (f'<picture><source media="(prefers-color-scheme: dark)" srcset="{base}/{dark}" />'
           f'<img src="{base}/{light}" width="{width}" alt={quoteattr(alt)} /></picture>')
    return f'<a href="{href}">{img}</a>' if href else img


def rows_of(items, per_row):
    """Join inline images into rows: a space between cards, a line break between rows."""
    rows = [" \n".join(items[i:i + per_row]) for i in range(0, len(items), per_row)]
    return "<br/>\n".join(rows)


def block(*lines):
    return '<p align="center">\n' + "\n".join(lines) + "\n</p>\n"


def markdown(base, s):
    login = CONFIG["login"]
    out = ["<!-- Generated by profile/build.py from profile/profile.toml. Edit that file, not this one. -->\n"]
    out.append(block(picture(base, "hero", 880, CONFIG["hero"]["name"])))
    out.append(block(rows_of([picture(base, "stats", CARD_W, "GitHub stats"),
                              picture(base, "languages", CARD_W, "Top languages")], 2) + "<br/>",
                     picture(base, "commits", WIDE_W, "Commit graph")))
    for key in ("open_source", "personal"):
        repos = CONFIG[key]["repos"]
        cards = [picture(base, f"project-{r['repo']}", CARD_W, r["repo"],
                         (s["repos"].get(r["repo"]) or {}).get("url") or f"https://github.com/{login}/{r['repo']}")
                 for r in repos]
        out.append(block(picture(base, f"section-{key}", WIDE_W, CONFIG[key]["title"]) + "<br/>",
                         rows_of(cards, 2)))
    st = CONFIG["startup"]
    out.append(block(picture(base, "section-startup", WIDE_W, st["title"]) + "<br/>",
                     picture(base, "startup", WIDE_W, st["name"], st["url"])))
    pubs = CONFIG["publications"]["items"]
    out.append(block(picture(base, "section-publications", WIDE_W, CONFIG["publications"]["title"]) + "<br/>",
                     rows_of([picture(base, f"pub-{i + 1}", WIDE_W, p["title"], p["url"])
                              for i, p in enumerate(pubs)], 1)))
    links = CONFIG["links"]["items"]
    out.append(block(picture(base, "section-links", WIDE_W, CONFIG["links"]["title"]) + "<br/>",
                     rows_of([picture(base, f"link-{i + 1}", round((WIDE_W - 8) / 3), e["label"], e["url"])
                              for i, e in enumerate(links)], 3)))
    out.append(block(f'<img src="https://komarev.com/ghpvc/?username={login}&label=profile%20views'
                     f'&color=1f3a5f&style=flat-square" alt="Profile views" />'))
    return "\n".join(out)


# ---------------------------------------------------------------- main

def render_all(theme, s):
    files = {
        "hero": hero(theme),
        "stats": stats_card(theme, s),
        "languages": languages_card(theme, s),
        "commits": commits_card(theme, s),
        "startup": startup_card(theme),
        "section-open_source": section(theme, "open_source", f'{len(CONFIG["open_source"]["repos"])} repos'),
        "section-personal": section(theme, "personal", f'{len(CONFIG["personal"]["repos"])} repos'),
        "section-startup": section(theme, "startup"),
        "section-publications": section(theme, "publications", f'{len(CONFIG["publications"]["items"])} papers'),
        "section-links": section(theme, "links"),
    }
    for key in ("open_source", "personal"):
        for entry in CONFIG[key]["repos"]:
            files[f"project-{entry['repo']}"] = project_card(theme, entry, s["repos"].get(entry["repo"]))
    for i, pub in enumerate(CONFIG["publications"]["items"]):
        files[f"pub-{i + 1}"] = publication_card(theme, i, pub)
    for i, entry in enumerate(CONFIG["links"]["items"]):
        files[f"link-{i + 1}"] = link_card(theme, i, entry)
    return files


def write(path, text):
    # Write-then-rename so a live preview never sees a missing or half-written file.
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "out"), help="directory for the SVGs")
    ap.add_argument("--markdown", help="also write the README markdown to this file")
    ap.add_argument("--img-base", default="profile/out", help="image path or URL prefix used in the markdown")
    ap.add_argument("--data", help="read a cached API response (JSON) instead of calling GitHub")
    ap.add_argument("--save-data", help="write the API response to this file")
    ap.add_argument("--versioned", action="store_true",
                    help="put a content hash in each filename (local previews; defeats image caching)")
    args = ap.parse_args()

    data = json.loads(Path(args.data).read_text()) if args.data else fetch()
    if args.save_data:
        Path(args.save_data).write_text(json.dumps(data))
    s = summarize(data)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    written = set()
    for theme in THEMES:
        for name, svg in render_all(theme, s).items():
            key = f"{name}-{theme}"
            digest = hashlib.sha1(svg.encode()).hexdigest()[:8]
            FILENAMES[key] = f"{key}.{digest}.svg" if args.versioned else f"{key}.svg"
            write(out / FILENAMES[key], svg)
            written.add(FILENAMES[key])
    # Drop figures that are no longer produced (a removed project, a renamed card).
    for old in out.glob("*.svg"):
        if old.name not in written:
            old.unlink()
    count = len(written)
    if args.markdown:
        write(Path(args.markdown), markdown(args.img_base.rstrip("/"), s))

    missing = [r for r, v in s["repos"].items() if v is None]
    print(f"built {count} svgs · {s['total']:,} contributions since {s['joined']} · "
          f"streak {s['current'][0]}/{s['longest'][0]}"
          + (f" · not visible to this token: {', '.join(missing)}" if missing else ""))


if __name__ == "__main__":
    main()
