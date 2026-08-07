"""Where LedgerMolty's material comes from.

It reads the public trust records of the other agents in the fleet through the
AiOps Enabler Query API (`GET /api/v1/query/agents/{slug}`), aggregates them,
and hands the writer a small flat dict of facts.

Two deliberate design choices:

**Schema tolerance.** The exact field names in the Query API response are not
pinned here. Each fact declares a list of candidate keys and `find_field`
searches the response recursively for the first one that exists. If a field is
nested under `metrics`, or named `successRate` rather than `success_rate`, this
still works. `describe()` dumps every scalar actually received, so a dry run
tells you the real shape rather than anyone guessing twice.

**Read-only, and the writer never sees the key.** This module holds the
consumer API key and hands the writer nothing but flattened facts. Nothing here
can publish.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("resident.material")

# For each fact, the field names it might arrive under. First match wins.
FIELD_CANDIDATES: dict[str, list[str]] = {
    "total_events": ["total_events", "totalEvents", "events_total", "event_count",
                     "total_tasks", "tasks_total", "run_count"],
    "success_rate": ["success_rate", "successRate", "success_percent",
                     "completion_rate", "task_success_rate"],
    "score": ["enabler_score", "enablerScore", "score", "trust_score"],
    "last_verified": ["last_verified", "lastVerified", "last_verified_at",
                      "verified_at", "last_event_at"],
    "failures": ["failures", "failure_count", "failed_tasks", "tasks_failed",
                 "error_count"],
    "rating": ["rating", "average_rating", "avg_rating", "human_rating"],
}

# Never surface these even in diagnostics.
SKIP_KEYS = {"api_key", "apikey", "secret", "token", "owner_email", "email",
             "claim_url", "webhook", "password"}


def find_field(node: Any, candidates: list[str], depth: int = 0) -> Any:
    """Search a nested response for the first candidate key that has a value."""
    if depth > 5 or not isinstance(node, dict):
        return None
    for key in candidates:
        if key in node and node[key] not in (None, "", [], {}):
            return node[key]
    for value in node.values():
        if isinstance(value, dict):
            found = find_field(value, candidates, depth + 1)
            if found is not None:
                return found
    return None


def flatten_scalars(node: Any, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """Every scalar in the response, for dry-run diagnostics only."""
    out: dict[str, Any] = {}
    if depth > 4 or not isinstance(node, dict):
        return out
    for key, value in node.items():
        if key.lower() in SKIP_KEYS:
            continue
        path = f"{prefix}{key}"
        if isinstance(value, (str, int, float, bool)):
            out[path] = value
        elif isinstance(value, dict):
            out.update(flatten_scalars(value, f"{path}.", depth + 1))
    return out


@dataclass
class AgentRecord:
    slug: str
    ok: bool = False
    fields: dict[str, Any] = field(default_factory=dict)
    raw_scalars: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class FleetReader:
    """Reads fleet trust records. Holds the consumer key; publishes nothing."""

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._auth_style: str | None = None

    def _headers(self, style: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "LedgerMolty/1.0 (+moltbook-resident)",
        }
        if not self.api_key:
            return headers
        if style == "bearer":
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif style == "x-api-key":
            headers["X-API-Key"] = self.api_key
        elif style == "x-aiops":
            headers["X-AiOps-Key"] = self.api_key
        return headers

    def _get(self, path: str) -> dict[str, Any]:
        """GET with auth-header autodetection.

        The docs say the Query API is keyed to a consumer API key but do not
        pin the header name. Try the three conventional forms once, then
        remember whichever the server accepted.
        """
        url = f"{self.base_url}{path}"
        styles = [self._auth_style] if self._auth_style else ["bearer", "x-api-key", "x-aiops"]
        last_error = ""

        for style in styles:
            request = urllib.request.Request(url, headers=self._headers(style))
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                if self._auth_style != style:
                    log.info("query API accepted the '%s' auth header", style)
                self._auth_style = style
                return json.loads(body or b"{}")
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                if exc.code in (401, 403):
                    continue  # wrong header name — try the next
                break
            except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break

        raise RuntimeError(last_error or "request failed")

    def read_agent(self, slug: str) -> AgentRecord:
        record = AgentRecord(slug=slug)
        try:
            data = self._get(f"/api/v1/query/agents/{slug}")
        except RuntimeError as exc:
            record.error = str(exc)
            log.warning("could not read %s: %s", slug, exc)
            return record

        payload = data.get("agent") if isinstance(data.get("agent"), dict) else data
        record.ok = True
        record.raw_scalars = flatten_scalars(payload)
        for name, candidates in FIELD_CANDIDATES.items():
            value = find_field(payload, candidates)
            if value is not None:
                record.fields[name] = value
        return record

    def read_fleet(self, slugs: list[str]) -> list[AgentRecord]:
        return [self.read_agent(slug) for slug in slugs]


# --------------------------------------------------------------------------- #


def as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def build_facts(records: list[AgentRecord]) -> dict[str, Any]:
    """Flatten fleet records into the narrow dict the writer receives.

    Only facts that survive this function can ever appear in a post. Anything
    the API returns that isn't mapped here is invisible to the writer — which
    is the point: it cannot casually mention a field nobody vetted.
    """
    live = [r for r in records if r.ok]
    if not live:
        return {}

    facts: dict[str, Any] = {
        "agents in the fleet": len(live),
        "agent names": ", ".join(r.slug for r in live),
    }

    totals = [n for n in (as_number(r.fields.get("total_events")) for r in live)
              if n is not None]
    if totals:
        facts["total runs recorded across the fleet"] = int(sum(totals))

    rated = [(n, r) for r, n in
             ((r, as_number(r.fields.get("success_rate"))) for r in live)
             if n is not None]
    if rated:
        rates = [n for n, _ in rated]
        facts["average success rate"] = f"{sum(rates) / len(rates):.1f}%"
        low_value, low_agent = min(rated, key=lambda pair: pair[0])
        if low_value < 100:
            facts["lowest success rate"] = f"{low_agent.slug} at {low_value:.1f}%"

    failures = [n for n in (as_number(r.fields.get("failures")) for r in live)
                if n is not None]
    if failures:
        facts["failures recorded"] = int(sum(failures))

    scores = [n for n in (as_number(r.fields.get("score")) for r in live)
              if n is not None]
    if scores:
        facts["average score"] = round(sum(scores) / len(scores), 1)

    verified = [str(r.fields["last_verified"]) for r in live
                if r.fields.get("last_verified")]
    if verified:
        facts["most recent verification"] = max(verified)

    missing = [r.slug for r in records if not r.ok]
    if missing:
        # State this plainly. An agent that went quiet is real information, and
        # hiding it would make the whole record less trustworthy.
        facts["agents with no readable record"] = ", ".join(missing)

    return facts


def describe(records: list[AgentRecord]) -> str:
    """Human-readable dump of what the API actually returned. Dry run only."""
    lines: list[str] = []
    for record in records:
        head = "ok" if record.ok else f"FAILED — {record.error}"
        lines.append(f"\n  {record.slug}: {head}")
        if record.fields:
            lines.append("    mapped facts:")
            for key, value in sorted(record.fields.items()):
                lines.append(f"      {key} = {value!r}")
        elif record.ok:
            lines.append("    mapped facts: NONE — field names differ, see below")
        if record.raw_scalars:
            lines.append(f"    all scalar fields received ({len(record.raw_scalars)}):")
            for key in sorted(record.raw_scalars):
                lines.append(f"      {key} = {record.raw_scalars[key]!r}")
    return "\n".join(lines) if lines else "  (no records)"
