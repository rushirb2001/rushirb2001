"""Every widget, as a pure function from data and parameters to an SVG string."""

import base64
import functools
import io
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from PIL import Image

from .canvas import CARD_W, THEMES, WIDE_W, Canvas, date_range, fit, measure, wrap
from . import github

# ---------------------------------------------------------------- status

# A repo's status comes from the URL (status=...), else its GitHub topics, else archival.
STATUSES = {
    "coming-soon": ("Coming soon", "amber", True),
    "in-progress": ("In progress", "blue", True),
    "beta": ("Beta", "purple", False),
    "alpha": ("Alpha", "orange", False),
    "new": ("New", "green", False),
    "archived": ("Archived", "faint", False),
}
STATUS_ALIASES = {"soon": "coming-soon", "comingsoon": "coming-soon", "upcoming": "coming-soon",
                  "wip": "in-progress", "work-in-progress": "in-progress"}


def status_key(value):
    """Normalise a status name; 'auto' and 'none' pass through, anything unknown is rejected."""
    value = (value or "auto").strip().lower()
    value = STATUS_ALIASES.get(value, value)
    if value not in STATUSES and value not in ("auto", "none"):
        raise ValueError(f"unknown status '{value}'")
    return value


def resolve_status(repo, requested="auto"):
    if requested == "none":
        return None
    if requested in STATUSES:
        return STATUSES[requested]
    for topic in repo.get("topics", []):
        key = STATUS_ALIASES.get(topic, topic)
        if key in STATUSES:
            return STATUSES[key]
    if repo.get("isArchived"):
        return STATUSES["archived"]
    return None


# ---------------------------------------------------------------- shared pieces

def _meta_row(c, repo, x, y, right, *, anim=True):
    """Language, stars, forks on the left; last update on the right."""
    a = (lambda kind: kind) if anim else (lambda kind: None)
    lang = repo.get("primaryLanguage")
    if lang:
        c.circle(x + 6, y - 4.5, 6, lang["color"] or "#8b949e", anim=a("pop"), delay=0.3)
        c.text(x + 18, y, lang["name"], "meta", fill="muted", anim=a("fade"), delay=0.3)
        x += 34 + measure("GS", lang["name"], 12.5)
    for icon, hue, value in [("star", "amber", repo["stargazerCount"]), ("repo-forked", "muted", repo["forkCount"])]:
        c.icon(icon, x, y - 12, 14, hue, anim=a("pop"), delay=0.35)
        c.text(x + 20, y, f"{value:,}", "meta", fill="muted", anim=a("fade"), delay=0.35)
        x += 36 + measure("GS", f"{value:,}", 12.5)
    pushed = datetime.fromisoformat(repo["pushedAt"])
    c.text(right, y, f"Updated {pushed:%b %Y}", "meta", anchor="end", anim=a("fade"), delay=0.4)


def _action(c, repo, x, y, width, install, *, anim=True):
    """The card's call to action: an install command, else the homepage, else the repo link."""
    a = (lambda kind: kind) if anim else (lambda kind: None)
    if install:
        text = fit("GSC", install, 12, width - 48)
        w = measure("GSC", text, 12) + 44
        c.rect(x, y, w, 26, "border", rx=6, anim=a("pop"), delay=0.25)
        c.icon("terminal", x + 9, y + 6, 14, "muted", anim=a("pop"), delay=0.25)
        c.label(x + 31, y + 17.5, text, font="GSC", size=12, fill="ink", anim=a("fade"), delay=0.28)
        return
    url = repo.get("homepageUrl") or repo["url"]
    shown = urllib.parse.urlsplit(url)
    shown = fit("GS", (shown.netloc + shown.path).rstrip("/").removeprefix("www."), 13, width - 26)
    c.icon("link-external", x, y + 6, 14, "blue", anim=a("pop"), delay=0.25)
    c.text(x + 22, y + 18, shown, "label", size=13, fill="blue", anim=a("fade"), delay=0.28)


