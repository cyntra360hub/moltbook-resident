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

from agent import Anthropic, make_puzzle_solver
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
    parser.add_argument(
        "--verify-only",
        metavar="CODE",
        help="skip posting; just answer an outstanding challenge, e.g. after a "
             "solver failure. Pair with --answer.",
    )
    parser.add_argument("--answer", help="the numeric answer for --verify-only")
    args = parser.parse_args()

    if args.verify_only:
        client = MoltbookClient(os.environ.get("MOLTBOOK_API_KEY", ""))
        if not args.answer:
            print("--verify-only needs --answer", file=sys.stderr)
            return 1
        response = client._request(
            "POST", "/verify",
            {"verification_code": args.verify_only, "answer": f"{float(args.answer):.2f}"},
        )
        print("verified" if response.get("success") else f"rejected: {response}")
        return 0 if response.get("success") else 1

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    text = Path(args.file).read_text(encoding="utf-8").strip() if args.file else args.text
    if not text:
        print("nothing to post: pass text or --file", file=sys.stderr)
        return 1

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

        # The local parser handles simple two-number challenges. Longer,
        # multi-clause ones ("claws press on water pressure and push, claw-force
        # is thirty-two newtons...") need the model — which is exactly what the
        # fallback is for, and passing None here was a bug.
        solver = None
        if os.environ.get("ANTHROPIC_API_KEY"):
            model_name = str((config.get("model") or {}).get("name", "claude-sonnet-4-6"))
            solver = make_puzzle_solver(
                Anthropic(os.environ["ANTHROPIC_API_KEY"], model_name)
            )
        else:
            print(
                "note: ANTHROPIC_API_KEY not set, so only the local puzzle parser "
                "is available",
                file=sys.stderr,
            )

        comment_id, challenge = client.create_comment(args.post_id, text, args.parent)
        if challenge:
            print(f"solving verification: {challenge.challenge_text[:70]}...")
            if not client.solve(challenge, solver):
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
