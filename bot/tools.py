"""Stew Bot — Tool implementations backed by Firestore.

Read tools return data immediately. Write tools return PendingWrite objects
that go through inline-keyboard confirmation before executing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import firestore_client as fsc

logger = logging.getLogger("tanuki.tools")


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
        return fn(**self.args)


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


def read_plan_summary() -> str:
    return fsc.get_plan_summary()


def read_day(day_number: int) -> str:
    day = fsc.get_day(day_number)
    if day is None:
        return f"Day {day_number} not found."
    return json.dumps(day, indent=2, default=str, ensure_ascii=False)


def read_booking(booking_id: str) -> str:
    b = fsc.get_booking(booking_id)
    if b is None:
        return f"Booking '{booking_id}' not found."
    return json.dumps(b, indent=2, default=str, ensure_ascii=False)


def get_needs_booking() -> str:
    needs = fsc.get_needs_booking()
    if not needs:
        return "Everything is booked!"
    return json.dumps(needs, indent=2, default=str, ensure_ascii=False)


# ── Write Tools (return PendingWrite) ─────────────────────


def prepare_add_activity(
    day_number: int,
    slug: str,
    name: str,
    activity_type: str,
    booking_required: bool,
    time: str = "",
    gps: list[float] | None = None,
    notes: str = "",
    specialty: str = "",
    sort_order: int | None = None,
    booking_ref: str = "",
) -> PendingWrite:
    data = {
        "name": name,
        "type": activity_type,
        "booking_required": booking_required,
        "booking_ref": booking_ref if booking_ref else None,
    }
    if time:
        data["time"] = time
    if gps:
        data["gps"] = gps
    if notes:
        data["notes"] = notes
    if specialty:
        data["specialty"] = specialty
    if sort_order is not None:
        data["sort_order"] = sort_order

    return PendingWrite(
        operation="add_activity",
        args={"day_number": day_number, "slug": slug, "activity_data": data, "changed_by": "bot"},
        summary=f"Add '{name}' to Day {day_number}",
    )


def prepare_remove_activity(day_number: int, slug: str) -> PendingWrite:
    return PendingWrite(
        operation="remove_activity",
        args={"day_number": day_number, "slug": slug, "changed_by": "bot"},
        summary=f"Remove '{slug}' from Day {day_number}",
    )


def prepare_update_activity(
    day_number: int, slug: str, field: str, value: Any
) -> PendingWrite:
    return PendingWrite(
        operation="update_activity_field",
        args={"day_number": day_number, "slug": slug, "field": field, "value": value, "changed_by": "bot"},
        summary=f"Update Day {day_number} {slug}.{field}",
    )


def prepare_update_booking(
    booking_id: str, field: str, value: Any
) -> PendingWrite:
    return PendingWrite(
        operation="update_booking_field",
        args={"booking_id": booking_id, "field": field, "value": value, "changed_by": "bot"},
        summary=f"Update booking {booking_id}.{field}",
    )


def prepare_confirm_booking(
    booking_id: str,
    day_number: int,
    activity_slug: str,
    confirmation_code: str = "",
) -> PendingWrite:
    updates = {"status": "confirmed"}
    if confirmation_code:
        updates["confirmation"] = confirmation_code
    return PendingWrite(
        operation="confirm_booking",
        args={
            "booking_id": booking_id,
            "booking_updates": updates,
            "day_number": day_number,
            "activity_slug": activity_slug,
            "changed_by": "bot",
        },
        summary=f"Confirm booking '{booking_id}' → Day {day_number}/{activity_slug}",
    )


def prepare_confirm_activity(
    day_number: int,
    slug: str,
    confirmation_code: str,
    details: str = "",
    cost: str = "",
) -> PendingWrite:
    return PendingWrite(
        operation="confirm_activity_booking",
        args={
            "day_number": day_number,
            "slug": slug,
            "confirmation_code": confirmation_code,
            "details": details,
            "cost": cost,
            "changed_by": "bot",
        },
        summary=f"Confirm Day {day_number} '{slug}' — ref: {confirmation_code}",
    )


def prepare_add_note(text: str) -> PendingWrite:
    return PendingWrite(
        operation="add_note",
        args={"text": text, "author": "bot"},
        summary=f"Add note: {text[:200]}",
    )


def prepare_add_todo(text: str, category: str = "prep", due_date: str = "") -> PendingWrite:
    return PendingWrite(
        operation="add_todo",
        args={"text": text, "category": category, "due_date": due_date, "changed_by": "bot"},
        summary=f"Add todo: {text[:200]}",
    )


def prepare_complete_todo(todo_id: str) -> PendingWrite:
    return PendingWrite(
        operation="complete_todo",
        args={"todo_id": todo_id, "changed_by": "bot"},
        summary=f"Complete todo id={todo_id}",
    )


def get_todos_text(include_ids: bool = False) -> str:
    """Format pending todos for display.

    When include_ids=True, prefix each line with the Firestore document id so the
    LLM can reference an exact todo when calling complete_todo. Humans see the
    pretty version (no ids) in /todos."""
    todos = fsc.get_todos(include_done=False)
    if not todos:
        return "No pending todos!"
    lines = []
    for t in todos:
        cat = t.get("category", "")
        due = f" (due {t['due_date']})" if t.get("due_date") else ""
        if include_ids:
            lines.append(f"  ○ id={t.get('_id', '?')}  [{cat}] {t.get('text', '?')}{due}")
        else:
            lines.append(f"  ○ [{cat}] {t.get('text', '?')}{due}")
    return "\n".join(lines)


# ── Reminder Tool ────────────────────────────────────────


def add_reminder(time_iso: str, message: str, chat_id: int) -> dict:
    """Add a reminder to Firestore. Returns the reminder dict."""
    return fsc.add_reminder(time_iso, message, chat_id)


def load_reminders() -> list[dict]:
    """Return pending reminders (used by /status for count)."""
    return fsc.get_due_reminders()


# ── Fast-Path Helpers (no Gemini call) ───────────────────


def get_today_plan(day_number: int) -> str | None:
    """Get today's plan from Firestore cache."""
    day = fsc.get_day(day_number)
    if not day:
        return None
    city = day.get("base_city", "?")
    acts = day.get("activities", {})
    act_names = ", ".join(
        a.get("name", k) for k, a in sorted(acts.items(), key=lambda x: x[1].get("sort_order", 99))
    )
    return f"Day {day_number} — {city} — {act_names}"


def get_tonight_hotel(date_str: str) -> dict | None:
    """Find the accommodation for a given date from Firestore cache."""
    from datetime import date
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        return None

    bookings = fsc.get_all_bookings()
    for bid, b in bookings.items():
        ci = b.get("check_in", "")
        co = b.get("check_out", "")
        if not ci or not co:
            continue
        try:
            check_in = date.fromisoformat(str(ci))
            check_out = date.fromisoformat(str(co))
            if check_in <= target < check_out:
                b["_id"] = bid
                return b
        except ValueError:
            continue
    return None


def get_total_spent() -> dict:
    """Sum up accommodation costs from Firestore cache."""
    bookings = fsc.get_all_bookings()
    meta = fsc.get_meta()

    flights = meta.get("flights", {})
    flights_cost = 3000  # known fixed cost

    acc_total = 0
    for bid, b in bookings.items():
        price = b.get("price_usd") or 0
        if isinstance(price, (int, float)):
            acc_total += price

    return {
        "flights_usd": flights_cost,
        "accommodations_usd": acc_total,
        "total_usd": flights_cost + acc_total,
    }