def _badge(c, repo, status, right, y, *, anim=True):
    """Top-right slot: a status pill, else the latest release, else the license."""
    delay = 0.2 if anim else 0
    if status:
        label, hue, live = status
        c.pill(right, y - 16, label, hue, pulse=live, delay=delay)
    elif repo.get("latestRelease"):
        c.pill(right, y - 16, repo["latestRelease"]["tagName"], "green", delay=delay)
    else:
        spdx = (repo.get("licenseInfo") or {}).get("spdxId")
        if spdx and spdx != "NOASSERTION":
            c.text(right, y, spdx, "meta", anchor="end", anim="fade" if anim else None, delay=0.2)


# ---------------------------------------------------------------- profile widgets

def hero(theme, name, eyebrow="", line="", tagline=()):
    c = Canvas(880, 150, theme)
    cx = c.w / 2
    if eyebrow:
        c.text(cx, 22, eyebrow, "eyebrow", size=12.5, spacing=2, anchor="middle", anim="up")
    c.label(cx, 80, name, font="GS", size=52, fill="ink", anchor="middle", spacing=-1, anim="up", delay=0.12)
    if line:
        c.text(cx, 110, fit("GS", line, 16, 860), "body", size=16, anchor="middle", anim="up", delay=0.28)
    if tagline:
        c.text(cx, 138, fit("GS", "   ·   ".join(tagline), 13.5, 860), "meta", size=13.5, anchor="middle",
               anim="fade", delay=0.48)
    return c.svg(f"{name} — {eyebrow}" if eyebrow else name)


def section(theme, title, caption=""):
    c = Canvas(WIDE_W, 50, theme)
    c.text(4, 34, title, "heading", anim="up")
    if caption:
        c.text(WIDE_W - 4, 34, fit("GS", caption, 13.5, 420), "body", anchor="end", anim="fade", delay=0.15)
    return c.svg(title)


TOP_H = 214  # stats and languages share a height so the row lines up


def stats(theme, s):
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
    # The GitHub mark as a filled disc, after the classic stats card.
    c.icon("mark-github", 268, 64, 104, "purple", delay=0.3)
    return c.svg(f'{s["stars"]} stars, {s["commits"]:,} commits, {s["prs"]} pull requests')


def languages(theme, s, skip, count=4):
    c = Canvas(CARD_W, TOP_H, theme)
    c.panel()
    c.eyebrow(24, 36, "Top languages")
    top, rest, total, repos = github.languages(s, skip, count)
    rows = list(top)
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
        c.text(44, y, fit("GS", name, 14, 140), "label", anim="up", delay=d)
        c.text(236, y, f"{v / total * 100:.0f}%", "value", size=14, fill="muted", anchor="end",
               anim="fade", delay=d)
    c.text(24, TOP_H - 20, f"By commits · {repos} public repos", "meta", anim="fade", delay=0.7)
    return c.svg("Top languages by commits")


def commits(theme, s):
    days, greens = s["recent"], THEMES[theme]["graph"]
    first = date.fromisoformat(days[0]["date"])
    offset = timedelta(days=first.isoweekday() % 7)
    n_cols = (date.fromisoformat(days[-1]["date"]) - first + offset).days // 7 + 1
    # Weekday labels take a gutter on the left, as on GitHub's own graph.
    pad, gutter = 24, 36
    gx, gy = pad + gutter, 176
    step = (WIDE_W - gx - pad) / n_cols
    cell = step * 0.8
    footer_y = gy + 7 * step + 30

    c = Canvas(WIDE_W, round(footer_y + 24), theme)
    c.panel()
    c.eyebrow(pad, 36, "Commit graph")

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

    c.text(pad, footer_y, f'Last 12 months · refreshed {s["built"]:%-d %b %Y}', "meta", anim="fade", delay=done)
    lx = grid_right - measure("GS", "More", 12.5) - 8 - 5 * step + (step - cell)
    c.text(lx - 8, footer_y, "Less", "meta", anchor="end", anim="fade", delay=done)
    for i, g in enumerate(greens):
        c.rect(lx + i * step, footer_y - cell + 1, cell, cell, g, rx=2.5, anim="pop", delay=done + i * 0.05)
    c.text(grid_right, footer_y, "More", "meta", anchor="end", anim="fade", delay=done)
    return c.svg(f'{s["total"]:,} contributions, current streak {cur} days, longest {best} days')


# ---------------------------------------------------------------- repositories

REPO_H = 184


