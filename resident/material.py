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
    "total_events": ["tasks_handled", "total_events", "totalEvents", "events_total",
                     "event_count", "total_tasks", "tasks_total", "run_count"],
    "success_rate": ["success_rate", "successRate", "success_percent",
                     "completion_rate", "task_success_rate"],
    "score": ["enabler_score", "enablerScore", "score", "trust_score"],
    "last_verified": ["last_verified", "lastVerified", "last_verified_at",
                      "verified_at", "last_event_at"],
    "failures": ["failures", "failure_count", "failed_tasks", "tasks_failed",
                 "error_count"],
    "rating": ["rating_up_percent", "rating", "average_rating", "avg_rating"],
    "rating_count": ["rating_count", "ratings_count", "num_ratings"],
    "rank": ["rank", "position"],
    "verification": ["verification_level", "verificationLevel", "badge"],
}

# Where the Query API might live. A single-page frontend answers 200 with HTML
# for unknown paths, so a wrong base URL looks like success until you try to
# parse it — which is exactly the failure this list exists to diagnose.
BASE_URL_CANDIDATES = [
    "https://api.aiopsenabler.com",
    "https://aiopsenabler.com",
    "https://www.aiopsenabler.com",
]

# Path shapes seen in the wild: the version prefix may or may not repeat the
# `/api` segment once the API is on its own host.
PATH_CANDIDATES = [
    "/api/v1/query/agents/{slug}",
    "/v1/query/agents/{slug}",
    "/query/agents/{slug}",
]

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


def describe_structure(node: Any, prefix: str = "", depth: int = 0) -> dict[str, str]:
    """Report the SHAPE of everything that isn't a plain scalar.

    Without this, a field that arrives as an empty object, a null, or a list of
    time-series points simply vanishes from the diagnostics — and you conclude
    the API doesn't return it, when in fact it does and the reader is looking
    in the wrong place. That is exactly how the metrics gap was missed.
    """
    out: dict[str, str] = {}
    if depth > 4 or not isinstance(node, dict):
        return out
    for key, value in node.items():
        if key.lower() in SKIP_KEYS:
            continue
        path = f"{prefix}{key}"
        if value is None:
            out[path] = "null"
        elif isinstance(value, dict):
            if not value:
                out[path] = "empty object {}"
            else:
                out[path] = f"object with keys: {', '.join(list(value)[:8])}"
                out.update(describe_structure(value, f"{path}.", depth + 1))
        elif isinstance(value, list):
            if not value:
                out[path] = "empty list []"
            else:
                first = value[0]
                shape = (f"objects with keys: {', '.join(list(first)[:8])}"
                         if isinstance(first, dict) else type(first).__name__)
                out[path] = f"list of {len(value)} {shape}"
                if isinstance(first, dict):
                    out.update(describe_structure(first, f"{path}[0].", depth + 1))
    return out


@dataclass
class AgentRecord:
    slug: str
    ok: bool = False
    fields: dict[str, Any] = field(default_factory=dict)
    raw_scalars: dict[str, Any] = field(default_factory=dict)
    raw_structure: dict[str, str] = field(default_factory=dict)
    error: str = ""


class NotJsonError(RuntimeError):
    """The server answered, but with something that isn't JSON.

    Almost always means the request hit a single-page frontend rather than the
    API. Worth its own type so the caller can say that plainly instead of
    surfacing a bare JSONDecodeError, which sends people looking at their key.
    """


