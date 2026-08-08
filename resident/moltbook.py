"""Moltbook API client.

Standard library only. The API key goes to exactly one host and nothing else —
their own documentation is emphatic about this, and it is the single most
important rule in the file. `_request` refuses to build a URL for any other
host rather than trusting the caller to pass the right one.

Also here: the verification puzzle. Moltbook holds new content hidden until the
agent solves an obfuscated arithmetic problem, and ten consecutive failures
suspend the account permanently. So the solver is conservative — it tries local
parsing first, escalates to the model only if that fails, and the caller is
expected to stop after two consecutive failures rather than burn the budget.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("resident.moltbook")

API_HOST = "https://www.moltbook.com"
API_BASE = f"{API_HOST}/api/v1"

# Ten consecutive verification failures suspend the account. We stop at two.
MAX_CONSECUTIVE_VERIFY_FAILURES = 2

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000,
}

ADD_WORDS = ("gains", "gain", "adds", "add", "plus", "speeds up", "increases",
             "faster by", "more")
SUB_WORDS = ("slows by", "slows", "loses", "lose", "minus", "drops", "decreases",
             "reduces", "less", "slower by")
MUL_WORDS = ("times", "multiplied by", "doubles", "triples")
DIV_WORDS = ("divided by", "halves", "split", "shared among", "per")


class MoltbookError(RuntimeError):
    pass


class SuspensionRisk(MoltbookError):
    """Raised when we stop ourselves before the platform stops us."""


@dataclass
class Challenge:
    verification_code: str
    challenge_text: str


def deobfuscate(text: str) -> str:
    """Strip the scattered symbols and alternating caps from a challenge.

    The obfuscation inserts punctuation inside words and randomises case:
        "A] lO^bSt-Er S[wImS aT/ tW]eNn-Tyy mE^tE[rS"
    Removing the injected characters and lowercasing recovers readable words.
    """
    cleaned = re.sub(r"[\]\[\^/\\|~`*_{}<>]", "", text)
    cleaned = re.sub(r"(?<=\w)-(?=\w)", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.lower().strip()


def _words_to_number(tokens: list[str]) -> float | None:
    total, current, seen = 0.0, 0.0, False
    for token in tokens:
        token = token.strip(".,;:!?")
        # "tWeNn-Tyy" style doubling survives deobfuscation as "twenny"/"tweenty"
        canonical = re.sub(r"(.)\1+", r"\1", token)
        value = NUMBER_WORDS.get(token) or NUMBER_WORDS.get(canonical)
        if value is None:
            continue
        seen = True
        if value == 100:
            current = (current or 1) * 100
        elif value == 1000:
            total += (current or 1) * 1000
            current = 0.0
        else:
            current += value
    return total + current if seen else None


def solve_locally(challenge_text: str) -> float | None:
    """Try to solve without spending a model call. Returns None if unsure."""
    text = deobfuscate(challenge_text)

    digits = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if len(digits) >= 2:
        numbers = digits[:2]
    else:
        segments = re.split(r"\b(?:and|then|but)\b", text)
        numbers = []
        for segment in segments:
            value = _words_to_number(segment.split())
            if value is not None:
                numbers.append(value)
        if len(numbers) < 2:
            return None
        numbers = numbers[:2]

    tail = text
    for words, op in (
        (DIV_WORDS, "/"), (MUL_WORDS, "*"), (SUB_WORDS, "-"), (ADD_WORDS, "+"),
    ):
        if any(word in tail for word in words):
            a, b = numbers
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            return a / b if b else None
    return None


class MoltbookClient:
    def __init__(self, api_key: str, timeout: float = 20.0) -> None:
        if not api_key:
            raise MoltbookError("no Moltbook API key configured")
        self.api_key = api_key
        self.timeout = timeout
        self.consecutive_verify_failures = 0

    # --- transport ------------------------------------------------------- #

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        # Belt and braces: the key must never leave this host, whatever a
        # caller (or a config file, or a compromised string) says.
        if urllib.parse.urlparse(url).netloc != "www.moltbook.com":
            raise MoltbookError(f"refusing to send credentials to {url}")

        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "moltbook-resident/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:400]
            if exc.code == 429:
                raise MoltbookError(f"rate limited: {detail}") from exc
            raise MoltbookError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MoltbookError(f"network error: {exc}") from exc

        return json.loads(body) if body else {}

    # --- reads ----------------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/agents/status")

    def home(self) -> dict[str, Any]:
        """The one-call dashboard. Tells us THAT there is activity.

        Note what this is used for: counting replies, not reading them. The
        posting lane never touches this. See triage.py for how reply text is
        handled when it is read at all.
        """
        return self._request("GET", "/home")

    def comments(self, post_id: str, limit: int = 35) -> dict[str, Any]:
        return self._request("GET", f"/posts/{post_id}/comments?sort=new&limit={limit}")

    def me(self) -> dict[str, Any]:
        """Our own profile — needed to tell our posts from other people's."""
        return self._request("GET", "/agents/me")

    def post(self, post_id: str) -> dict[str, Any]:
        """One post, including its author and body.

        `/home` reports activity on posts we were MENTIONED in as well as posts
        we wrote, and does not reliably distinguish them. Fetching the post and
        comparing author ids is the only dependable test — and it matters,
        because replying inside a stranger's thread is the behaviour this agent
        exists not to have.
        """
        return self._request("GET", f"/posts/{post_id}")

    def my_recent_posts(self, name: str, limit: int = 10) -> list[dict[str, Any]]:
        """Our own recent posts, straight from the platform.

        The local ledger cannot be the source of truth for "did I already post
        today": a laptop run and a CI run keep separate state files and never
        see each other, which is how three near-identical posts went out. The
        platform knows what we actually published, so ask it.

        Returns [] if no endpoint shape works — the caller then falls back to
        the local ledger rather than posting blind.
        """
        # /agents/me/posts is the shape that actually works — confirmed against
        # the live API. The name-based paths 404, but are kept as fallbacks in
        # case the endpoint changes.
        for path in (
            f"/agents/me/posts?limit={limit}",
            f"/agents/{name}/posts?limit={limit}",
            f"/agents/{name}?include=posts",
        ):
            try:
                data = self._request("GET", path)
            except MoltbookError:
                continue
            for key in ("posts", "items", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [p for p in value if isinstance(p, dict)]
            agent = data.get("agent")
            if isinstance(agent, dict) and isinstance(agent.get("posts"), list):
                return [p for p in agent["posts"] if isinstance(p, dict)]
        log.warning("could not read our own posts; falling back to the local ledger")
        return []

    def delete_post(self, post_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/posts/{post_id}")

    def mark_read(self, post_id: str) -> dict[str, Any]:
        return self._request("POST", f"/notifications/read-by-post/{post_id}")

    # --- writes ---------------------------------------------------------- #

    def set_description(self, description: str) -> dict[str, Any]:
        return self._request("PATCH", "/agents/me", {"description": description})

    def create_post(
        self, submolt: str, title: str, content: str
    ) -> tuple[str, Challenge | None]:
        response = self._request(
            "POST",
            "/posts",
            {"submolt_name": submolt, "title": title, "content": content},
        )
        post = response.get("post") or {}
        return post.get("id", ""), self._challenge_from(post)

    def create_comment(
        self, post_id: str, content: str, parent_id: str = ""
    ) -> tuple[str, Challenge | None]:
        payload: dict[str, Any] = {"content": content}
        if parent_id:
            payload["parent_id"] = parent_id
        response = self._request("POST", f"/posts/{post_id}/comments", payload)
        comment = response.get("comment") or response.get("post") or {}
        return comment.get("id", ""), self._challenge_from(comment)

    @staticmethod
    def _challenge_from(node: dict[str, Any]) -> Challenge | None:
        block = node.get("verification")
        if not isinstance(block, dict):
            return None
        code = block.get("verification_code")
        text = block.get("challenge_text")
        if not code or not text:
            return None
        return Challenge(verification_code=str(code), challenge_text=str(text))

    # --- verification ---------------------------------------------------- #

    def solve(self, challenge: Challenge, model_solver=None) -> bool:
        """Solve and submit. Stops the whole run rather than risk suspension."""
        if self.consecutive_verify_failures >= MAX_CONSECUTIVE_VERIFY_FAILURES:
            raise SuspensionRisk(
                f"{self.consecutive_verify_failures} consecutive verification "
                "failures — stopping before the platform suspends the account. "
                "Solve one by hand to confirm the format still matches."
            )

        answer = solve_locally(challenge.challenge_text)
        source = "local"
        if answer is None and model_solver is not None:
            answer = model_solver(deobfuscate(challenge.challenge_text))
            source = "model"

        if answer is None:
            self.consecutive_verify_failures += 1
            log.error("could not solve challenge: %s", challenge.challenge_text[:120])
            return False

        response = self._request(
            "POST",
            "/verify",
            {
                "verification_code": challenge.verification_code,
                "answer": f"{answer:.2f}",
            },
        )
        ok = bool(response.get("success"))
        if ok:
            self.consecutive_verify_failures = 0
            log.info("verification passed (%s solver, answer %.2f)", source, answer)
        else:
            self.consecutive_verify_failures += 1
            log.error("verification rejected: %s", response.get("error"))
        return ok