def repo(theme, repo, *, status="auto", description="", install=""):
    """A pinned-repo card with a status pill and a link or install row."""
    c = Canvas(CARD_W, REPO_H, theme)
    c.panel()
    status = resolve_status(repo, status)
    c.icon("repo", 24, 22, 16, "muted", delay=0.05)
    right = CARD_W - 24
    title_w = 330 if not status else 330 - measure("GS", status[0], 11.5) - 30
    c.text(50, 36, fit("GS", repo["name"], 16, title_w), "title", anim="up", delay=0.08)
    _badge(c, repo, status, right, 36)
    desc = description or repo.get("description") or "No description yet."
    for i, line in enumerate(wrap("GS", desc, 13.5, CARD_W - 48, 2)):
        c.text(24, 66 + i * 20, line, "body", anim="up", delay=0.16 + i * 0.06)
    _action(c, repo, 24, 106, CARD_W - 48, install)
    _meta_row(c, repo, 24, REPO_H - 22, right)
    return c.svg(f'{repo["name"]}: {desc}')


def _slide(c, repo, *, status, description, install):
    """One carousel frame, drawn without per-element motion (the frame itself animates)."""
    right, pad = WIDE_W - 28, 28
    c.icon("repo", pad, 62, 20, "muted", anim=None)
    title_w = 560 if not status else 560 - measure("GS", status[0], 11.5) - 30
    c.label(pad + 30, 79, fit("GS", repo["name"], 22, title_w), font="GS", size=22, weight=500, fill="ink")
    _badge(c, repo, status, right, 80, anim=False)
    desc = description or repo.get("description") or "No description yet."
    for i, line in enumerate(wrap("GS", desc, 14.5, WIDE_W - 2 * pad, 2)):
        c.text(pad, 112 + i * 21, line, "body", size=14.5)
    _action(c, repo, pad, 146, WIDE_W - 2 * pad, install, anim=False)
    _meta_row(c, repo, pad, 206, right, anim=False)


def carousel(theme, repos, *, title="", statuses=None, descriptions=None,
             installs=None, interval=4.5):
    """Circulating card: one project at a time, crossfading on a loop, with position dots.

    With no title, each frame is labelled with its position instead ("02 / 04"), so a
    carousel under its own section heading doesn't repeat the heading.
    """
    statuses, descriptions, installs = statuses or {}, descriptions or {}, installs or {}
    n = len(repos)
    c = Canvas(WIDE_W, 228, theme)
    c.panel()
    if title:
        c.eyebrow(28, 36, title)
    fade, cycle = 0.6, n * interval
    a, b, e = fade / cycle * 100, (interval - fade) / cycle * 100, interval / cycle * 100
    # Static fallback (reduced motion, PNG): only the first frame shows.
    c.base_css.append(".slide,.on{opacity:0}.s0{opacity:1}")
    if n > 1:
        c.css.append(f"@keyframes slide{{0%{{opacity:0;transform:translateX(16px)}}"
                     f"{a:.3f}%{{opacity:1;transform:none}}{b:.3f}%{{opacity:1;transform:none}}"
                     f"{e:.3f}%{{opacity:0;transform:translateX(-16px)}}100%{{opacity:0}}}}"
                     f"@keyframes on{{0%{{opacity:0}}{a:.3f}%{{opacity:1}}{b:.3f}%{{opacity:1}}"
                     f"{e:.3f}%{{opacity:0}}100%{{opacity:0}}}}"
                     f".slide{{animation:slide {cycle:.2f}s linear infinite both}}"
                     f".on{{animation:on {cycle:.2f}s linear infinite both}}")
    # Position dots, top right: a faint dot per project, with a pill that lights on its turn.
    dx = WIDE_W - 28 - (n - 1) * 16 - 18
    for i in range(n):
        x = dx + i * 16
        c.rect(x, 27, 8, 8, "faint", rx=4, opacity=0.45)
        c.add(f'<rect x="{x - 5:.1f}" y="27" width="18" height="8" rx="4" fill="{c.color("accent")}" '
              f'class="on s{i}" style="animation-delay:{i * interval:.2f}s"/>')
    for i, r in enumerate(repos):
        c.add(f'<g class="slide s{i}" style="animation-delay:{i * interval:.2f}s">')
        if not title:
            c.text(28, 36, f"{i + 1:02d} / {n:02d}", "eyebrow")
        _slide(c, r, status=resolve_status(r, statuses.get(r["name"], "auto")),
               description=descriptions.get(r["name"], ""), install=installs.get(r["name"], ""))
        c.add("</g>")
    return c.svg(f"{title or 'Projects'}: " + ", ".join(r["name"] for r in repos))


