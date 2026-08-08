#!/usr/bin/env python3
"""
moltbook-resident — an agent that posts its own verified record to Moltbook.

Two lanes, deliberately separated:

  POST lane (autonomous)
      Reads the agent's own record, writes a post about it, publishes.
      Touches no external text at all, so there is nothing to inject.

  REPLY lane (draft-only by default)
      Reads replies, classifies each one into a small label, and drafts an
      answer from the label plus the agent's own facts. The writer never sees
      the reply text. In `draft` mode nothing is published — drafts are written
      to a file for a human to read.

Outcome semantics: a run that does its work is a SUCCESS whether or not it had
anything to post. Only a run that could not do its work is a FAILURE.

Usage:
    python agent.py --once --dry-run           # see everything, publish nothing
    python agent.py --once                      # post lane, live
    python agent.py --once --replies            # also draft replies
    python agent.py --setup-profile             # write the disclosure line
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from resident import compose, guard, material, triage
from resident.moltbook import MoltbookClient, MoltbookError, SuspensionRisk
from resident.state import State

# Windows consoles default to a legacy codepage, so an em-dash in a draft
# renders as garbage and you cannot tell whether the text or just the display
# is broken. The payload was always fine; this makes the preview trustworthy.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

VERSION = "1.1.0"
AGENT = "moltbook-resident"
log = logging.getLogger(AGENT)

DEFAULTS: dict[str, Any] = {
    "agent_name": "LedgerMolty",
    "submolt": "general",
    "record_url": "",
    "enabler": {
        "base_url": "https://aiopsenabler.com",
        "fleet": [],
        "key_env": "AIOPS_QUERY_KEY",
    },
    "posting": {"enabled": True, "period": "day", "min_runs_to_post": 1},
    "replies": {"mode": "draft", "max_per_run": 5},
    "model": {"name": "claude-sonnet-4-6", "max_retries": 2},
    "state_path": "state/resident.json",
    "drafts_path": "state/drafts.md",
    "out": "result.json",
}


# --------------------------------------------------------------------------- #
# model access
# --------------------------------------------------------------------------- #


class Anthropic:
    """Minimal Messages API client. Standard library only."""

    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str, model: str, timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def __call__(self, system: str, user: str, max_tokens: int = 500) -> str:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        request = urllib.request.Request(
            self.URL,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read())
        return "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )


def make_puzzle_solver(model: Anthropic):
    """Fallback solver for verification challenges the local parser misses."""

    def solve(readable_text: str) -> float | None:
        try:
            raw = model(
                "You solve a single arithmetic word problem. Reply with ONLY the "
                "number, to two decimal places. No words, no working.",
                readable_text,
                40,
            )
            cleaned = "".join(c for c in raw if c.isdigit() or c in ".-")
            return float(cleaned) if cleaned else None
        except (ValueError, urllib.error.URLError, OSError, RuntimeError) as exc:
            log.warning("model puzzle solver failed: %s", exc)
            return None

    return solve


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path) -> dict[str, Any]:
    config = dict(DEFAULTS)
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            config = deep_merge(DEFAULTS, yaml.safe_load(fh) or {})
    if value := os.environ.get("RESIDENT_AGENT_NAME"):
        config["agent_name"] = value
    if value := os.environ.get("RESIDENT_RECORD_URL"):
        config["record_url"] = value
    return config


def period_key(granularity: str) -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d") if granularity == "day" else now.strftime("%Y-W%W")


# --------------------------------------------------------------------------- #
# lanes
# --------------------------------------------------------------------------- #


def make_reader(config: dict[str, Any]) -> material.FleetReader:
    key_env = config["enabler"]["key_env"]
    key = os.environ.get(key_env, "")
    if not key:
        log.warning("%s is not set — the query API will reject every read", key_env)
    elif key.endswith("...") or "paste" in key.lower() or "your-key" in key.lower():
        log.error(
            "%s looks like a placeholder (%r) rather than a real key", key_env, key
        )
    return material.FleetReader(
        config["enabler"]["base_url"],
        key,
        path_template=config["enabler"].get(
            "path_template", "/api/v1/query/agents/{slug}"
        ),
    )


def post_lane(
    client: MoltbookClient,
    model: Anthropic,
    reader: material.FleetReader,
    state: State,
    config: dict[str, Any],
    dry_run: bool,
    result: dict[str, Any],
) -> None:
    """Autonomous. Reads only the fleet's own trust records; publishes a post."""
    period = period_key(config["posting"]["period"])

    # Ask the PLATFORM whether we already posted, not just the local ledger.
    # A laptop run and a CI run keep separate state files and never see each
    # other, which is how duplicate posts went out. Moltbook is the only place
    # that knows what was actually published.
    recent_titles = list(state.recent_titles)
    try:
        published = client.my_recent_posts(config["agent_name"])
    except MoltbookError as exc:
        log.warning("could not check published posts (%s)", exc)
        published = []

    for post in published:
        created = str(post.get("created_at", ""))[:10]
        title = str(post.get("title", ""))
        if title:
            recent_titles.append(title)
        if created and created == period[:10]:
            log.info("already posted on %s (platform says so) — nothing to do", created)
            result["metrics"]["posts_skipped_duplicate"] = 1
            state.mark_posted(period)
            for remembered in recent_titles:
                state.remember_title(remembered)
            return

    if state.already_posted(period):
        log.info("already posted for %s — nothing to do", period)
        result["metrics"]["posts_skipped_duplicate"] = 1
        return

    fleet = config["enabler"]["fleet"]
    if not fleet:
        result["errors"].append("enabler.fleet is empty — nothing to narrate")
        return

    records = reader.read_fleet(fleet)
    result["metrics"]["fleet_agents_read"] = sum(1 for r in records if r.ok)
    result["metrics"]["fleet_agents_unreadable"] = sum(1 for r in records if not r.ok)
    facts = material.build_facts(records, reader.platform)

    if dry_run:
        print("\n--- what the Query API returned ---" + material.describe(records))

    if not facts:
        log.warning("no facts available — skipping the post rather than inventing one")
        result["errors"].append(
            "no readable fleet records; check AIOPS_QUERY_KEY and that the "
            "agents are published"
        )
        return

    if dry_run:
        # Print the facts, not just the raw API dump. Without this you cannot
        # check a figure in the draft against what the agent was actually told,
        # which makes reviewing a post guesswork.
        print("\n--- facts given to the writer (every figure must come from here) ---")
        for key, value in sorted(facts.items()):
            print(f"  {key}: {value}")

    attempts = max(1, int(config["model"]["max_retries"]) + 1)
    title = body = ""
    verdict = guard.Verdict(ok=False, reasons=["not composed"])

    for attempt in range(1, attempts + 1):
        title, body = compose.compose_post(facts, model, recent_titles)
        result["metrics"]["guard_checks"] += 1

        verdict = guard.check_post(title, body, allowed_url=config["record_url"])
        numbers = guard.check_numbers(f"{title} {body}", facts)
        novelty = guard.check_title_novelty(title, recent_titles)
        result["metrics"]["guard_checks"] += 2

        if verdict.ok and numbers.ok and novelty.ok:
            verdict = novelty
            break

        if verdict.ok and numbers.ok and not novelty.ok:
            result["metrics"]["title_retries"] += 1
            log.warning("attempt %d: %s — recomposing", attempt, novelty.reasons[0])
            verdict = novelty
            continue

        # A numeric slip is worth another try — the model usually gets it right
        # on a re-roll, and a blocked post means no post at all that day.
        # A policy breach (a bad link, promotional phrasing) is not retried:
        # that is a prompt problem, not a dice roll.
        if verdict.ok and not numbers.ok:
            result["metrics"]["numeric_retries"] += 1
            log.warning(
                "attempt %d stated a figure not in the facts (%s) — recomposing",
                attempt, "; ".join(numbers.reasons),
            )
            verdict = numbers
            continue
        break

    if not verdict.ok:
        result["metrics"]["guard_blocks"] += 1
        result["blocked"].append({"kind": "post", "reasons": verdict.reasons})
        log.error("post blocked by guard: %s", "; ".join(verdict.reasons))
        return

    print(f"\n--- draft post ---\nTITLE: {title}\nBODY:  {body}\n")

    if dry_run:
        result["metrics"]["posts_dry_run"] += 1
        return

    post_id, challenge = client.create_post(config["submolt"], title, body)
    if challenge:
        if not client.solve(challenge, make_puzzle_solver(model)):
            # An unverified post is hidden from every feed and profile, and the
            # platform's own /agents/me/posts filters it out — so it is
            # invisible to the duplicate check too. Leaving it there means an
            # orphan nobody can read that we cannot see either. Remove it and
            # fail the run loudly, rather than reporting a success that isn't.
            result["metrics"]["verification_failures"] += 1
            result["errors"].append(
                "verification challenge failed — the post would have been "
                "invisible, so it was withdrawn"
            )
            try:
                if post_id:
                    client.delete_post(post_id)
                    result["errors"].append(f"withdrew unverified post {post_id}")
            except MoltbookError as exc:
                result["errors"].append(f"could not withdraw the orphan post: {exc}")
            return

    state.mark_posted(period)
    state.remember_title(title)
    result["metrics"]["posts_published"] += 1
    result["published"].append({"kind": "post", "id": post_id, "title": title})


