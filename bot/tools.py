"""Stew Tool Implementations — backed by Firestore.

Read tools return data immediately. Write tools return PendingWrite objects
that go through inline-keyboard confirmation before executing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import firestore_client as fsc

logger = logging.getLogger("stew.tools")


# ── PendingWrite ──────────────────────────────────────────


class PendingWrite:
    """A queued Firestore operation awaiting user confirmation."""

    def __init__(self, operation: str, args: dict, summary: str):
        self.operation = operation
        self.args = args
        self.summary = summary
        self.created_at = datetime.now(timezone.utc)

    def execute(self) -> str:
        """Run the actual Firestore write."""
        fn = getattr(fsc, self.operation, None)
        if fn is None:
            return f"Unknown operation: {self.operation}"
        try:
            return fn(**self.args)
        except Exception as e:
            logger.error("Operation failed: %s", e, exc_info=True)
            return f"Failed: {e}"


def execute_writes(writes: list[PendingWrite]) -> str:
    """Execute all pending writes. Returns a combined status string."""
    if not writes:
        return "Nothing to write."
    results = []
    for w in writes:
        try:
            results.append(w.execute())
        except Exception as e:
            logger.error("Write failed for %s: %s", w.summary, e, exc_info=True)
            results.append(f"Failed: {w.summary} — {e}")
    return " | ".join(results)


# ── Read Tools (no confirmation) ─────────────────────────

def get_member_summary(name: str) -> str:
    """Get a member's profile + latest interview summary."""
    member = fsc.get_member(name)
    if not member:
        return None

    name_str = member.get("name", "?")
    bday = member.get("birthday", "")
    phone = member.get("phone", "")
    status = member.get("status", "active")

    lines = [f"*{name_str}*"]
    if bday:
        lines.append(f"📅 Birthday: {bday}")
    if phone:
        lines.append(f"☎️ {phone}")
    if status == "inactive":
        lines.append("(Inactive)")

    interviews = fsc.get_member_interviews(member.get("_id", ""))
    if interviews:
        latest = interviews[0]
        lines.append(f"\nLatest interview: {latest.get('date', '?')}")
        if latest.get("work_notes"):
            lines.append(f"💼 Work: {latest['work_notes'][:100]}")
        if latest.get("faith_notes"):
            lines.append(f"🙏 Faith: {latest['faith_notes'][:100]}")

    return "\n".join(lines)


def get_pending_follow_ups() -> str:
    """Format pending follow-ups for display."""
    follow_ups = fsc.get_pending_follow_ups()
    if not follow_ups:
        return "No pending follow-ups!"

    lines = []
    for f in follow_ups:
        member_name = f.get("member_name", "?")
        topic = f.get("topic", "?")
        due = f.get("due_date", "?")
        lines.append(f"  • {member_name} — {topic} (due {due})")

    return "\n".join(lines)


def get_upcoming_birthdays(days: int = 7) -> list[dict]:
    """Get members with birthdays in next N days."""
    return fsc.get_upcoming_birthdays(days=days)


def add_reminder(time_iso: str, message: str, chat_id: int) -> dict:
    """Add a reminder to Firestore. Returns the reminder dict."""
    return fsc.add_reminder(time_iso, message, chat_id)


# ── Write Tools (return PendingWrite) ─────────────────────


# TODO: Implement these as PendingWrite stubs
# - prepare_save_interview
# - prepare_save_note
# - prepare_add_prayer_request
# - prepare_schedule_follow_up
# - prepare_complete_prayer_request
# - prepare_complete_follow_up
# - prepare_snooze_follow_up

def prepare_add_reminder(time_iso: str, message: str, chat_id: int) -> PendingWrite:
    """Queue a reminder for addition."""
    return PendingWrite(
        operation="add_reminder",
        args={"time_iso": time_iso, "message": message, "chat_id": chat_id},
        summary=f"Add reminder: {message[:50]}"
    )
