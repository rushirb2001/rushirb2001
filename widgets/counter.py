"""Profile view counts, read through komarev.com, the counter the original badge used.

Every call records one view there and returns the new total, so the count carries
on from the old badge. The widget serving this is never cached, so each profile
visit reaches it exactly once, the same way the badge counted.
"""

import re
import threading
import urllib.request

from . import github

URL = "https://komarev.com/ghpvc/?username={}&style=flat&abbreviated=false"
NUMBER = re.compile(r">([0-9][0-9,]*)<")

_last, _lock = {}, threading.Lock()


def hit(username):
    """Record a view and return the total; on failure, the last known total (or None)."""
    github.check_user(username)  # the allowlist also stops anyone inflating other people's counts
    key = username.lower()
    try:
        req = urllib.request.Request(URL.format(username), headers={"User-Agent": "rushirb2001-widgets"})
        with urllib.request.urlopen(req, timeout=5) as r:
            svg = r.read(64_000).decode("utf-8", "replace")
        count = int(NUMBER.findall(svg)[-1].replace(",", ""))
    except Exception:
        with _lock:
            return _last.get(key)
    with _lock:
        _last[key] = count
    return count
