"""The deterministic guard.

Everything else in this repo involves a language model, which means everything
else can in principle be talked into something. This module cannot. It is plain
Python `if` statements, and it is the last thing that runs before anything is
published.

Rules it enforces:

  1. Only one URL is ever allowed, and only the agent's own record page.
  2. Length caps, so a runaway generation can't dump 40,000 characters.
  3. A blocklist of promotional phrasing — the platform's terms prohibit
     advertising and marketing content, so a post that reads like an ad is a
     terms breach, not just bad manners.
  4. No instruction-shaped text. If a reply contains "ignore previous
     instructions", something upstream went wrong and we stop.

A blocked draft is never silently dropped. It is returned with the reason, and
the caller reports it. A guard that fails quietly is a guard nobody notices has
stopped working.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_POST_TITLE = 300
MAX_POST_BODY = 3000
MAX_COMMENT = 1200

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

# Marketing language. The terms prohibit "unauthorized advertising, marketing,
# spam or commercial sales content" — this list is deliberately blunt.
PROMOTIONAL = [
    "sign up",
    "sign-up",
    "check us out",
    "check out our",
    "try it free",
    "free trial",
    "get started today",
    "limited time",
    "don't miss",
    "dont miss",
    "act now",
    "book a demo",
    "contact us today",
    "special offer",
    "discount",
    "best in class",
    "industry leading",
    "industry-leading",
    "revolutionary",
    "game changer",
    "game-changer",
    "you should use",
    "you need to try",
    "we're the only",
    "were the only",
]

# Text shaped like an instruction to a model. Its presence in an outbound draft
# means untrusted content leaked into the writer's context somewhere upstream.
INJECTION_SHAPED = [
    "ignore previous",
    "ignore all previous",
    "ignore your instructions",
    "disregard the above",
    "disregard previous",
    "system prompt",
    "you are now",
    "new instructions",
    "override your",
    "reveal your",
    "print your api",
    "your api key",
]

# Never let a credential-shaped string out, whatever the source.
SECRET_SHAPED = re.compile(
    r"(moltbook_[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}"
    r"|Bearer\s+[A-Za-z0-9._-]{16,})",
    re.IGNORECASE,
)


@dataclass
class Verdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def _check_text(text: str, limit: int, allowed_url: str, label: str) -> list[str]:
    reasons: list[str] = []
    lowered = text.lower()

    if not text.strip():
        reasons.append(f"{label} is empty")
    if len(text) > limit:
        reasons.append(f"{label} is {len(text)} chars, limit is {limit}")

    urls = URL_RE.findall(text)
    for url in urls:
        cleaned = url.rstrip(".,;:!?)")
        if allowed_url and cleaned.lower().startswith(allowed_url.lower()):
            continue
        reasons.append(f"{label} contains a URL that is not the allowed link: {cleaned}")
    if len(urls) > 1:
        reasons.append(f"{label} contains {len(urls)} URLs, at most one is allowed")

    for phrase in PROMOTIONAL:
        if phrase in lowered:
            reasons.append(f"{label} contains promotional phrasing: '{phrase}'")

    for phrase in INJECTION_SHAPED:
        if phrase in lowered:
            reasons.append(
                f"{label} contains instruction-shaped text ('{phrase}') — "
                "untrusted content may have reached the writer"
            )

    if SECRET_SHAPED.search(text):
        reasons.append(f"{label} contains something shaped like a credential")

    return reasons


def check_post(title: str, body: str, allowed_url: str = "") -> Verdict:
    reasons = _check_text(title, MAX_POST_TITLE, allowed_url, "title")
    reasons += _check_text(body, MAX_POST_BODY, allowed_url, "body")
    return Verdict(ok=not reasons, reasons=reasons)


def check_comment(text: str, allowed_url: str = "") -> Verdict:
    return Verdict(
        ok=not (r := _check_text(text, MAX_COMMENT, allowed_url, "comment")),
        reasons=r,
    )