def repo_list(theme, repos, *, title="Public projects", statuses=None, descriptions=None):
    """Embedded list: every project on one card, one row each."""
    statuses, descriptions = statuses or {}, descriptions or {}
    row_h, top = 58, 58
    c = Canvas(WIDE_W, top + row_h * len(repos) + 10, theme)
    c.panel()
    c.eyebrow(28, 36, title)
    for i, r in enumerate(repos):
        y, d = top + i * row_h, 0.12 + i * 0.08
        lang = r.get("primaryLanguage")
        c.circle(34, y + 17, 6, (lang or {}).get("color") or c.color("faint"), anim="pop", delay=d)
        name_w = measure("GS", r["name"], 15)
        c.text(50, y + 22, fit("GS", r["name"], 15, 330), "title", size=15, anim="up", delay=d)
        status = resolve_status(r, statuses.get(r["name"], "auto"))
        if status:
            c.pill(50 + min(name_w, 330) + 12, y + 6, status[0], status[1], anchor="start", pulse=status[2], delay=d)
        desc = descriptions.get(r["name"]) or r.get("description") or "No description yet."
        c.text(50, y + 43, fit("GS", desc, 13, WIDE_W - 190), "body", size=13, anim="fade", delay=d + 0.05)
        stars = f'{r["stargazerCount"]:,}'
        c.text(WIDE_W - 28, y + 22, stars, "value", size=14, anchor="end", anim="fade", delay=d)
        c.icon("star", WIDE_W - 28 - measure("GS", stars, 14) - 22, y + 10, 15, "amber", delay=d)
    return c.svg(f"{title}: " + ", ".join(r["name"] for r in repos))


# ---------------------------------------------------------------- product

IMAGE_LIMIT = 6 * 1024 * 1024


def image_hosts():
    return {h.strip().lower() for h in os.environ.get("IMAGE_HOSTS", "sushrutalgs.ai").split(",") if h.strip()}


class _ImageUnavailable(Exception):
    pass