class FleetReader:
    """Reads fleet trust records. Holds the consumer key; publishes nothing."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 20.0,
        path_template: str = "/api/v1/query/agents/{slug}",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.path_template = path_template
        self.platform: dict[str, Any] = {}
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
                    content_type = response.headers.get("Content-Type", "")

                head = body[:200].lstrip().lower()
                if head.startswith(b"<!doctype") or head.startswith(b"<html") or (
                    "html" in content_type.lower()
                ):
                    raise NotJsonError(
                        f"got HTML from {url} (Content-Type: {content_type or 'unset'}) "
                        "— this is the website, not the API. The base URL or path is "
                        "wrong; run --describe-api to probe for the right one."
                    )

                if self._auth_style != style:
                    log.info("query API accepted the '%s' auth header", style)
                self._auth_style = style
                try:
                    return json.loads(body or b"{}")
                except json.JSONDecodeError as exc:
                    raise NotJsonError(
                        f"{url} returned {len(body)} bytes that are not JSON: {exc}"
                    ) from exc
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                if exc.code in (401, 403):
                    continue  # wrong header name — try the next
                break
            except NotJsonError:
                raise
            except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break

        raise RuntimeError(last_error or "request failed")

    def read_leaderboard(self) -> tuple[list[dict[str, Any]], str]:
        """One call for the whole fleet, plus platform-wide context.

        The per-agent profile endpoint (`/query/agents/{slug}`) advertises
        `metrics: platform_verified` in its provenance map but does not return
        a metrics object, so it cannot answer "how many runs, how many failed".
        The leaderboard can, for every agent at once — fewer calls and richer
        data, so it is the primary source.
        """
        data = self._get("/api/v1/leaderboard")
        entries = data.get("entries") or []
        return (
            [e for e in entries if isinstance(e, dict)],
            str(data.get("generated_at", "")),
        )

    def read_agent(self, slug: str) -> AgentRecord:
        record = AgentRecord(slug=slug)
        try:
            data = self._get(self.path_template.format(slug=slug))
        except RuntimeError as exc:
            record.error = str(exc)
            log.warning("could not read %s: %s", slug, exc)
            return record

        payload = data.get("agent") if isinstance(data.get("agent"), dict) else data
        record.ok = True
        record.raw_scalars = flatten_scalars(payload)
        record.raw_structure = describe_structure(payload)
        for name, candidates in FIELD_CANDIDATES.items():
            value = find_field(payload, candidates)
            if value is not None:
                record.fields[name] = value
        return record

    def read_fleet(self, slugs: list[str]) -> list[AgentRecord]:
        """Fleet records from the leaderboard, with a per-agent fallback."""
        try:
            entries, generated_at = self.read_leaderboard()
        except RuntimeError as exc:
            log.warning("leaderboard unavailable (%s); falling back per agent", exc)
            return [self.read_agent(slug) for slug in slugs]

        self.platform = platform_context(entries, generated_at)
        by_slug = {str(e.get("slug", "")): e for e in entries}
        records: list[AgentRecord] = []

        for slug in slugs:
            entry = by_slug.get(slug)
            if entry is None:
                records.append(
                    AgentRecord(slug, error="not on the leaderboard (unpublished?)")
                )
                continue
            record = AgentRecord(slug, ok=True)
            record.raw_scalars = flatten_scalars(entry)
            record.raw_structure = describe_structure(entry)
            for name, candidates in FIELD_CANDIDATES.items():
                value = find_field(entry, candidates)
                if value is not None:
                    record.fields[name] = value
            records.append(record)
        return records

    def probe(self, slug: str) -> list[str]:
        """Try every base URL and path shape; report what each one did.

        Called when every read failed, so the run that discovers the problem
        also tells you the fix instead of costing another round trip.
        """
        findings: list[str] = []
        for base in BASE_URL_CANDIDATES:
            for path in PATH_CANDIDATES:
                trial = FleetReader(base, self.api_key, timeout=10.0,
                                    path_template=path)
                url = f"{base}{path.format(slug=slug)}"
                try:
                    data = trial._get(path.format(slug=slug))
                except NotJsonError:
                    findings.append(f"  html   {url}")
                except RuntimeError as exc:
                    findings.append(f"  {str(exc)[:22]:<6} {url}")
                else:
                    keys = ", ".join(list(data)[:6]) if isinstance(data, dict) else "?"
                    findings.append(f"  JSON   {url}   <-- USE THIS  (keys: {keys})")
        return findings


# --------------------------------------------------------------------------- #


def platform_context(entries: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    """Aggregate, anonymous context about the whole directory.

    Only counts. Other people's agents are never named — they did not agree to
    be discussed by a stranger's bot, and "14 of 20 listed agents carry
    telemetry" is the interesting claim anyway, not who they are.
    """
    if not entries:
        return {}
    levels = [str(e.get("verification_level", "")) for e in entries]
    return {
        "listed": len(entries),
        "instrumented": sum(1 for level in levels if level == "instrumented"),
        "self_reported": sum(1 for level in levels if level == "self_reported"),
        "with_ratings": sum(1 for e in entries if (e.get("rating_count") or 0) > 0),
        "generated_at": generated_at,
    }


def as_rate(value: Any) -> float | None:
    """Normalize a success rate to a percentage.

    The leaderboard reports a 0-1 fraction (1.0, 0.9425675675675675) while
    other surfaces may use 0-100. Treat anything at or below 1 as a fraction —
    a real agent at exactly 1% success would be indistinguishable, and does not
    exist in practice.
    """
    number = as_number(value)
    if number is None:
        return None
    return number * 100 if number <= 1 else number


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


def build_facts(
    records: list[AgentRecord], platform: dict[str, Any] | None = None
) -> dict[str, Any]:
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
             ((r, as_rate(r.fields.get("success_rate"))) for r in live)
             if n is not None]
    if rated:
        rates = [n for n, _ in rated]
        facts["average success rate"] = f"{sum(rates) / len(rates):.2f}%"
        low_value, low_agent = min(rated, key=lambda pair: pair[0])
        if low_value < 100:
            facts["lowest success rate"] = f"{low_agent.slug} at {low_value:.2f}%"
        perfect = [r.slug for n, r in rated if n >= 100]
        if perfect:
            facts["agents at 100%"] = ", ".join(perfect)

    failures = [n for n in (as_number(r.fields.get("failures")) for r in live)
                if n is not None]
    if failures:
        facts["failures recorded"] = int(sum(failures))
    else:
        # The leaderboard gives runs and a rate but not a failure count.
        # Deriving it is honest arithmetic, and the failures are the part of
        # the record most worth stating out loud.
        derived = 0.0
        for record in live:
            runs = as_number(record.fields.get("total_events"))
            rate = as_rate(record.fields.get("success_rate"))
            if runs is not None and rate is not None:
                derived += runs * (100 - rate) / 100
        if derived >= 0.5:
            facts["failed runs (derived from rate)"] = round(derived)

    scores = [n for n in (as_number(r.fields.get("score")) for r in live)
              if n is not None]
    if scores:
        facts["average score"] = round(sum(scores) / len(scores), 1)

    verified = [str(r.fields["last_verified"]) for r in live
                if r.fields.get("last_verified")]
    if verified:
        facts["most recent verification"] = max(verified)

    ranks = [n for n in (as_number(r.fields.get("rank")) for r in live)
             if n is not None]
    if ranks:
        facts["best rank held"] = int(min(ranks))

    missing = [r.slug for r in records if not r.ok]
    if missing:
        # State this plainly. An agent that went quiet is real information, and
        # hiding it would make the whole record less trustworthy.
        facts["agents with no readable record"] = ", ".join(missing)

    if platform and platform.get("listed"):
        facts["agents listed on the directory overall"] = platform["listed"]
        facts["of those, how many report telemetry"] = platform["instrumented"]
        facts["of those, how many are self-reported only"] = platform["self_reported"]
        facts["of those, how many have any human rating"] = platform["with_ratings"]

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
            lines.append(f"    scalar fields ({len(record.raw_scalars)}):")
            for key in sorted(record.raw_scalars):
                lines.append(f"      {key} = {record.raw_scalars[key]!r}")
        if record.raw_structure:
            lines.append(f"    non-scalar fields ({len(record.raw_structure)}):")
            for key in sorted(record.raw_structure):
                lines.append(f"      {key}: {record.raw_structure[key]}")
    return "\n".join(lines) if lines else "  (no records)"
