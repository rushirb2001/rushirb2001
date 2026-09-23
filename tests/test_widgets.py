"""Every widget renders valid, readable output; private data never leaks; bad input fails soft."""

import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

import app as server
from widgets import cards, github

TODAY = date.today()


def calendar(days=371):
    start = TODAY - timedelta(days=days - 1)
    week, weeks = [], []
    for i in range(days):
        d = start + timedelta(days=i)
        week.append({"date": d.isoformat(), "contributionCount": (i * 7) % 5 if i % 4 else 0,
                     "weekday": (d.weekday() + 1) % 7})
        if len(week) == 7:
            weeks.append({"contributionDays": week})
            week = []
    if week:
        weeks.append({"contributionDays": week})
    return {"totalContributions": 999, "weeks": weeks}


def repo(name, private=False, topics=(), release=None, archived=False):
    return {"name": name, "description": f"{name} does one thing well.", "url": f"https://github.com/u/{name}",
            "homepageUrl": None, "stargazerCount": 3, "forkCount": 1, "pushedAt": "2026-07-01T00:00:00Z",
            "isPrivate": private, "isFork": False, "isArchived": archived, "licenseInfo": {"spdxId": "MIT"},
            "primaryLanguage": {"name": "Rust", "color": "#dea584"}, "topics": list(topics),
            "latestRelease": {"tagName": release} if release else None}


@pytest.fixture(autouse=True)
def fake_github(monkeypatch):
    base = {"createdAt": "2024-04-23T00:00:00Z", "repositories": {"nodes": [{"stargazerCount": 5}]},
            "repositoriesContributedTo": {"totalCount": 4},
            "contributionsCollection": {"contributionYears": [TODAY.year], "contributionCalendar": calendar(),
                                        "commitContributionsByRepository": [
                                            {"contributions": {"totalCount": 10}, "repository": {
                                                "isPrivate": False, "isFork": False, "languages": {"edges": [
                                                    {"size": 900, "node": {"name": "Rust", "color": "#dea584"}},
                                                    {"size": 100, "node": {"name": "HTML", "color": "#e34c26"}}]}}},
                                            {"contributions": {"totalCount": 99}, "repository": {
                                                "isPrivate": True, "isFork": False, "languages": {"edges": [
                                                    {"size": 5000, "node": {"name": "Secretlang", "color": "#000"}}]}}}]}}
    years = {f"y{TODAY.year}": {"totalCommitContributions": 40, "totalPullRequestContributions": 3,
                                "totalIssueContributions": 1, "contributionCalendar": calendar()}}
    known = {"cohors": repo("cohors", release="v0.6.0"), "principia": repo("principia"),
             "secret": repo("secret", private=True), "beta-thing": repo("beta-thing", topics=["beta"])}

    def fake_repository(username, name):
        github.check_user(username)
        github.check_repo(name)
        found = known.get(name)
        if found is None or found["isPrivate"]:
            raise github.NotFound("repository not found")
        return found

    def fake_repositories(username, names=None, limit=6):
        github.check_user(username)
        pool = [known[n] for n in (names or known) if n in known and not known[n]["isPrivate"]]
        return pool[:limit]

    monkeypatch.setattr(github, "profile", lambda u: (github.check_user(u), github.summarize(base, years))[1])
    monkeypatch.setattr(github, "repository", fake_repository)
    monkeypatch.setattr(github, "repositories", fake_repositories)
    def no_download(url, crop, width):  # the host allowlist still runs; only the download is faked
        raise cards._ImageUnavailable("offline")

    monkeypatch.setattr(cards, "_fetch_image", no_download)
    monkeypatch.delenv("ALLOWED_USERS", raising=False)


client = TestClient(server.app)

URLS = [
    "/api/stats?username=u",
    "/api/languages?username=u",
    "/api/commits?username=u",
    "/api/repo?username=u&repo=cohors&install=pip%20install%20cohors",
    "/api/repo?username=u&repo=principia&status=coming-soon",
    "/api/repo?username=u&repo=beta-thing",
    "/api/repos?username=u&repos=cohors,principia&soon=principia",
    "/api/repos?username=u&layout=list",
    "/api/hero?name=Ada%20Lovelace&eyebrow=Engineer&line=Builds%20things&tagline=a|b|c",
    "/api/section?title=Open%20source&caption=Tools",
    "/api/link?label=Email&value=a@b.c&icon=mail&hue=red",
    "/api/publication?title=A%20paper&venue=Journal&year=2025",
    "/api/product?name=App&tagline=Does%20it&feature=book:Cited%20answers&cta=Try%20it",
]


def parse(svg):
    root = ET.fromstring(svg)
    return root, [e for e in root.iter() if e.tag.endswith("text")]


