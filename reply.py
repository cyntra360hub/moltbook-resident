#!/usr/bin/env python3
"""
reply.py — post one reply by hand, with the verification puzzle handled.

For the occasional message worth answering yourself rather than leaving to the
reply lane. Reuses the same client, puzzle solver and output guard as the
agent, so a hand-written reply is held to the same rules as a generated one.

Usage:
    python reply.py <post_id> "your reply text"
    python reply.py <post_id> --file reply.txt
    python reply.py <post_id> "text" --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

from resident import guard
from resident.moltbook import MoltbookClient, MoltbookError


def main() -> int:
    parser = argparse.ArgumentParser(description="Post one reply to a Moltbook post.")
    parser.add_argument("post_id")
    parser.add_argument("text", nargs="?", default="")
    parser.add_argument("--file", help="read the reply from a file instead")
    parser.add_argument("--parent", default="", help="comment id, to reply to a comment")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    text = Path(args.file).read_text(encoding="utf-8").strip() if args.file else args.text
    if not text:
        print("nothing to post: pass text or --file", file=sys.stderr)
        return 1

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    record_url = str(config.get("record_url", ""))

    # Same guard the agent uses. A hand-written reply can still contain a stray
    # link or a phrase the platform's terms treat as advertising.
    verdict = guard.check_comment(text, allowed_url=record_url)
    if not verdict:
        print("blocked by guard:", file=sys.stderr)
        for reason in verdict.reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 2

    print(f"--- reply ({len(text)} chars) ---\n{text}\n")
    if args.dry_run:
        print("dry run — nothing posted")
        return 0

    try:
        client = MoltbookClient(os.environ.get("MOLTBOOK_API_KEY", ""))
        comment_id, challenge = client.create_comment(args.post_id, text, args.parent)
        if challenge:
            print(f"solving verification: {challenge.challenge_text[:70]}...")
            if not client.solve(challenge, None):
                print(
                    "could not solve the puzzle locally. Solve it by hand and POST "
                    "the answer to /api/v1/verify with this code:\n  "
                    f"{challenge.verification_code}",
                    file=sys.stderr,
                )
                return 3
        print(f"posted (comment {comment_id or 'ok'})")
    except MoltbookError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
