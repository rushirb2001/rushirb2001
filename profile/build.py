"""Render every image on the profile README as a self-contained SVG.

Copy comes from profile.toml; numbers come from one GitHub GraphQL call.
Each figure is written in a light and a dark variant, with subsetted
Google Sans fonts embedded and a one-shot entrance animation, so nothing
depends on a third-party image server and README.md never needs editing.

    python profile/build.py [--out profile/out] [--data cached.json]
"""

import argparse
import base64
import io
import json
import os
import subprocess
import tomllib
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools import subset
from fontTools.ttLib import TTFont

HERE = Path(__file__).parent
CONFIG = tomllib.loads((HERE / "profile.toml").read_text())

# Palette mirrors personal-website: paper, ink, navy accent. Green is the commit graph.
THEMES = {
    "light": {
        "bg": "#f4f1ec", "border": "rgba(26,26,26,0.14)", "ink": "#1a1a1a",
        "muted": "rgba(26,26,26,0.62)", "faint": "rgba(26,26,26,0.40)", "accent": "#1f3a5f",
        "green": ["rgba(26,26,26,0.07)", "#c3dabc", "#8cbd85", "#4f9a5a", "#2c6e3c"],
    },
    "dark": {
        "bg": "#151515", "border": "rgba(244,241,236,0.12)", "ink": "#f4f1ec",
        "muted": "rgba(244,241,236,0.62)", "faint": "rgba(244,241,236,0.38)", "accent": "#a9bfdc",
        "green": ["rgba(244,241,236,0.07)", "#1f3d28", "#2b603a", "#3f8b50", "#62b86f"],
    },
}

QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      totalCommitContributions
      totalPullRequestContributions
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount weekday } }
      }
      commitContributionsByRepository(maxRepositories: 100) {
        contributions { totalCount }
        repository {
          isPrivate isFork
          languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
            edges { size node { name } }
          }
        }
      }
    }
  }
}"""


# ---------------------------------------------------------------- data

def github_token():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()


def fetch():
    body = json.dumps({"query": QUERY, "variables": {"login": CONFIG["login"]}}).encode()
    req = urllib.request.Request("https://api.github.com/graphql", data=body, headers={
        "Authorization": f"bearer {github_token()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.load(r)
    if "errors" in payload:
        raise SystemExit(f"GraphQL error: {payload['errors']}")
    return payload["data"]["user"]["contributionsCollection"]


def summarize(cc):
    days = [d for w in cc["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
    counts = [d["contributionCount"] for d in days]

    longest = run = 0
    for c in counts:
        run = run + 1 if c else 0
        longest = max(longest, run)
    # Today is still in progress, so an empty today doesn't break the streak.
    tail = counts[:-1] if counts and counts[-1] == 0 else counts
    current = 0
    for c in reversed(tail):
        if not c:
            break
        current += 1

    by_weekday = defaultdict(int)
    for d in days:
        by_weekday[d["weekday"]] += d["contributionCount"]

    # Languages weighted by this year's commits, split by each repo's byte share.
    # Public repos only, so a local run and the CI run (default token) agree.
    skip = set(CONFIG["languages"]["skip"])
    langs = defaultdict(float)
    public_repos = 0
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

    return {
        "days": days,
        "total": cc["contributionCalendar"]["totalContributions"],
        "commits": cc["totalCommitContributions"],
        "prs": cc["totalPullRequestContributions"],
        "active": sum(1 for c in counts if c),
        "current": current,
        "longest": longest,
        "weekday": [by_weekday[i] for i in (1, 2, 3, 4, 5, 6, 0)],
        "langs": sorted(langs.items(), key=lambda kv: -kv[1]),
        "lang_repos": public_repos,
        "built": datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------- fonts

FONTS = {"GS": TTFont(HERE / "fonts/GoogleSans.woff2"), "GSC": TTFont(HERE / "fonts/GoogleSansCode.woff2")}
_face_cache = {}


def measure(font, text, size):
    """Advance width of text at the default instance; close enough to right-align runs."""
    f = FONTS[font]
    cmap, hmtx, upm = f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm
    return sum(hmtx[cmap.get(ord(ch), ".notdef")][0] for ch in text) * size / upm


def font_face(family, text):
    """Subset a font to just the glyphs used and return an embeddable @font-face rule."""
    key = (family, frozenset(text))
    if key not in _face_cache:
        font = TTFont(HERE / ("fonts/GoogleSans.woff2" if family == "GS" else "fonts/GoogleSansCode.woff2"))
        opts = subset.Options()
        opts.flavor = "woff2"
        opts.layout_features = ["kern", "liga", "calt", "tnum"]
        sub = subset.Subsetter(opts)
        sub.populate(text=text + " ")
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
.up,.fade,.gx,.gy,.pop{transform-box:fill-box;animation-duration:.7s;
  animation-timing-function:cubic-bezier(.2,.8,.2,1);animation-fill-mode:both}
.up{animation-name:up}.fade{animation-name:fade}
.gx{animation-name:gx;transform-origin:left center}
.gy{animation-name:gy;transform-origin:center bottom}
.pop{animation-name:pop;transform-origin:center;animation-duration:.4s}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""


class Canvas:
    """Collects SVG elements and the text drawn in each font, for subsetting."""

    def __init__(self, w, h, theme):
        self.w, self.h, self.t = w, h, THEMES[theme]
        self.parts = []
        self.text = {"GS": "", "GSC": ""}

    def add(self, s):
        self.parts.append(s)

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
                 f'font-weight="{weight}" fill="{self.t.get(fill, fill)}" text-anchor="{anchor}"{ls}'
                 f'{self._motion(anim, delay, "font-variant-numeric:tabular-nums;")}>{escape(s)}</text>')

    def rect(self, x, y, w, h, fill, *, rx=0, opacity=None, anim=None, delay=0):
        # fill-opacity, not opacity: the entrance keyframes animate opacity and would override it.
        op = f' fill-opacity="{opacity}"' if opacity is not None else ""
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" '
                 f'fill="{self.t.get(fill, fill)}"{op}{self._motion(anim, delay)}/>')

    def panel(self):
        self.parts.insert(0, f'<rect x="0.5" y="0.5" width="{self.w-1}" height="{self.h-1}" rx="8" '
                             f'fill="{self.t["bg"]}" stroke="{self.t["border"]}"/>')

    def eyebrow(self, x, y, s):
        """The site's section opener: a short accent hairline over spaced uppercase mono."""
        self.rect(x, y, 28, 2, "accent", anim="gx")
        self.label(x, y + 20, s, size=10.5, fill="accent", spacing=1.6, upper=True, weight=500,
                   anim="up", delay=0.1)

    def svg(self, title):
        faces = "".join(font_face(f, s) for f, s in self.text.items() if s)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{escape(title)}">'
                f'<title>{escape(title)}</title><style>{faces}{MOTION}</style>{"".join(self.parts)}</svg>')


# ---------------------------------------------------------------- figures

def hero(theme):
    h = CONFIG["hero"]
    c = Canvas(880, 150, theme)
    cx = c.w / 2
    c.rect(cx - 14, 8, 28, 2, "accent", anim="gx")
    c.label(cx, 32, h["eyebrow"], size=11, fill="accent", spacing=2, upper=True, weight=500,
            anchor="middle", anim="up", delay=0.1)
    c.label(cx, 86, h["name"], font="GS", size=50, fill="ink", anchor="middle", spacing=-1,
            anim="up", delay=0.2)
    c.label(cx, 114, h["line"], font="GS", size=15, fill="muted", anchor="middle", anim="up", delay=0.35)
    c.label(cx, 140, "   ·   ".join(h["principles"]), size=10.5, fill="faint", anchor="middle",
            spacing=0.4, anim="fade", delay=0.55)
    return c.svg(f'{h["name"]} — {h["eyebrow"]}')


