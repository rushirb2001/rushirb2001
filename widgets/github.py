"""GitHub data for the widgets: GraphQL queries, a small TTL cache, and shaping.

The server's token may be able to see private repositories. Nothing private is
ever returned from here: repository lookups on a private repo raise NotFound,
and listings and language totals only count public repositories.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

USERNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

TTL = 600  # seconds a GitHub response is reused inside one warm instance


class NotFound(Exception):
    pass


class Upstream(Exception):
    pass


# ---------------------------------------------------------------- transport

_token_lock = threading.Lock()
_token = None


def token():
    global _token
    with _token_lock:
        if _token is None:
            _token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if not _token:  # local development: fall back to the gh CLI login
                _token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
        return _token


def gql(query, **variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request("https://api.github.com/graphql", data=body, headers={
        "Authorization": f"bearer {token()}", "Content-Type": "application/json",
        "User-Agent": "rushirb2001-widgets"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise Upstream(f"GitHub request failed: {type(exc).__name__}") from None
    errors = payload.get("errors") or []
    if any(e.get("type") == "NOT_FOUND" for e in errors):
        raise NotFound("not found")
    if errors or not payload.get("data"):
        raise Upstream("GitHub API error")
    return payload["data"]


_cache, _cache_lock = {}, threading.Lock()


def cached(key, load):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    value = load()
    with _cache_lock:
        if len(_cache) > 256:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:64]:
                del _cache[k]
        _cache[key] = (now + TTL, value)
    return value


def check_user(username):
    if not USERNAME.match(username or ""):
        raise ValueError("invalid username")
    allowed = {u.strip().lower() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()}
    if allowed and username.lower() not in allowed:
        raise PermissionError("this widget server only renders for approved users")


def check_repo(name):
    if not REPO_NAME.match(name or ""):
        raise ValueError("invalid repository name")


# ---------------------------------------------------------------- profile

PROFILE_QUERY = """
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

YEAR_FRAGMENT = """
fragment Year on ContributionsCollection {
  totalCommitContributions totalPullRequestContributions totalIssueContributions
  contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } }
}"""


def _fetch_profile(username):
    base = gql(PROFILE_QUERY, login=username)["user"]
    if base is None:
        raise NotFound("user not found")
    now = datetime.now(timezone.utc)
    years = []
    for y in sorted(base["contributionsCollection"]["contributionYears"]):
        end = now if y == now.year else datetime(y, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        years.append(f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", '
                     f'to: "{end:%Y-%m-%dT%H:%M:%SZ}") {{ ...Year }}')
    detail = gql(f'query($login: String!) {{ user(login: $login) {{ {" ".join(years)} }} }} {YEAR_FRAGMENT}',
                 login=username)["user"]
    return summarize(base, detail)


def profile(username):
    check_user(username)
    return cached(("profile", username.lower()), lambda: _fetch_profile(username))


def streaks(counts):
    """Current and longest runs of active days, with their date ranges, over sorted (date, n)."""
    run_start, best = None, (0, None, None)
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


def summarize(base, years):
    cc = base["contributionsCollection"]
    recent = [d for w in cc["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
    joined = datetime.fromisoformat(base["createdAt"]).date()
    today = date.fromisoformat(recent[-1]["date"]) if recent else date.today()
    daily = {}
    for y in years.values():
        for w in y["contributionCalendar"]["weeks"]:
            for d in w["contributionDays"]:
                day = date.fromisoformat(d["date"])
                if joined <= day <= today:
                    daily[day] = d["contributionCount"]
    current, longest = streaks(sorted(daily.items()))

    # Language bytes and commit counts per public, non-fork repo, for the languages card.
    repo_langs = []
    for entry in cc["commitContributionsByRepository"]:
        repo = entry["repository"]
        if repo["isPrivate"] or repo["isFork"]:
            continue
        repo_langs.append((entry["contributions"]["totalCount"],
                           [(e["node"]["name"], e["node"]["color"], e["size"]) for e in repo["languages"]["edges"]]))

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
        "repo_langs": repo_langs,
        "built": datetime.now(timezone.utc),
    }


def languages(summary, skip, count):
    """Languages weighted by the last year's commits, split by each repo's byte share."""
    totals, colors = defaultdict(float), {}
    repos = 0
    for commits, langs in summary["repo_langs"]:
        kept = [(n, c, s) for n, c, s in langs if n not in skip]
        size = sum(s for _, _, s in kept)
        if not size:
            continue
        repos += 1
        for name, color, s in kept:
            totals[name] += commits * s / size
            colors[name] = color or "#8b949e"
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    total = sum(totals.values()) or 1
    top = [(k, v, colors[k]) for k, v in ranked[:count] if v / total >= 0.01]
    rest = total - sum(v for _, v, _ in top)
    return top, rest, total, repos


# ---------------------------------------------------------------- repositories

REPO_FIELDS = """
fragment Repo on Repository {
  name description url homepageUrl stargazerCount forkCount pushedAt
  isPrivate isFork isArchived
  licenseInfo { spdxId }
  primaryLanguage { name color }
  repositoryTopics(first: 20) { nodes { topic { name } } }
  latestRelease { tagName }
}"""


def _public(repo):
    if repo is None or repo["isPrivate"]:
        raise NotFound("repository not found")
    repo["topics"] = [n["topic"]["name"] for n in repo.pop("repositoryTopics")["nodes"]]
    return repo


def repository(username, name):
    check_user(username)
    check_repo(name)

    def load():
        data = gql("query($o: String!, $n: String!) { repository(owner: $o, name: $n) { ...Repo } }"
                   + REPO_FIELDS, o=username, n=name)
        return _public(data["repository"])

    return cached(("repo", username.lower(), name.lower()), load)


def repositories(username, names=None, limit=6):
    """Named public repos in the given order, or the owner's top public repos by stars."""
    check_user(username)
    if names:
        for n in names:
            check_repo(n)

        def load():
            aliases = " ".join(f"r{i}: repository(owner: $o, name: {json.dumps(n)}) {{ ...Repo }}"
                               for i, n in enumerate(names))
            data = gql(f"query($o: String!) {{ {aliases} }}" + REPO_FIELDS, o=username)
            out = []
            for i in range(len(names)):
                try:
                    out.append(_public(data.get(f"r{i}")))
                except NotFound:
                    continue  # a missing or private repo drops out of the set
            return out

        return cached(("repos", username.lower(), tuple(n.lower() for n in names)), load)

    def load_top():
        data = gql("""query($o: String!) { user(login: $o) {
            repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC, first: 50,
                         orderBy: {field: STARGAZERS, direction: DESC}) { nodes { ...Repo } } } }"""
                   + REPO_FIELDS, o=username)
        if data["user"] is None:
            raise NotFound("user not found")
        repos = [_public(r) for r in data["user"]["repositories"]["nodes"]]
        # The profile README repo is not a project.
        repos = [r for r in repos if r["name"].lower() != username.lower() and not r["isArchived"]]
        # Most stars first; among equals, the most recently pushed (stable sort, two passes).
        repos.sort(key=lambda r: r["pushedAt"], reverse=True)
        repos.sort(key=lambda r: -r["stargazerCount"])
        return repos

    return cached(("top", username.lower()), load_top)[:limit]
