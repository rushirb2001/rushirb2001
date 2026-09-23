"""Widget server: every README image is a URL rendered on request.

    GET /api/<widget>?<params>&theme=light|dark&format=svg|png

GET /api lists every widget and its parameters. Responses are cached at the
edge, so a README image costs one render per cache window, not per view.
"""

import logging
import os
import re

import resvg_py
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from widgets import cards, github
from widgets.canvas import ICONS, MODE, THEMES, font_files

log = logging.getLogger("widgets")

# Data widgets refresh every few hours at the edge; pure-content ones rarely change.
LIVE = "public, max-age=1800, s-maxage=14400, stale-while-revalidate=86400"
STATIC = "public, max-age=3600, s-maxage=86400, stale-while-revalidate=604800"
FAILED = "public, max-age=60, s-maxage=60"
SECURITY = {
    "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:",
    "X-Content-Type-Options": "nosniff",
}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
DEFAULT_SKIP = ("Jupyter Notebook", "HTML", "CSS")
HUES = ("blue", "green", "purple", "amber", "orange", "red", "accent", "muted")


class Params:
    """Validated access to the query string. Anything malformed raises ValueError."""

    def __init__(self, query):
        self.q = query

    def text(self, key, default="", limit=160):
        return CONTROL.sub("", self.q.get(key, default)).strip()[:limit]

    def required(self, key, limit=160):
        value = self.text(key, limit=limit)
        if not value:
            raise ValueError(f"missing '{key}'")
        return value

    def items(self, key, sep=",", limit=12):
        values = [CONTROL.sub("", v).strip() for v in self.text(key, limit=2000).split(sep)]
        return [v for v in values if v][:limit]

    def pairs(self, key, limit=160):
        """Repeated key=name:value parameters, as a dict."""
        out = {}
        for raw in self.q.getlist(key)[:24]:
            name, sep, value = CONTROL.sub("", raw).partition(":")
            if sep and name.strip() and value.strip():
                out[name.strip()] = value.strip()[:limit]
        return out

    def choice(self, key, options, default):
        value = self.text(key, default).lower()
        if value not in options:
            raise ValueError(f"'{key}' must be one of: {', '.join(options)}")
        return value

    def number(self, key, default, low, high):
        try:
            value = float(self.q.get(key, default))
        except ValueError:
            raise ValueError(f"'{key}' must be a number") from None
        return min(max(value, low), high)

    def user(self):
        return self.required("username", limit=39)


# ---------------------------------------------------------------- widgets

def w_stats(q, theme):
    return cards.stats(theme, github.profile(q.user()))


def w_languages(q, theme):
    skip = set(q.items("skip") or DEFAULT_SKIP)
    return cards.languages(theme, github.profile(q.user()), skip, int(q.number("count", 4, 1, 6)))


def w_commits(q, theme):
    return cards.commits(theme, github.profile(q.user()))


def w_repo(q, theme):
    repo = github.repository(q.user(), q.required("repo", limit=100))
    return cards.repo(theme, repo, status=cards.status_key(q.text("status")),
                      description=q.text("description", limit=240), install=q.text("install", limit=120))


def w_repos(q, theme):
    names = q.items("repos")
    repos = github.repositories(q.user(), names or None, limit=int(q.number("count", 6, 1, 12)))
    if not repos:
        raise ValueError("no public repositories to show")
    statuses = {name: "coming-soon" for name in q.items("soon")}
    statuses.update({k: cards.status_key(v) for k, v in q.pairs("status").items()})
    options = {"statuses": statuses, "descriptions": q.pairs("describe", limit=240)}
    title = q.text("title", limit=60)
    if q.choice("layout", ("carousel", "list"), "carousel") == "list":
        return cards.repo_list(theme, repos, title=title or "Public projects", **options)
    return cards.carousel(theme, repos, title=title, installs=q.pairs("install", limit=120),
                          interval=q.number("interval", 4.5, 2, 12), **options)


def w_hero(q, theme):
    return cards.hero(theme, q.required("name", limit=60), eyebrow=q.text("eyebrow", limit=80),
                      line=q.text("line", limit=140), tagline=tuple(q.items("tagline", sep="|", limit=5)))


def w_section(q, theme):
    return cards.section(theme, q.required("title", limit=60), q.text("caption", limit=80))


def w_link(q, theme):
    return cards.link(theme, q.required("label", limit=24), q.required("value", limit=60),
                      icon=q.choice("icon", tuple(ICONS), "link-external"), hue=q.choice("hue", HUES, "blue"))