def fetch_image(url, crop, width):
    """Fetch a product screenshot from an allowed host; return (JPEG data URI, size) or None."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in image_hosts():
        raise ValueError("image host is not allowed")
    try:
        return _fetch_image(url, crop, width)
    except _ImageUnavailable:
        return None  # the card still renders, just without the picture


@functools.lru_cache(maxsize=16)  # failures raise, so only successful fetches are cached
def _fetch_image(url, crop, width):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rushirb2001-widgets"})
        with urllib.request.urlopen(req, timeout=15) as r:
            if (urllib.parse.urlsplit(r.geturl()).hostname or "").lower() not in image_hosts():
                raise _ImageUnavailable("redirected off the allowlist")
            data = r.read(IMAGE_LIMIT + 1)
        if len(data) > IMAGE_LIMIT:
            raise _ImageUnavailable("image too large")
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except _ImageUnavailable:
        raise
    except Exception as exc:
        raise _ImageUnavailable(type(exc).__name__) from None
    w, h = img.size
    img = img.crop((round(crop[0] * w), round(crop[1] * h), round(crop[2] * w), round(crop[3] * h)))
    img.thumbnail((width * 2, width * 2), Image.LANCZOS)  # 2x for sharp rendering on retina screens
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}", img.size


FEATURE_HUES = ["purple", "blue", "green", "amber", "orange"]


def product(theme, *, name, role="", tagline="", audience="", features=(), cta="", image=None,
            crop=(0.0, 0.0, 1.0, 1.0)):
    """A product showcase: what it is, who it's for, what it does, and how it looks."""
    H, pad = 300, 24
    shot = fetch_image(image, tuple(crop), 340) if image else None
    text_w = 392 if shot else WIDE_W - 2 * pad
    c = Canvas(WIDE_W, H, theme)
    c.panel()
    if role:
        c.eyebrow(pad, 38, role)
    c.label(pad, 80, fit("GS", name, 30, text_w), font="GS", size=30, weight=500, fill="ink", spacing=-0.5,
            anim="up", delay=0.1)
    if tagline:
        c.text(pad, 110, fit("GS", tagline, 17, text_w), "title", size=17, weight=400, anim="up", delay=0.18)
    y = 136
    for line in wrap("GS", audience, 13.5, text_w, 2):
        c.text(pad, y, line, "body", anim="up", delay=0.26)
        y += 20
    y += 18
    for i, (icon, text) in enumerate(features):
        d = 0.35 + i * 0.08
        lines = wrap("GS", text, 13.5, text_w - 28, 2)
        c.icon(icon, pad, y - 13, 16, FEATURE_HUES[i % len(FEATURE_HUES)], delay=d)
        for j, line in enumerate(lines):
            c.text(pad + 28, y + j * 19, line, "label", size=13.5, anim="up", delay=d)
        y += 19 * len(lines) + 10
    if cta:
        c.text(pad, H - 26, fit("GS", cta, 14.5, text_w - 30), "title", size=14.5, fill="orange", anim="up", delay=0.7)
        c.icon("arrow-right", pad + measure("GS", cta, 14.5) + 12, H - 38, 15, "orange", delay=0.75)
    if shot:
        uri, (sw, sh) = shot
        fx = pad + text_w + 26
        fw = WIDE_W - fx - pad
        fh = min(fw * sh / sw, H - 2 * pad)
        fy = (H - fh) / 2
        c.defs.append(f'<clipPath id="shot"><rect x="{fx:.1f}" y="{fy:.1f}" width="{fw:.1f}" '
                      f'height="{fh:.1f}" rx="10"/></clipPath>')
        c.add(f'<g class="up" style="animation-delay:.3s"><image href="{uri}" x="{fx:.1f}" y="{fy:.1f}" '
              f'width="{fw:.1f}" height="{fh:.1f}" clip-path="url(#shot)" preserveAspectRatio="xMidYMid slice"/>'
              f'<rect x="{fx:.1f}" y="{fy:.1f}" width="{fw:.1f}" height="{fh:.1f}" rx="10" fill="none" '
              f'stroke="{c.t["border"]}"/></g>')
    return c.svg(f"{name}: {tagline} {audience}".strip())


# ---------------------------------------------------------------- small cards

def publication(theme, title, venue="", year=""):
    c = Canvas(WIDE_W, 72, theme)
    c.panel()
    c.icon("book", 24, 18, 18, "purple")
    c.text(54, 32, fit("GS", title, 15.5, WIDE_W - 150), "title", size=15.5, anim="up", delay=0.05)
    if venue:
        c.text(54, 53, fit("GS", venue, 12.5, WIDE_W - 150), "meta", fill="muted", anim="fade", delay=0.12)
    if year:
        c.text(WIDE_W - 24, 43, str(year), "heading", size=20, weight=400, fill="faint", anchor="end",
               anim="fade", delay=0.15)
    return c.svg(f"{title} — {venue}, {year}".strip(" ,—"))


LINK_W = round((WIDE_W - 8) / 3)


def link(theme, label, value, icon="link-external", hue="blue"):
    c = Canvas(LINK_W, 64, theme)
    c.panel()
    c.icon(icon, 20, 22, 20, hue)
    c.text(52, 27, label, "eyebrow", spacing=1, anim="up", delay=0.05)
    size = min(14.5, (LINK_W - 70) / max(measure("GS", value, 1), 1))
    c.text(52, 47, value, "label", size=round(max(size, 11), 2), anim="up", delay=0.1)
    return c.svg(f"{label}: {value}")


def error(theme, message):
    c = Canvas(CARD_W, 72, theme)
    c.panel()
    c.icon("issue-opened", 24, 18, 18, "red", anim=None)
    c.text(52, 31, "This widget couldn't render", "title", size=14.5)
    c.text(52, 51, fit("GS", message, 12.5, CARD_W - 76), "meta", fill="muted")
    return c.svg(f"Widget error: {message}")
