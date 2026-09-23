"""Errors whose messages are safe to show on a public error card, and a scrubber for the rest."""

import re


class BadRequest(ValueError):
    """Invalid input. The message is written by us and is safe to show."""


# Anything shaped like a credential never reaches a response, whatever raised it.
SECRET = re.compile(r"(gh[pousr]_\w+|github_pat_\w+|bearer\s+\S+|token\s+\S+)", re.IGNORECASE)


def scrub(message):
    return SECRET.sub("[redacted]", message)
