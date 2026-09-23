"""Render the profile README, images and markdown alike, from profile.toml.

Copy comes from profile.toml; numbers come from the GitHub GraphQL API. Each
image is written in a light and a dark variant, with subsetted Google Sans
fonts embedded and a one-shot entrance animation, so nothing depends on a
third-party image server. The markdown is generated too, so adding a project
or a link means editing profile.toml, never README.md.

    python profile/build.py --markdown README.md          # after editing profile.toml
    python profile/build.py --markdown SAMPLE.md --img-base profile/out --versioned   # local preview

The workflow runs the same script on a schedule and deploys the images to
GitHub Pages; it never commits. It only checks that README.md is current.
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
from PIL import Image

HERE = Path(__file__).parent
CONFIG = tomllib.loads((HERE / "profile.toml").read_text())
ICONS = json.loads((HERE / "icons.json").read_text())  # Octicons, MIT (see icons.LICENSE)

# Base palette mirrors personal-website (paper, ink, navy). The hues are GitHub's
# Primer colors, used for icons and chart marks; green carries the commit graph.
THEMES = {
    "light": {
        "bg": "#f4f1ec", "border": "rgba(26,26,26,0.14)", "ink": "#1a1a1a",
        "muted": "rgba(26,26,26,0.74)", "faint": "rgba(26,26,26,0.60)", "accent": "#1f3a5f",
        "blue": "#0969da", "green": "#1a7f37", "purple": "#8250df",
        "amber": "#bf8700", "orange": "#bc4c00", "red": "#cf222e",
        "graph": ["rgba(26,26,26,0.07)", "#c3dabc", "#8cbd85", "#4f9a5a", "#2c6e3c"],
    },
    "dark": {
        "bg": "#151515", "border": "rgba(244,241,236,0.12)", "ink": "#f4f1ec",
        "muted": "rgba(244,241,236,0.76)", "faint": "rgba(244,241,236,0.58)", "accent": "#a9bfdc",
        "blue": "#4493f8", "green": "#3fb950", "purple": "#a371f7",
        "amber": "#d29922", "orange": "#db6d28", "red": "#f85149",
        "graph": ["rgba(244,241,236,0.07)", "#1f3d28", "#2b603a", "#3f8b50", "#62b86f"],
    },
}

# Widths fit GitHub's ~848px profile README column: two cards and a space per row.
CARD_W, WIDE_W = 401, 806

# One type scale for every figure. Nothing renders below 12px; monospace is kept
# for the small uppercase headings only, since spaced mono is hard to read in runs.
TYPE = {
    "eyebrow": {"font": "GSC", "size": 12, "weight": 600, "spacing": 1.1, "upper": True, "fill": "accent"},
    "heading": {"font": "GS", "size": 22, "weight": 500, "spacing": -0.3, "fill": "ink"},
    "title": {"font": "GS", "size": 16, "weight": 500, "fill": "ink"},
    "stat": {"font": "GS", "size": 30, "spacing": -0.5, "fill": "ink"},
    "value": {"font": "GS", "size": 15, "weight": 500, "fill": "ink"},
    "label": {"font": "GS", "size": 14, "fill": "ink"},
    "body": {"font": "GS", "size": 13.5, "fill": "muted"},
    "meta": {"font": "GS", "size": 12.5, "fill": "faint"},
}


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
        self.glyphs = {"GS": "", "GSC": ""}

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
        self.glyphs[font] += s
        fam = "'GS',sans-serif" if font == "GS" else "'GSC',monospace"
        ls = f' letter-spacing="{spacing}"' if spacing else ""
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{fam}" font-size="{size}" '
                 f'font-weight="{weight}" fill="{self.color(fill)}" text-anchor="{anchor}"{ls}'
                 f'{self._motion(anim, delay, "font-variant-numeric:tabular-nums;")}>{escape(s)}</text>')

    def text(self, x, y, s, style, **overrides):
        """Draw text in one of the TYPE styles, with per-call overrides."""
        self.label(x, y, s, **{**TYPE[style], **overrides})

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
        self.text(x, y, s, "eyebrow", anim="up")

    def svg(self, title):
        faces = "".join(font_face(f, s) for f, s in self.glyphs.items() if s)
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
    c = Canvas(880, 150, theme)
    cx = c.w / 2
    c.text(cx, 22, h["eyebrow"], "eyebrow", size=12.5, spacing=2, anchor="middle", anim="up")
    c.label(cx, 80, h["name"], font="GS", size=52, fill="ink", anchor="middle", spacing=-1,
            anim="up", delay=0.12)
    c.text(cx, 110, h["line"], "body", size=16, anchor="middle", anim="up", delay=0.28)
    c.text(cx, 138, "   ·   ".join(h["principles"]), "meta", size=13.5, anchor="middle",
           anim="fade", delay=0.48)
    return c.svg(f'{h["name"]} — {h["eyebrow"]}')


def section(theme, key, count=None):
    s = CONFIG[key]
    c = Canvas(WIDE_W, 50, theme)
    c.text(4, 34, s["title"], "heading", anim="up")
    right = s["caption"] + (f" · {count}" if count is not None else "")
    c.text(WIDE_W - 4, 34, right, "body", anchor="end", anim="fade", delay=0.15)
    return c.svg(s["title"])


TOP_H = 214  # both top cards share a height so the row lines up


def stats_card(theme, s):
    c = Canvas(CARD_W, TOP_H, theme)
    c.panel()
    c.eyebrow(24, 36, "GitHub stats")
    rows = [("star", "amber", "Total stars", s["stars"]),
            ("git-commit", "green", "Total commits", s["commits"]),
            ("git-pull-request", "purple", "Pull requests", s["prs"]),
            ("issue-opened", "orange", "Issues", s["issues"]),
            ("repo", "blue", "Contributed to", s["contributed"])]
    for i, (icon, hue, label, value) in enumerate(rows):
        y, d = 74 + i * 29, 0.15 + i * 0.07
        c.icon(icon, 24, y - 13, 16, hue, delay=d)
        c.text(50, y, label, "label", anim="up", delay=d)
        c.text(236, y, f"{value:,}", "value", anchor="end", anim="up", delay=d + 0.05)
    # The GitHub mark as a filled disc, after the old stats card.
    c.icon("mark-github", 268, 64, 104, "purple", delay=0.3)
    return c.svg(f'{s["stars"]} stars, {s["commits"]:,} commits, {s["prs"]} pull requests')


def languages_card(theme, s):
    c = Canvas(CARD_W, TOP_H, theme)
    c.panel()
    c.eyebrow(24, 36, "Top languages")
    langs = s["langs"]
    total = sum(v for _, v in langs) or 1
    top = [(k, v) for k, v in langs[:4] if v / total >= 0.01]
    rest = total - sum(v for _, v in top)
    rows = [(k, v, s["lang_colors"][k]) for k, v in top]
    if rest / total >= 0.01:
        rows.append(("Other", rest, c.t["faint"]))

    # Donut: each arc grows from where the previous one ends, one after another.
    cx, cy, r, sw = 318, 116, 50, 16
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
    if rows:
        c.text(cx, cy + 6, f"{rows[0][1] / total * 100:.0f}%", "value", size=18, anchor="middle",
               anim="fade", delay=0.6)

    for i, (name, v, color) in enumerate(rows):
        y, d = 74 + i * 26, 0.2 + i * 0.07
        c.rect(24, y - 11, 11, 11, color, rx=2.5, anim="pop", delay=d)
        c.text(44, y, name, "label", anim="up", delay=d)
        c.text(236, y, f"{v / total * 100:.0f}%", "value", size=14, fill="muted", anchor="end",
               anim="fade", delay=d)
    c.text(24, TOP_H - 20, f'By commits · {s["lang_repos"]} public repos', "meta", anim="fade", delay=0.7)
    return c.svg("Top languages by commits")


def commits_card(theme, s):
    days, greens = s["recent"], THEMES[theme]["graph"]
    first = date.fromisoformat(days[0]["date"])
    offset = timedelta(days=first.isoweekday() % 7)
    n_cols = (date.fromisoformat(days[-1]["date"]) - first + offset).days // 7 + 1
    # Weekday labels take a gutter on the left, as on GitHub's own graph.
    gutter, pad = 36, 24
    gx, gy = pad + gutter, 176
    step = (WIDE_W - gx - pad) / n_cols
    cell = step * 0.8
    footer_y = gy + 7 * step + 30

    c = Canvas(WIDE_W, round(footer_y + 24), theme)
    c.panel()
    c.eyebrow(pad, 36, "Commit graph")

    # Streak strip, after the old streak card: total, current (with flame), longest.
    cur, cur_start, cur_end = s["current"]
    best, best_start, best_end = s["longest"]
    blocks = [(f'{s["total"]:,}', "Total contributions", date_range(s["joined"], None, True), None),
              (f"{cur}", "Current streak (days)", date_range(cur_start, cur_end), "flame"),
              (f"{best}", "Longest streak (days)", date_range(best_start, best_end), "trophy")]
    for i, (num, label, when, icon) in enumerate(blocks):
        x, d = pad + i * 262, 0.15 + i * 0.1
        nx = x
        if icon:
            c.icon(icon, x, 62, 22, "orange" if icon == "flame" else "amber", delay=d)
            nx = x + 30
        c.text(nx, 84, num, "stat", anim="up", delay=d)
        c.text(x, 108, label, "body", anim="fade", delay=d + 0.05)
        c.text(x, 128, when, "meta", anim="fade", delay=d + 0.1)

    nonzero = sorted(d["contributionCount"] for d in days if d["contributionCount"])
    # Quartile buckets, the same scheme GitHub uses for its own graph.
    qs = [nonzero[int(len(nonzero) * q)] for q in (0.25, 0.5, 0.75)] if nonzero else [1, 2, 3]

    def level(n):
        return 0 if n == 0 else 1 + sum(n > q for q in qs)

    for row, name in [(1, "Mon"), (3, "Wed"), (5, "Fri")]:
        c.text(pad, gy + row * step + cell * 0.85, name, "meta", size=12, anim="fade", delay=0.3)
    col, last_month = 0, None
    for d in days:
        day = date.fromisoformat(d["date"])
        col = (day - first + offset).days // 7
        row = d["weekday"]
        # Cells cascade in diagonally, left to right, then hold.
        c.rect(gx + col * step, gy + row * step, cell, cell, greens[level(d["contributionCount"])],
               rx=2.5, anim="pop", delay=0.3 + col * 0.014 + row * 0.03)
        if day.day <= 7 and row == 0 and day.month != last_month:
            c.text(gx + col * step, gy - 10, day.strftime("%b"), "meta", size=12, anim="fade",
                   delay=0.3 + col * 0.014)
            last_month = day.month
    grid_right = gx + col * step + cell
    done = 0.3 + col * 0.014 + 0.3

    c.text(pad, footer_y, f'Last 12 months · refreshed {s["built"]:%-d %b %Y}', "meta",
           anim="fade", delay=done)
    more_w = measure("GS", "More", 12.5)
    lx = grid_right - more_w - 8 - 5 * step + (step - cell)
    c.text(lx - 8, footer_y, "Less", "meta", anchor="end", anim="fade", delay=done)
    for i, g in enumerate(greens):
        c.rect(lx + i * step, footer_y - cell + 1, cell, cell, g, rx=2.5, anim="pop", delay=done + i * 0.05)
    c.text(grid_right, footer_y, "More", "meta", anchor="end", anim="fade", delay=done)
    return c.svg(f'{s["total"]:,} contributions, current streak {cur} days, longest {best} days')


def project_card(theme, entry, repo):
    """A pinned-repo card: the repo describes itself unless profile.toml overrides it."""
    c = Canvas(CARD_W, 140, theme)
    c.panel()
    name = entry["repo"]
    desc = entry.get("description") or (repo or {}).get("description") or ""
    c.icon("repo", 24, 22, 16, "muted", delay=0.05)
    c.text(50, 36, fit("GS", name, 16, 260), "title", anim="up", delay=0.08)
    license_id = ((repo or {}).get("licenseInfo") or {}).get("spdxId")
    if license_id and license_id != "NOASSERTION":
        c.text(CARD_W - 24, 35, license_id, "meta", anchor="end", anim="fade", delay=0.2)
    for i, line in enumerate(wrap("GS", desc, 13.5, CARD_W - 48, 2)):
        c.text(24, 66 + i * 20, line, "body", anim="up", delay=0.16 + i * 0.06)

    if repo:
        y, x = 118, 24
        lang = repo.get("primaryLanguage")
        if lang:
            c.add(f'<circle cx="{x + 6}" cy="{y - 4.5}" r="6" fill="{lang["color"] or "#8b949e"}" '
                  f'class="pop" style="animation-delay:.3s"/>')
            c.text(x + 18, y, lang["name"], "meta", fill="muted", anim="fade", delay=0.3)
            x += 34 + measure("GS", lang["name"], 12.5)
        for icon, hue, value in [("star", "amber", repo["stargazerCount"]),
                                 ("repo-forked", "muted", repo["forkCount"])]:
            c.icon(icon, x, y - 12, 14, hue, delay=0.35)
            c.text(x + 20, y, f"{value:,}", "meta", fill="muted", anim="fade", delay=0.35)
            x += 36 + measure("GS", f"{value:,}", 12.5)
        pushed = datetime.fromisoformat(repo["pushedAt"])
        c.text(CARD_W - 24, y, f"Updated {pushed:%b %Y}", "meta", anchor="end", anim="fade", delay=0.4)
    return c.svg(f"{name}: {desc}")


def screenshot(url, crop, width):
    """Fetch the product screenshot from the live site, crop and downscale it, as a JPEG data URI."""
    if url in _shot_cache:
        return _shot_cache[url]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "profile-build"})
        with urllib.request.urlopen(req, timeout=30) as r:
            img = Image.open(io.BytesIO(r.read())).convert("RGB")
    except Exception as exc:  # the card still renders, just without the picture
        print(f"warning: screenshot unavailable ({exc})")
        _shot_cache[url] = None
        return None
    w, h = img.size
    img = img.crop((round(crop[0] * w), round(crop[1] * h), round(crop[2] * w), round(crop[3] * h)))
    img.thumbnail((width * 2, width * 2), Image.LANCZOS)  # 2x for sharp rendering on retina screens
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
    _shot_cache[url] = (f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}", img.size)
    return _shot_cache[url]


_shot_cache = {}


def startup_card(theme):
    """The product as its users see it: what it is, who it's for, what it does."""
    st = CONFIG["startup"]
    H, pad, text_w = 300, 24, 392
    c = Canvas(WIDE_W, H, theme)
    c.panel()
    c.eyebrow(pad, 38, st["role"])
    c.label(pad, 80, st["name"], font="GS", size=30, weight=500, fill="ink", spacing=-0.5, anim="up", delay=0.1)
    c.text(pad, 110, st["tagline"], "title", size=17, weight=400, anim="up", delay=0.18)
    y = 136
    for i, line in enumerate(wrap("GS", st["audience"], 13.5, text_w, 2)):
        c.text(pad, y, line, "body", anim="up", delay=0.26)
        y += 20

    y += 18
    hues = ["purple", "blue", "green", "amber"]
    for i, feat in enumerate(st["features"]):
        d = 0.35 + i * 0.08
        lines = wrap("GS", feat["text"], 13.5, text_w - 28, 2)
        c.icon(feat["icon"], pad, y - 13, 16, hues[i % len(hues)], delay=d)
        for j, line in enumerate(lines):
            c.text(pad + 28, y + j * 19, line, "label", size=13.5, anim="up", delay=d)
        y += 19 * len(lines) + 10

    c.text(pad, H - 26, st["cta"], "title", size=14.5, fill="orange", anim="up", delay=0.7)
    c.icon("arrow-right", pad + measure("GS", st["cta"], 14.5) + 12, H - 38, 15, "orange", delay=0.75)

    # The live product screenshot, framed on the right.
    shot = screenshot(st["screenshot"], st.get("screenshot_crop", [0, 0, 1, 1]), 340)
    if shot:
        uri, (sw, sh) = shot
        fx, fw = pad + text_w + 26, WIDE_W - (pad + text_w + 26) - pad
        fh = fw * sh / sw
        fy = (H - fh) / 2
        c.add(f'<clipPath id="shot"><rect x="{fx:.1f}" y="{fy:.1f}" width="{fw:.1f}" height="{fh:.1f}" rx="10"/></clipPath>')
        c.add(f'<g class="up" style="animation-delay:.3s"><image href="{uri}" x="{fx:.1f}" y="{fy:.1f}" '
              f'width="{fw:.1f}" height="{fh:.1f}" clip-path="url(#shot)" preserveAspectRatio="xMidYMid slice"/>'
              f'<rect x="{fx:.1f}" y="{fy:.1f}" width="{fw:.1f}" height="{fh:.1f}" rx="10" fill="none" '
              f'stroke="{c.t["border"]}"/></g>')
    return c.svg(f'{st["name"]}: {st["tagline"]} {st["audience"]}')