def w_publication(q, theme):
    return cards.publication(theme, q.required("title", limit=160), q.text("venue", limit=80),
                             q.text("year", limit=4))


def w_product(q, theme):
    features = []
    for raw in q.q.getlist("feature")[:5]:
        icon, sep, text = CONTROL.sub("", raw).partition(":")
        if not sep:
            icon, text = "arrow-right", raw
        if icon not in ICONS:
            raise ValueError(f"unknown icon '{icon}'")
        features.append((icon, text.strip()[:90]))
    crop = [q.number(f"crop_{side}", default, 0, 1) for side, default in
            (("left", 0), ("top", 0), ("right", 1), ("bottom", 1))]
    return cards.product(theme, name=q.required("name", limit=40), role=q.text("role", limit=60),
                         tagline=q.text("tagline", limit=80), audience=q.text("audience", limit=160),
                         features=tuple(features), cta=q.text("cta", limit=40),
                         image=q.text("image", limit=300) or None, crop=tuple(crop))


# name: (renderer, cache policy, parameters) — the parameter list is what GET /api publishes.
WIDGETS = {
    "stats": (w_stats, LIVE, ["username"]),
    "languages": (w_languages, LIVE, ["username", "skip=Lang,Lang", "count=1..6"]),
    "commits": (w_commits, LIVE, ["username"]),
    "repo": (w_repo, LIVE, ["username", "repo", "status=auto|none|" + "|".join(cards.STATUSES),
                            "description", "install"]),
    "repos": (w_repos, LIVE, ["username", "repos=a,b,c (default: top by stars)", "count=1..12",
                              "layout=carousel|list", "title", "soon=a,b", "status=repo:status",
                              "describe=repo:text", "install=repo:command", "interval=2..12"]),
    "hero": (w_hero, STATIC, ["name", "eyebrow", "line", "tagline=a|b|c"]),
    "section": (w_section, STATIC, ["title", "caption"]),
    "link": (w_link, STATIC, ["label", "value", "icon", "hue=" + "|".join(HUES)]),
    "publication": (w_publication, STATIC, ["title", "venue", "year"]),
    "product": (w_product, STATIC, ["name", "role", "tagline", "audience", "feature=icon:text (repeat)",
                                    "cta", "image (allowed hosts only)", "crop_left|top|right|bottom=0..1"]),
}


# ---------------------------------------------------------------- routes

def render(request):
    q = Params(request.query_params)
    theme = q.q.get("theme", "light") if q.q.get("theme") in THEMES else "light"
    fmt = "png" if q.q.get("format") == "png" else "svg"
    animate = fmt == "svg" and q.q.get("animate", "true").lower() not in ("0", "false", "no")
    mode = MODE.set({"animate": animate, "embed": fmt == "svg"})
    try:
        spec = WIDGETS.get(request.path_params["widget"])
        if spec is None:
            raise LookupError(f"unknown widget '{request.path_params['widget']}'")
        renderer, policy, _ = spec
        svg, failed = renderer(q, theme), False
    except (ValueError, PermissionError, LookupError, github.NotFound) as exc:
        svg, policy, failed = cards.error(theme, str(exc)), FAILED, True
    except github.Upstream:
        svg, policy, failed = cards.error(theme, "GitHub is unavailable right now, try again shortly"), FAILED, True
    except Exception:
        log.exception("render failed: %s", request.url.path)
        svg, policy, failed = cards.error(theme, "unexpected error"), FAILED, True
    finally:
        MODE.reset(mode)

    headers = {"Cache-Control": policy, **SECURITY}
    if failed:
        headers["X-Widget-Error"] = "1"
    if fmt == "png":
        scale = Params(request.query_params).number("scale", 2, 1, 3)
        png = resvg_py.svg_to_bytes(svg_string=svg, skip_system_fonts=True, font_files=font_files(), zoom=scale)
        return Response(bytes(png), media_type="image/png", headers=headers)
    return Response(svg, media_type="image/svg+xml; charset=utf-8", headers=headers)


def index(request):
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "usage": f"{base}/api/<widget>?<params>&theme=light|dark&format=svg|png&animate=true|false",
        "widgets": {name: params for name, (_, _, params) in WIDGETS.items()},
        "source": "https://github.com/rushirb2001/rushirb2001",
    }, headers={"Cache-Control": STATIC})


def health(request):
    return JSONResponse({"ok": True, "commit": os.environ.get("VERCEL_GIT_COMMIT_SHA", "local")[:7]},
                        headers={"Cache-Control": "no-store"})


app = Starlette(routes=[
    Route("/api", index),
    Route("/api/health", health),
    Route("/api/{widget}", render),
])