def link(theme, slot, entry):
    label, value = entry["label"], entry["value"]
    d = slot * 0.12
    c = Canvas(170, 48, theme)
    c.panel()
    c.rect(14, 12, 2, 24, "accent", anim="gy", delay=d)
    c.label(25, 22, label, size=9, fill="accent", spacing=1.5, upper=True, weight=500, anim="up", delay=d + 0.1)
    size = 12.5 if measure("GS", value, 12.5) <= 132 else 132 / measure("GS", value, 1)
    c.label(25, 38, value, font="GS", size=round(size, 2), fill="ink", anim="up", delay=d + 0.18)
    return c.svg(f"{label}: {value}")


# Card widths fit GitHub's ~848px profile README column: two cards side by side in
# a table (13px cell padding each side), with the commit graph spanning both.
CARD_W, WIDE_W = 390, 806


def year_card(theme, s):
    c = Canvas(CARD_W, 190, theme)
    c.panel()
    c.eyebrow(22, 20, "The last 12 months")
    stats = [(f'{s["total"]:,}', "contributions"), (f'{s["commits"]:,}', "commits"),
             (f'{s["prs"]:,}', "pull requests"), (f'{s["active"]}', "active days")]
    for i, (n, lab) in enumerate(stats):
        x, y = 22 + (i % 2) * 112, 86 + (i // 2) * 54
        c.label(x, y, n, font="GS", size=26, fill="ink", spacing=-0.5, anim="up", delay=0.2 + i * 0.08)
        c.label(x, y + 16, lab, size=9.5, fill="muted", anim="fade", delay=0.3 + i * 0.08)
    # Weekday rhythm: when the work actually happens.
    names, vals = "MTWTFSS", s["weekday"]
    peak, base, ph, bx = max(vals) or 1, 152, 80, 252
    c.label(bx, 58, "by weekday", size=9.5, fill="muted", anim="fade", delay=0.2)
    for i, v in enumerate(vals):
        h = max(2, v / peak * ph)
        x = bx + i * 17
        c.rect(x, base - h, 11, h, "accent", rx=1.5, opacity=1 if v == peak else 0.45,
               anim="gy", delay=0.3 + i * 0.05)
        c.label(x + 5.5, base + 15, names[i], size=9, fill="faint", anchor="middle")
    return c.svg(f'{s["total"]:,} contributions in the last 12 months')


def languages_card(theme, s):
    c = Canvas(CARD_W, 190, theme)
    c.panel()
    c.eyebrow(22, 20, "Languages, by commits")
    langs = s["langs"]
    total = sum(v for _, v in langs) or 1
    top = [(k, v) for k, v in langs[:5] if v / total >= 0.01]
    rest = total - sum(v for _, v in top)
    rows = top + ([("Other", rest)] if rest / total >= 0.01 else [])
    opac = [1, 0.72, 0.5, 0.34, 0.22, 0.12]
    x, bar_w = 22.0, CARD_W - 44
    for i, ((name, v), o) in enumerate(zip(rows, opac)):
        w = v / total * bar_w
        c.rect(x, 58, max(w - 2, 1), 7, "accent", rx=2, opacity=o, anim="gx", delay=0.2 + i * 0.12)
        x += w
    col_w = (CARD_W - 44) / 2
    for i, ((name, v), o) in enumerate(zip(rows, opac)):
        col, row = i // 3, i % 3
        lx, ly = 22 + col * col_w, 96 + row * 24
        d = 0.3 + i * 0.07
        c.rect(lx, ly - 8, 8, 8, "accent", rx=2, opacity=o, anim="pop", delay=d)
        c.label(lx + 14, ly, name, font="GS", size=12.5, fill="ink", anim="up", delay=d)
        c.label(lx + col_w - 22, ly, f"{v / total * 100:.0f}%", size=10, fill="muted", anchor="end",
                anim="fade", delay=d)
    c.label(22, 174, f'weighted by commits · {s["lang_repos"]} public repos', size=9, fill="faint",
            anim="fade", delay=0.7)
    return c.svg("Languages by commits")


def commits_card(theme, s):
    days, greens = s["days"], THEMES[theme]["green"]
    first = date.fromisoformat(days[0]["date"])
    offset = timedelta(days=first.isoweekday() % 7)
    n_cols = (date.fromisoformat(days[-1]["date"]) - first + offset).days // 7 + 1
    step = (WIDE_W - 44) / n_cols
    cell, gx, gy = step * 0.8, 22, 74

    c = Canvas(WIDE_W, round(gy + 7 * step + 8 + cell + 18), theme)
    c.panel()
    c.eyebrow(22, 20, "Commit graph")
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
        c.rect(gx + col * step, gy + row * step, cell, cell,
               greens[level(d["contributionCount"])], rx=2.5, anim="pop",
               delay=0.2 + col * 0.014 + row * 0.03)
        if day.day <= 7 and row == 0 and day.month != last_month:
            c.label(gx + col * step, gy - 8, day.strftime("%b"), size=9.5, fill="faint",
                    anim="fade", delay=0.2 + col * 0.014)
            last_month = day.month
    grid_right = gx + col * step + cell
    done = 0.2 + col * 0.014 + 0.3

    # Footer: total, refresh stamp, legend.
    cell_gap = step - cell
    ly = gy + 7 * step + 8
    built = s["built"].strftime("%-d %b %Y")
    c.label(gx, ly + cell * 0.8, f'{s["total"]:,} contributions in the last year · refreshed {built}',
            size=9, fill="muted", anim="fade", delay=done)
    lx = grid_right - 5 * step + cell_gap - measure("GSC", "more", 9) - 6
    c.label(lx - 6, ly + cell * 0.8, "less", size=9, fill="faint", anchor="end", anim="fade", delay=done)
    for i, g in enumerate(greens):
        c.rect(lx + i * step, ly, cell, cell, g, rx=2.5, anim="pop", delay=done + i * 0.05)
    c.label(grid_right, ly + cell * 0.8, "more", size=9, fill="faint", anchor="end", anim="fade", delay=done)

    # Streaks sit in the header row, right-aligned against the grid.
    x = grid_right
    for n, lab in [(s["longest"], "longest streak"), (s["current"], "current streak")]:
        num = f"{n}d"
        c.label(x, 38, lab, size=9.5, fill="muted", anchor="end", anim="fade", delay=0.3)
        x -= measure("GSC", lab, 9.5) + 6
        c.label(x, 39, num, font="GS", size=18, fill="ink", anchor="end", anim="up", delay=0.25)
        x -= measure("GS", num, 18) + 24
    return c.svg(f'{s["total"]:,} contributions, current streak {s["current"]} days')


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--data", help="read a cached API response instead of calling GitHub")
    args = ap.parse_args()

    cc = json.loads(Path(args.data).read_text()) if args.data else fetch()
    s = summarize(cc)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for theme in THEMES:
        files = {
            "hero": hero(theme),
            "year": year_card(theme, s),
            "languages": languages_card(theme, s),
            "commits": commits_card(theme, s),
            **{f"link-{i + 1}": link(theme, i, e) for i, e in enumerate(CONFIG["links"])},
        }
        for name, svg in files.items():
            # Write-then-rename so a live preview never sees a missing or half-written file.
            tmp = out / f".{name}-{theme}.svg.tmp"
            tmp.write_text(svg)
            os.replace(tmp, out / f"{name}-{theme}.svg")

    print(f"built {len(list(out.glob('*.svg')))} svgs · {s['total']:,} contributions · "
          f"streak {s['current']}/{s['longest']} · {s['built']:%Y-%m-%d %H:%M} UTC")


if __name__ == "__main__":
    main()