def publication_card(theme, i, pub):
    c = Canvas(WIDE_W, 72, theme)
    c.panel()
    d = i * 0.1
    c.icon("book", 24, 18, 18, "purple", delay=d)
    c.text(54, 32, fit("GS", pub["title"], 15.5, WIDE_W - 150), "title", size=15.5, anim="up", delay=d + 0.05)
    c.text(54, 53, pub["venue"], "meta", fill="muted", anim="fade", delay=d + 0.12)
    c.text(WIDE_W - 24, 43, str(pub["year"]), "heading", size=20, weight=400, fill="faint",
           anchor="end", anim="fade", delay=d + 0.15)
    return c.svg(f'{pub["title"]} — {pub["venue"]}, {pub["year"]}')


def link_card(theme, i, entry):
    w = (WIDE_W - 8) / 3
    c = Canvas(round(w), 64, theme)
    c.panel()
    d = i * 0.1
    c.icon(entry["icon"], 20, 22, 20, ["blue", "red", "green"][i % 3], delay=d)
    c.text(52, 27, entry["label"], "eyebrow", spacing=1, anim="up", delay=d + 0.05)
    size = min(14.5, (w - 70) / max(measure("GS", entry["value"], 1), 1))
    c.text(52, 47, entry["value"], "label", size=round(size, 2), anim="up", delay=d + 0.1)
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
    ap.add_argument("--img-base", default=CONFIG["images"],
                    help="image URL prefix used in the markdown (default: the published Pages site)")
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
