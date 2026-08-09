"""Local state: what we posted, what we drafted, and whether we should stop.

Small and file-backed. Two jobs:

  1. Never post twice for the same period — a retried workflow run must not
     produce a duplicate, which the platform treats as spam.
  2. Carry the consecutive-verification-failure count between runs, so the
     suspension guard survives a process restart. A counter that resets every
     run is not a guard.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class State:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "posted_periods": [],
            "consecutive_verify_failures": 0,
            "replied_comment_ids": [],
            "replied_post_ids": [],
            "recent_titles": [],
            "last_post_at": "",
            # One dated leaderboard snapshot per day, so the next run can report
            # what changed rather than the same frozen numbers every time.
            "leaderboard_snapshots": [],
            # Outreach: posts we've already replied to (kept separate from the
            # mention/comment reply set), and a per-day counter so the hard cap
            # survives a restart. A counter that resets every run is no cap.
            "outreach_replied_post_ids": [],
            "outreach_sent": {},
        }
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.data.update(loaded)
            except (OSError, json.JSONDecodeError):
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the lists bounded so the file cannot grow without limit.
        self.data["posted_periods"] = self.data["posted_periods"][-90:]
        self.data["replied_comment_ids"] = self.data["replied_comment_ids"][-500:]
        self.data["replied_post_ids"] = self.data["replied_post_ids"][-500:]
        self.data["recent_titles"] = self.data["recent_titles"][-10:]
        # Keep at most 30 days of snapshots. Dates are ISO strings, so a plain
        # sort is chronological; the newest 30 survive.
        self.data["leaderboard_snapshots"] = sorted(
            self.data.get("leaderboard_snapshots", []),
            key=lambda snap: str(snap.get("date", "")),
        )[-30:]
        self.data["outreach_replied_post_ids"] = (
            self.data["outreach_replied_post_ids"][-500:]
        )
        # Keep only the most recent 30 days of outreach counters.
        sent = self.data.get("outreach_sent", {})
        if len(sent) > 30:
            self.data["outreach_sent"] = {
                day: sent[day] for day in sorted(sent)[-30:]
            }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def already_posted(self, period: str) -> bool:
        return period in self.data["posted_periods"]

    def mark_posted(self, period: str) -> None:
        if period not in self.data["posted_periods"]:
            self.data["posted_periods"].append(period)
        self.data["last_post_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )

    def already_replied(self, comment_id: str) -> bool:
        return comment_id in self.data["replied_comment_ids"]

    def mark_replied(self, comment_id: str) -> None:
        if comment_id and comment_id not in self.data["replied_comment_ids"]:
            self.data["replied_comment_ids"].append(comment_id)

    @property
    def recent_titles(self) -> list[str]:
        return list(self.data.get("recent_titles", []))

    def remember_title(self, title: str) -> None:
        if title and title not in self.data["recent_titles"]:
            self.data["recent_titles"].append(title)

    def already_replied_to_post(self, post_id: str) -> bool:
        return post_id in self.data["replied_post_ids"]

    def mark_replied_to_post(self, post_id: str) -> None:
        if post_id and post_id not in self.data["replied_post_ids"]:
            self.data["replied_post_ids"].append(post_id)

    @property
    def verify_failures(self) -> int:
        return int(self.data.get("consecutive_verify_failures", 0))

    @verify_failures.setter
    def verify_failures(self, value: int) -> None:
        self.data["consecutive_verify_failures"] = int(value)

    @property
    def snapshots(self) -> list[dict[str, Any]]:
        return list(self.data.get("leaderboard_snapshots", []))

    def add_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Record one dated leaderboard snapshot, one per date (a re-run the
        same day replaces the earlier one). Kept locally only: a snapshot holds
        every listed agent's slug so a COUNT of newcomers is possible, but those
        names must never be published — see material.diff_snapshots.
        """
        date = str(snapshot.get("date", ""))
        if not date:
            return
        kept = [s for s in self.data.get("leaderboard_snapshots", [])
                if str(s.get("date", "")) != date]
        kept.append(snapshot)
        self.data["leaderboard_snapshots"] = kept

    def snapshot_before(self, date: str) -> dict[str, Any] | None:
        """The most recent snapshot strictly older than `date`, or None.

        Same-date snapshots are skipped: diffing today against an earlier run
        the same day would report no movement and hide the real day-over-day
        change. No earlier snapshot means no baseline, and the caller must then
        emit no delta facts rather than invent one.
        """
        earlier = [s for s in self.data.get("leaderboard_snapshots", [])
                   if str(s.get("date", "")) < date]
        if not earlier:
            return None
        return max(earlier, key=lambda snap: str(snap.get("date", "")))

    # --- outreach --------------------------------------------------------- #

    def already_outreached(self, post_id: str) -> bool:
        return post_id in self.data["outreach_replied_post_ids"]

    def mark_outreached(self, post_id: str) -> None:
        if post_id and post_id not in self.data["outreach_replied_post_ids"]:
            self.data["outreach_replied_post_ids"].append(post_id)

    def outreach_sent_on(self, date: str) -> int:
        return int(self.data.get("outreach_sent", {}).get(date, 0))

    def record_outreach(self, date: str) -> None:
        """Count one outreach reply against `date`'s daily cap. Persisted, so a
        retried or restarted run cannot reset the count and exceed the cap."""
        sent = self.data.setdefault("outreach_sent", {})
        sent[date] = int(sent.get(date, 0)) + 1
