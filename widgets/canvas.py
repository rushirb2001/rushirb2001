"""Drawing primitives shared by every widget: palette, type scale, fonts and motion.

A Canvas collects SVG elements plus the text drawn in each font. On output it
embeds just those glyphs as a subsetted woff2 (SVG mode), or leaves the fonts
out so the PNG rasterizer can load the TTFs directly (PNG mode).
"""

import base64
import functools
import io
import json
import tempfile
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from fontTools import subset
from fontTools.ttLib import TTFont

ASSETS = Path(__file__).parent / "assets"
ICONS = json.loads((ASSETS / "icons.json").read_text())  # Octicons, MIT (see icons.LICENSE)

# Base palette mirrors personal-website (paper, ink, navy). The hues are GitHub's
# Primer colors, used for icons and chart marks; green carries the commit graph.
THEMES = {
    "light": {
        "bg": "#f4f1ec", "border": "rgba(26,26,26,0.14)", "ink": "#1a1a1a",
        "muted": "rgba(26,26,26,0.74)", "faint": "rgba(26,26,26,0.60)", "accent": "#1f3a5f",
        "blue": "#0969da", "green": "#1a7f37", "purple": "#8250df",
        "amber": "#9a6700", "orange": "#bc4c00", "red": "#cf222e",
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

# One type scale for every widget. Nothing renders below 12px; monospace is kept
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

# The embedded @font-face uses the short alias; the real family names follow so
# the PNG rasterizer (which reads font files, not CSS) finds the same faces.
FAMILIES = {
    "GS": "GS, 'Google Sans', sans-serif",
    "GSC": "GSC, 'Google Sans Code Monospace', 'Google Sans Code', monospace",
}

# Per-request render options, set by the server: animate, and embed fonts (SVG) or not (PNG).
MODE = ContextVar("mode", default={"animate": True, "embed": True})


# ---------------------------------------------------------------- fonts

def _decode(name):
    font = TTFont(ASSETS / f"{name}.woff2")
    font.flavor = None
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


TTF = {"GS": _decode("GoogleSans"), "GSC": _decode("GoogleSansCode")}
_METRICS = {k: TTFont(io.BytesIO(v)) for k, v in TTF.items()}


def measure(font, text, size):
    """Advance width of text at the default instance; close enough for layout."""
    f = _METRICS[font]
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
        out.append(fit(font, line, size, width))
    if words and out:
        out[-1] = fit(font, out[-1] + " " + " ".join(words), size, width)
    return out


@functools.lru_cache(maxsize=512)
def _font_face(family, glyphs):
    font = TTFont(io.BytesIO(TTF[family]))
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["kern", "liga", "calt", "tnum"]
    sub = subset.Subsetter(opts)
    sub.populate(text=glyphs + " …")
    sub.subset(font)
    font.flavor = "woff2"
    buf = io.BytesIO()
    font.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return (f"@font-face{{font-family:'{family}';font-weight:300 800;"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2')}}")


def font_face(family, text):
    """An embeddable @font-face rule carrying only the glyphs in text."""
    return _font_face(family, "".join(sorted(set(text))))


@functools.cache
def font_files():
    """TTF paths for the PNG rasterizer, written once per instance."""
    folder = Path(tempfile.gettempdir()) / "widget-fonts"
    folder.mkdir(exist_ok=True)
    paths = []
    for name, data in TTF.items():
        path = folder / f"{name}.ttf"
        if not path.exists() or path.stat().st_size != len(data):
            path.write_bytes(data)
        paths.append(str(path))
    return paths


# ---------------------------------------------------------------- motion

# Entrance motion: every element plays once on load, then holds. Only carousels
# and status dots loop, since those are the parts meant to stay alive.
MOTION = """
@keyframes up{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes gx{from{transform:scaleX(0)}to{transform:none}}
@keyframes gy{from{transform:scaleY(0)}to{transform:none}}
@keyframes pop{from{opacity:0;transform:scale(.3)}to{opacity:1;transform:none}}
@keyframes draw{from{stroke-dashoffset:1}to{stroke-dashoffset:0}}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(1.9)}}
.up,.fade,.gx,.gy,.pop,.draw,.arc{animation-duration:.7s;
  animation-timing-function:cubic-bezier(.2,.8,.2,1);animation-fill-mode:both}
.up,.gx,.gy,.pop,.pulse{transform-box:fill-box}
.up{animation-name:up}.fade{animation-name:fade}
.gx{animation-name:gx;transform-origin:left center}
.gy{animation-name:gy;transform-origin:center bottom}
.pop{animation-name:pop;transform-origin:center;animation-duration:.4s}
.draw{animation-name:draw;stroke-dasharray:1;animation-duration:.6s}
.arc{animation-duration:.9s}
.pulse{animation:pulse 1.8s ease-in-out infinite;transform-origin:center}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""


class Canvas:
    """Collects SVG elements and the text drawn in each font, for subsetting."""

    def __init__(self, w, h, theme):
        mode = MODE.get()
        self.w, self.h, self.t = w, h, THEMES[theme]
        self.animate, self.embed = mode["animate"], mode["embed"]
        self.parts, self.css, self.base_css, self.defs = [], [], [], []
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
        ls = f' letter-spacing="{spacing}"' if spacing else ""
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FAMILIES[font]}" font-size="{size}" '
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

    def circle(self, cx, cy, r, fill, *, anim=None, delay=0):
        self.add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" fill="{self.color(fill)}"'
                 f'{self._motion(anim, delay)}/>')

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

    def pill(self, x, y, label, hue, *, anchor="end", pulse=False, delay=0):
        """A status pill: hue outline, optional live dot. Returns its width."""
        size = 11.5
        w = measure("GS", label, size) + (30 if pulse else 20)
        left = x - w if anchor == "end" else x
        self.rect(left, y, w, 22, "bg", rx=11, stroke=hue, anim="pop", delay=delay)
        tx = left + 10
        if pulse:
            self.circle(left + 12, y + 11, 3.2, hue, anim="pulse")
            tx = left + 21
        self.label(tx, y + 15, label, font="GS", size=size, weight=500, fill=hue, anim="fade", delay=delay)
        return w

    def panel(self):
        self.parts.insert(0, f'<rect x="0.5" y="0.5" width="{self.w-1}" height="{self.h-1}" rx="8" '
                             f'fill="{self.t["bg"]}" stroke="{self.t["border"]}"/>')

    def eyebrow(self, x, y, s, **overrides):
        self.text(x, y, s, "eyebrow", anim="up", **overrides)

    def svg(self, title):
        faces = "".join(font_face(f, s) for f, s in self.glyphs.items() if s) if self.embed else ""
        motion = MOTION + "".join(self.css) if self.animate else ""
        style = faces + "".join(self.base_css) + motion
        defs = (f'<marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" '
                f'markerHeight="7" orient="auto-start-reverse"><path d="M0 0L8 4L0 8z" '
                f'fill="{self.t["faint"]}"/></marker>' + "".join(self.defs))
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label={quoteattr(title)}>'
                f'<title>{escape(title)}</title><style>{style}</style>'
                f'<defs>{defs}</defs>{"".join(self.parts)}</svg>')


def date_range(start, end, open_ended=False):
    if not start:
        return "—"

    def fmt(d):
        return d.strftime("%b %-d") + ("" if d.year == date.today().year else d.strftime(", %Y"))

    if open_ended:
        return f"{fmt(start)} – Present"
    return fmt(start) if start == end else f"{fmt(start)} – {fmt(end)}"