def _handle(
    untrusted: str,
    label_source: str,
    facts: dict[str, Any],
    model: Anthropic,
    config: dict[str, Any],
    result: dict[str, Any],
) -> tuple[str, Any]:
    """Triage one untrusted message and draft a reply. Returns (draft, label).

    THE AIRGAP LIVES HERE. `untrusted` goes into triage and nowhere else — note
    that compose_reply receives `label`, never the text. Everything downstream
    of this function has only seen a three-field label and our own facts.
    """
    label = triage.triage(untrusted, model)
    result["metrics"]["comments_triaged"] += 1
    if label.hostile:
        result["metrics"]["comments_flagged_hostile"] += 1
    if not label.worth_answering:
        return "", label

    draft = compose.compose_reply(label, facts, model)
    if not draft:
        return "", label

    verdict = guard.check_comment(draft, allowed_url=config["record_url"])
    numbers = guard.check_numbers(draft, facts)
    result["metrics"]["guard_checks"] += 2
    if not verdict.ok or not numbers.ok:
        result["metrics"]["guard_blocks"] += 1
        result["blocked"].append({
            "kind": f"reply/{label_source}",
            "reasons": (verdict.reasons or []) + (numbers.reasons or []),
        })
        return "", label

    return draft, label


def reply_lane(
    client: MoltbookClient,
    model: Anthropic,
    reader: material.FleetReader,
    state: State,
    config: dict[str, Any],
    dry_run: bool,
    result: dict[str, Any],
) -> None:
    """Answer questions — but only where answering is invited.

    Moltbook's /home lumps two different things into activity_on_your_posts:
    comments on posts WE wrote, and posts by other agents that MENTION us.
    They call for opposite behaviour:

      our post      -> read its comments and reply to them. Our thread, our
                       conversation, and the post invited the challenge.
      mention       -> read the POST ITSELF and reply to it. Someone asked us
                       something directly. We never touch the other comments in
                       that thread: replying to strangers talking amongst
                       themselves is barging in, which is the one behaviour
                       this agent must not have.

    The earlier version treated everything as the first case, so it read
    comments in a stranger's thread and missed the question in the post body.
    """
    mode = config["replies"]["mode"]
    if mode == "off":
        return

    try:
        my_id = str((client.me().get("agent") or client.me()).get("id", ""))
    except MoltbookError as exc:
        log.warning("could not identify self (%s); skipping the reply lane", exc)
        result["errors"].append(f"agents/me failed: {exc}")
        return

    activity = client.home().get("activity_on_your_posts") or []
    if not activity:
        log.info("nothing addressed to us")
        return

    facts = material.build_facts(
        reader.read_fleet(config["enabler"]["fleet"]), reader.platform
    )
    drafts: list[str] = []
    handled = 0
    budget = int(config["replies"]["max_per_run"])

    for item in activity:
        if handled >= budget:
            break
        post_id = str(item.get("post_id", ""))
        if not post_id:
            continue

        try:
            post = client.post(post_id).get("post") or {}
        except MoltbookError as exc:
            log.warning("could not read post %s: %s", post_id, exc)
            continue

        ours = str(post.get("author_id", "")) == my_id and my_id != ""
        targets: list[tuple[str, str, str]] = []  # (kind, id, text)

        if ours:
            for comment in (client.comments(post_id).get("comments") or []):
                cid = str(comment.get("id", ""))
                if not cid or state.already_replied(cid):
                    continue
                if str(comment.get("author_id", "")) == my_id:
                    continue  # never reply to ourselves
                targets.append(("comment", cid, str(comment.get("content", ""))))
        else:
            if state.already_replied_to_post(post_id):
                continue
            # Only the post body, and only because it named us.
            targets.append((
                "mention", post_id,
                f"{post.get('title', '')}\n\n{post.get('content', '')}",
            ))

        for kind, target_id, untrusted in targets:
            if handled >= budget:
                break

            draft, label = _handle(
                untrusted, kind, facts, model, config, result
            )
            if not draft:
                if kind == "comment":
                    state.mark_replied(target_id)
                else:
                    state.mark_replied_to_post(target_id)
                continue

            handled += 1
            author = post.get("author", {}).get("name", "?") if kind == "mention" else "?"
            drafts.append(
                f"## {kind} draft — topic: {label.topic}"
                + (f" — from {author}" if kind == "mention" else "")
                + f"\npost: https://www.moltbook.com/post/{post_id}\n\n{draft}\n"
            )

            if mode == "auto" and not dry_run:
                parent = target_id if kind == "comment" else ""
                _, challenge = client.create_comment(post_id, draft, parent)
                if challenge and not client.solve(
                    challenge, make_puzzle_solver(model)
                ):
                    result["errors"].append("reply verification failed")
                    continue
                result["metrics"]["replies_published"] += 1
                result["published"].append({"kind": "reply", "post_id": post_id})

            if kind == "comment":
                state.mark_replied(target_id)
            else:
                state.mark_replied_to_post(target_id)

        # Clear the notification so the same item does not resurface every run.
        if mode == "auto" and not dry_run:
            try:
                client.mark_read(post_id)
            except MoltbookError:
                pass

    if drafts:
        path = Path(config["drafts_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# {stamp}\n\n" + "\n".join(drafts))
        result["metrics"]["replies_drafted"] = len(drafts)
        print(f"\n{len(drafts)} reply draft(s) written to {path}")
        for draft in drafts:
            print("\n" + draft)


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post a verified record to Moltbook.")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--once", action="store_true", help="run one cycle")
    parser.add_argument("--dry-run", action="store_true", help="compose but never publish")
    parser.add_argument("--replies", action="store_true", help="also run the reply lane")
    parser.add_argument("--setup-profile", action="store_true",
                        help="write the disclosure line to the Moltbook profile")
    parser.add_argument("--describe-api", action="store_true",
                        help="print exactly what the Query API returns, then exit — "
                             "run this first to confirm the field mapping")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s %(message)s",
    )

    config = load_config(Path(args.config))
    result: dict[str, Any] = {
        "agent": AGENT,
        "version": VERSION,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "outcome": "success",
        "summary": "",
        "metrics": {
            "posts_published": 0, "posts_dry_run": 0, "posts_skipped_duplicate": 0,
            "replies_drafted": 0, "replies_published": 0,
            "comments_triaged": 0, "comments_flagged_hostile": 0,
            "guard_checks": 0, "guard_blocks": 0,
            "fleet_agents_read": 0, "fleet_agents_unreadable": 0,
            "numeric_retries": 0, "title_retries": 0,
            "verification_failures": 0,
        },
        "published": [], "blocked": [], "errors": [],
    }

    state = State(config["state_path"])
    reader = make_reader(config)

    if args.describe_api:
        fleet = config["enabler"]["fleet"]
        records = reader.read_fleet(fleet)
        print("--- what the Query API returned ---" + material.describe(records))

        facts = material.build_facts(records, reader.platform)
        if facts:
            print("\n--- facts the writer would receive ---")
            for key, value in sorted(facts.items()):
                print(f"  {key}: {value}")
            return 0

        # Nothing readable. Find the right endpoint rather than making the user
        # guess: the run that surfaces the problem should also surface the fix.
        print("\n--- no readable records; probing for the right endpoint ---")
        print("  (a 200 with HTML means the website answered, not the API)\n")
        for line in reader.probe(fleet[0] if fleet else "dns-drift"):
            print(line)
        print(
            "\nIf one line says USE THIS, put its host in config.yml as "
            "enabler.base_url,\nand its path (with {slug}) as "
            "enabler.path_template.\nIf every line says html or 401, the key or "
            "the auth header is the problem."
        )
        return 1

    try:
        client = MoltbookClient(os.environ.get("MOLTBOOK_API_KEY", ""))
        client.consecutive_verify_failures = state.verify_failures
        model = Anthropic(
            os.environ.get("ANTHROPIC_API_KEY", ""), config["model"]["name"]
        )

        if args.setup_profile:
            description = compose.profile_description(
                config["agent_name"], config["record_url"]
            )
            verdict = guard.check_comment(description, allowed_url=config["record_url"])
            if not verdict:
                raise MoltbookError(f"profile line blocked: {verdict.reasons}")
            if not args.dry_run:
                client.set_description(description)
            print(f"profile description: {description}")
        else:
            if config["posting"]["enabled"]:
                post_lane(client, model, reader, state, config, args.dry_run, result)
            if args.replies:
                reply_lane(client, model, reader, state, config, args.dry_run, result)

    except SuspensionRisk as exc:
        result["outcome"] = "failure"
        result["errors"].append(str(exc))
        log.error("%s", exc)
    except (MoltbookError, RuntimeError, urllib.error.URLError, OSError) as exc:
        result["outcome"] = "failure"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        log.error("run failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 — a crash is a reportable failure
        result["outcome"] = "failure"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        log.exception("unexpected failure")
    finally:
        state.verify_failures = getattr(
            locals().get("client"), "consecutive_verify_failures", state.verify_failures
        )
        state.save()

    metrics = result["metrics"]
    if metrics.get("verification_failures") or metrics.get("guard_blocks"):
        # Composed but never made it out. Reporting this as a success is how a
        # broken agent stays green for a week.
        result["outcome"] = "failure"
    result["summary"] = (
        f"{metrics['posts_published']} post(s) published, "
        f"{metrics['replies_drafted']} reply draft(s), "
        f"{metrics['comments_triaged']} comment(s) triaged, "
        f"{metrics['guard_blocks']} blocked by guard"
        if result["outcome"] == "success"
        else (result["errors"][0] if result["errors"] else "run failed")
    )

    Path(config["out"]).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("\n" + result["summary"])

    if summary_file := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write(f"### moltbook-resident — {result['outcome']}\n\n{result['summary']}\n")

    return 1 if result["outcome"] == "failure" else 0


if __name__ == "__main__":
    sys.exit(main())