@pytest.mark.parametrize("url", URLS)
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_widget_renders_readable_svg(url, theme):
    r = client.get(f"{url}&theme={theme}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "X-Widget-Error" not in r.headers, r.text[:400]
    assert "max-age" in r.headers["cache-control"]
    _, texts = parse(r.text)
    sizes = [float(t.get("font-size")) for t in texts]
    assert sizes and min(sizes) >= 11, f"text below 11px: {min(sizes)}"
    assert all((t.text or "").strip() not in ("None", "nan") for t in texts)


def test_png_output():
    r = client.get("/api/repo?username=u&repo=cohors&format=png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def text_of(svg):
    return " ".join(t.text or "" for t in parse(svg)[1])


def test_private_repo_is_not_found():
    r = client.get("/api/repo?username=u&repo=secret")
    assert r.headers.get("X-Widget-Error") == "1"
    assert "secret does one thing" not in r.text


def test_private_languages_never_counted():
    assert "Secretlang" not in client.get("/api/languages?username=u").text


def test_html_is_skipped_by_default_but_can_be_included():
    assert "HTML" not in text_of(client.get("/api/languages?username=u").text)
    assert "HTML" in text_of(client.get("/api/languages?username=u&skip=CSS").text)


def test_status_resolution():
    assert "Coming soon" in text_of(client.get("/api/repo?username=u&repo=principia&status=soon").text)
    assert "Beta" in text_of(client.get("/api/repo?username=u&repo=beta-thing").text)  # from the topic
    assert "v0.6.0" in text_of(client.get("/api/repo?username=u&repo=cohors").text)  # latest release
    assert "Beta" not in text_of(client.get("/api/repo?username=u&repo=beta-thing&status=none").text)


def test_carousel_cycles_every_repo_with_a_static_fallback():
    svg = client.get("/api/repos?username=u&repos=cohors,principia,beta-thing").text
    assert svg.count('class="slide s') == 3
    assert "@keyframes slide" in svg and ".s0{opacity:1}" in svg


@pytest.mark.parametrize("url", [
    "/api/stats?username=bad_name!",
    "/api/repo?username=u&repo=../etc",
    "/api/repo?username=u",
    "/api/nope",
    "/api/repo?username=u&repo=cohors&status=whatever",
    "/api/product?name=App&image=https://evil.example/x.png",
])
def test_bad_input_fails_soft(url):
    r = client.get(url)
    assert r.status_code == 200 and r.headers.get("X-Widget-Error") == "1"
    assert r.headers["cache-control"] == server.FAILED  # errors are never cached for long


def test_image_host_allowlist():
    for url in ["https://evil.example/x.png", "http://sushrutalgs.ai/x.png", "file:///etc/passwd"]:
        with pytest.raises(ValueError):
            cards.fetch_image(url, (0, 0, 1, 1), 340)
    assert cards.fetch_image("https://sushrutalgs.ai/x.png", (0, 0, 1, 1), 340) is None  # allowed, offline


def test_allowed_users(monkeypatch):
    monkeypatch.setenv("ALLOWED_USERS", "someone-else")
    r = client.get("/api/stats?username=u")
    assert r.headers.get("X-Widget-Error") == "1"


def test_index_describes_every_widget():
    assert set(client.get("/api").json()["widgets"]) == set(server.WIDGETS)


def test_streaks():
    d0 = date(2026, 1, 1)
    counts = [(d0 + timedelta(days=i), n) for i, n in enumerate([1, 1, 0, 1, 1, 1, 0])]
    (cur, _, _), (best, start, end) = github.streaks(counts)
    assert cur == 3 and best == 3 and start == d0 + timedelta(days=3)


def test_no_font_size_attribute_is_ever_missing():
    svg = client.get("/api/hero?name=X").text
    assert not re.search(r"<text(?![^>]*font-size)", svg)


# ---------------------------------------------------------------- credential safety

REAL_GQL = github.gql


def test_token_never_reaches_a_response(monkeypatch):
    """Regression: a token with a trailing newline made urllib raise with the header in its message."""
    monkeypatch.setattr(github, "_token", "gho_TESTSECRET0123456789\n")
    monkeypatch.setattr(github, "gql", REAL_GQL)
    monkeypatch.setattr(github, "profile", lambda u: github._fetch_profile(u))
    r = client.get("/api/stats?username=u")
    assert "TESTSECRET" not in r.text and "gho_" not in r.text and "bearer" not in r.text.lower()
    assert r.headers.get("X-Widget-Error") == "1" and r.headers["cache-control"] == "no-store"


def test_unexpected_errors_show_fixed_text(monkeypatch):
    def boom(u):
        raise ValueError("Invalid header value b'bearer gho_LEAKED'")
    monkeypatch.setattr(github, "profile", boom)
    r = client.get("/api/stats?username=u")
    assert "LEAKED" not in r.text and "unexpected error" in r.text


def test_scrub():
    from widgets.errors import scrub
    for secret in ["gho_abc123", "ghp_XYZ", "github_pat_11AB_cd", "Bearer abc.def", "token abc"]:
        cleaned = scrub(f"x {secret} y")
        assert secret not in cleaned and "[redacted]" in cleaned
