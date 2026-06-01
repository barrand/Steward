"""Firestore client for Stew Bot — single interface to quorum member data.

Used by:
- The Telegram bot (imports as a library, uses in-memory cache)
- CLI agent (calls as CLI: python firestore_client.py import-csv)

Data model: members, interviews, notes, prayer_requests, follow_ups collections.
See the plan for full schema documentation.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import DocumentSnapshot

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger("stew.firestore")


# ── Structured JSON Logging for Cloud Run ─────────────────

class _CloudRunFormatter(logging.Formatter):
    """Emit JSON logs that Cloud Logging auto-parses into structured entries."""
    def format(self, record):
        return json.dumps({
            "severity": record.levelname,
            "message": super().format(record),
            "logger": record.name,
        }, ensure_ascii=False)


def configure_logging():
    """Set up logging: JSON on Cloud Run, plain text locally."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("K_SERVICE"):
        handler.setFormatter(_CloudRunFormatter("%(message)s"))
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
        ))
    root.handlers = [handler]


# ── Firebase Init ─────────────────────────────────────────

_app = None
_db = None


def get_db():
    """Initialize and return the Firestore client. Idempotent.

    On Cloud Run (K_SERVICE set): uses the default service account via IAM.
    Locally: uses the service-account-key.json file.
    """
    global _app, _db
    if _db is not None:
        return _db

    if os.environ.get("K_SERVICE"):
        _app = firebase_admin.initialize_app()
    else:
        cred_path = os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS",
            str(Path(__file__).parent / "service-account-key.json"),
        )
        if not Path(cred_path).exists():
            raise FileNotFoundError(
                f"Service account key not found at {cred_path}. "
                "Download from Firebase Console > Project Settings > Service Accounts."
            )
        cred = credentials.Certificate(cred_path)
        _app = firebase_admin.initialize_app(cred)

    _db = firestore.client()
    logger.info("Firestore connected — project: %s", _app.project_id)
    return _db


# ── In-Memory Cache ───────────────────────────────────────

_cache = {
    "members": {},           # {member_id: member_data}
    "interviews": [],        # sorted by date desc
    "notes": [],             # casual notes, sorted by date desc
    "prayer_requests": [],   # status="pending", sorted by next_remind_date asc
    "follow_ups": [],        # status="pending", sorted by due_date asc
    "meta": {},              # reminder state, chat history
    "reminders": [],         # pending reminders
}
_cache_lock = threading.Lock()
_listeners = []
_collections_loaded: set = set()
_cache_ready = threading.Event()
_REQUIRED_COLLECTIONS = {"members", "interviews", "notes", "prayer_requests", "follow_ups", "meta", "reminders"}


def _mark_loaded(name: str):
    """Track which collections have delivered their first snapshot."""
    _collections_loaded.add(name)
    if _collections_loaded >= _REQUIRED_COLLECTIONS:
        _cache_ready.set()


def init_cache():
    """Subscribe to all collections via on_snapshot. Call once on startup."""
    db = get_db()

    def _on_plan(col_snapshot, changes, read_time):
        with _cache_lock:
            for change in changes:
                doc = change.document
                if change.type.name in ("ADDED", "MODIFIED"):
                    _cache["plan"][doc.id] = doc.to_dict()
                elif change.type.name == "REMOVED":
                    _cache["plan"].pop(doc.id, None)
        _mark_loaded("plan")
        logger.debug("Plan cache updated: %d days", len(_cache["plan"]))

    def _on_bookings(col_snapshot, changes, read_time):
        with _cache_lock:
            for change in changes:
                doc = change.document
                if change.type.name in ("ADDED", "MODIFIED"):
                    _cache["bookings"][doc.id] = doc.to_dict()
                elif change.type.name == "REMOVED":
                    _cache["bookings"].pop(doc.id, None)
        _mark_loaded("bookings")
        logger.debug("Bookings cache updated: %d bookings", len(_cache["bookings"]))

    def _on_family(col_snapshot, changes, read_time):
        with _cache_lock:
            for change in changes:
                doc = change.document
                if change.type.name in ("ADDED", "MODIFIED"):
                    _cache["family"][doc.id] = doc.to_dict()
                elif change.type.name == "REMOVED":
                    _cache["family"].pop(doc.id, None)
        _mark_loaded("family")

    def _on_meta(col_snapshot, changes, read_time):
        with _cache_lock:
            for change in changes:
                doc = change.document
                if change.type.name in ("ADDED", "MODIFIED"):
                    _cache["meta"][doc.id] = doc.to_dict()
                elif change.type.name == "REMOVED":
                    _cache["meta"].pop(doc.id, None)
        _mark_loaded("meta")

    def _on_notes(col_snapshot, changes, read_time):
        with _cache_lock:
            all_notes = []
            for doc in col_snapshot:
                d = doc.to_dict()
                d["_id"] = doc.id
                all_notes.append(d)
            _cache["notes"] = sorted(
                all_notes,
                key=lambda n: n.get("created_at", ""),
                reverse=True,
            )[:50]
        _mark_loaded("notes")

    def _on_todos(col_snapshot, changes, read_time):
        with _cache_lock:
            all_todos = []
            for doc in col_snapshot:
                d = doc.to_dict()
                d["_id"] = doc.id
                all_todos.append(d)
            _cache["todos"] = sorted(
                all_todos,
                key=lambda t: (t.get("status") == "done", t.get("created_at", "")),
            )
        _mark_loaded("todos")

    def _on_reminders(col_snapshot, changes, read_time):
        with _cache_lock:
            all_reminders = []
            for doc in col_snapshot:
                d = doc.to_dict()
                d["_id"] = doc.id
                all_reminders.append(d)
            _cache["reminders"] = sorted(
                all_reminders,
                key=lambda r: r.get("fire_at", ""),
            )
        _mark_loaded("reminders")

    _listeners.append(db.collection("plan").on_snapshot(_on_plan))
    _listeners.append(db.collection("bookings").on_snapshot(_on_bookings))
    _listeners.append(db.collection("family").on_snapshot(_on_family))
    _listeners.append(db.collection("meta").on_snapshot(_on_meta))
    _listeners.append(db.collection("notes").on_snapshot(_on_notes))
    _listeners.append(db.collection("todos").on_snapshot(_on_todos))
    _listeners.append(db.collection("reminders").on_snapshot(_on_reminders))

    _cache_ready.wait(timeout=15)
    logger.info(
        "Cache ready: %d days, %d bookings, %d family members",
        len(_cache["plan"]),
        len(_cache["bookings"]),
        len(_cache["family"]),
    )


# ── Read Layer (from cache) ───────────────────────────────


def get_plan_summary() -> str:
    """Compact one-line-per-day overview for the system prompt."""
    with _cache_lock:
        days = sorted(_cache["plan"].values(), key=lambda d: d.get("day_number", 0))

    lines = []
    for day in days:
        n = day.get("day_number", "?")
        date = day.get("date", "?")
        city = day.get("base_city", "?")
        gw = " ⚠GW" if day.get("golden_week") else ""
        acts = day.get("activities", {})
        act_names = ", ".join(
            a.get("name", k) for k, a in sorted(acts.items(), key=lambda x: x[1].get("sort_order", 99))
        )
        lines.append(f"Day {n} ({date}{gw}) — {city} — {act_names or 'TBD'}")
    return "\n".join(lines)


def get_day(day_number: int) -> dict | None:
    """Full day document from cache."""
    doc_id = f"day-{day_number:02d}"
    with _cache_lock:
        return _cache["plan"].get(doc_id)


def get_all_bookings() -> dict:
    """All bookings from cache. {booking_id: booking_data}"""
    with _cache_lock:
        return dict(_cache["bookings"])


def get_booking(booking_id: str) -> dict | None:
    """Single booking from cache."""
    with _cache_lock:
        return _cache["bookings"].get(booking_id)


def get_family() -> dict:
    """All family profiles from cache. {name: profile_data}"""
    with _cache_lock:
        return dict(_cache["family"])


def get_meta() -> dict:
    """Trip metadata from cache."""
    with _cache_lock:
        return _cache["meta"].get("trip-info", {})


def get_recent_notes(limit: int = 10) -> list[dict]:
    """Recent notes from cache."""
    with _cache_lock:
        return list(_cache["notes"][:limit])


def get_needs_booking() -> list[dict]:
    """Derived list of items that still need booking."""
    needs = []
    with _cache_lock:
        for day_id, day in _cache["plan"].items():
            for act_slug, act in day.get("activities", {}).items():
                if act.get("booking_required") and not act.get("booking_ref"):
                    needs.append({
                        "day_number": day.get("day_number"),
                        "date": day.get("date"),
                        "activity_slug": act_slug,
                        "activity_name": act.get("name", act_slug),
                        "type": act.get("type"),
                    })
        for book_id, book in _cache["bookings"].items():
            if book.get("status") == "needs_booking":
                needs.append({
                    "booking_id": book_id,
                    "booking_name": book.get("name", book_id),
                    "type": book.get("type"),
                    "status": "needs_booking",
                })
    return sorted(needs, key=lambda x: x.get("date", x.get("booking_id", "")))


# ── Write Layer (all log to changelog) ────────────────────


def _log_change(
    collection: str,
    doc_id: str,
    changes: dict,
    changed_by: str,
    summary: str,
):
    """Write a changelog entry for undo support."""
    db = get_db()
    db.collection("changelog").add({
        "collection": collection,
        "doc_id": doc_id,
        "changes": changes,
        "changed_by": changed_by,
        "timestamp": datetime.now(timezone.utc),
        "summary": summary,
    })
    logger.info("Changelog: [%s] %s/%s — %s", changed_by, collection, doc_id, summary)


def _get_previous_values(collection: str, doc_id: str, field_paths: list[str]) -> dict:
    """Read current values of fields before overwriting (for changelog)."""
    db = get_db()
    doc = db.collection(collection).document(doc_id).get()
    if not doc.exists:
        return {fp: None for fp in field_paths}

    data = doc.to_dict()
    prev = {}
    for fp in field_paths:
        parts = fp.split(".")
        val = data
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                val = None
                break
        prev[fp] = val
    return prev


def add_activity(
    day_number: int,
    slug: str,
    activity_data: dict,
    changed_by: str = "cursor",
) -> str:
    """Add an activity to a day's activities map.

    If sort_order is provided and conflicts with an existing activity,
    bump all activities at or above that sort_order up by 1.
    If sort_order is not provided, append to the end.
    """
    db = get_db()
    doc_id = f"day-{day_number:02d}"
    field_path = f"activities.{slug}"
    day = get_day(day_number)
    existing_acts = day.get("activities", {}) if day else {}

    target_order = activity_data.get("sort_order")

    if target_order is not None:
        # Bump existing activities at or above the target sort_order
        updates = {}
        for existing_slug, act in existing_acts.items():
            if act.get("sort_order", 99) >= target_order:
                updates[f"activities.{existing_slug}.sort_order"] = act.get("sort_order", 99) + 1
        if updates:
            db.collection("plan").document(doc_id).update(updates)
    else:
        max_order = max(
            (a.get("sort_order", 0) for a in existing_acts.values()),
            default=0,
        )
        activity_data["sort_order"] = max_order + 1

    activity_data.setdefault("booking_ref", None)

    prev = _get_previous_values("plan", doc_id, [field_path])
    db.collection("plan").document(doc_id).update({field_path: activity_data})
    _log_change("plan", doc_id, prev, changed_by, f"Added {activity_data.get('name', slug)} to Day {day_number}")
    return f"Added {activity_data.get('name', slug)} to Day {day_number}"


def remove_activity(
    day_number: int,
    slug: str,
    changed_by: str = "cursor",
) -> str:
    """Remove an activity from a day."""
    db = get_db()
    doc_id = f"day-{day_number:02d}"
    field_path = f"activities.{slug}"

    # Validate the activity actually exists before removing (avoid silent no-op).
    doc = db.collection("plan").document(doc_id).get()
    if not doc.exists:
        return f"ERROR: Day {day_number} does not exist"
    activities = (doc.to_dict() or {}).get("activities", {}) or {}
    if slug not in activities:
        available = ", ".join(sorted(activities.keys())[:10]) or "(none)"
        return (
            f"ERROR: Activity slug '{slug}' not found on Day {day_number}. "
            f"Existing slugs: {available}. Call read_day first to get exact slugs."
        )

    prev = _get_previous_values("plan", doc_id, [field_path])
    db.collection("plan").document(doc_id).update({field_path: firestore.DELETE_FIELD})
    _log_change("plan", doc_id, prev, changed_by, f"Removed {slug} from Day {day_number}")
    return f"Removed {slug} from Day {day_number}"


def update_activity_field(
    day_number: int,
    slug: str,
    field: str,
    value: Any,
    changed_by: str = "cursor",
) -> str:
    """Update a single field of an activity."""
    db = get_db()
    doc_id = f"day-{day_number:02d}"
    field_path = f"activities.{slug}.{field}"

    # Validate the activity actually exists. Firestore's dotted-path update
    # will silently create missing parent keys, which produced half-empty
    # "ghost" activities when Gemini hallucinated a slug.
    doc = db.collection("plan").document(doc_id).get()
    if not doc.exists:
        return f"ERROR: Day {day_number} does not exist"
    activities = (doc.to_dict() or {}).get("activities", {}) or {}
    if slug not in activities:
        available = ", ".join(sorted(activities.keys())[:10]) or "(none)"
        return (
            f"ERROR: Activity slug '{slug}' not found on Day {day_number}. "
            f"Existing slugs: {available}. Call read_day first to get exact slugs."
        )

    prev = _get_previous_values("plan", doc_id, [field_path])
    db.collection("plan").document(doc_id).update({field_path: value})
    _log_change("plan", doc_id, prev, changed_by, f"Updated {slug}.{field} on Day {day_number}")
    return f"Updated {slug}.{field} on Day {day_number}"


def update_day_field(
    day_number: int,
    field: str,
    value: Any,
    changed_by: str = "cursor",
) -> str:
    """Update a top-level day field (notes, transport_notes, base_city)."""
    db = get_db()
    doc_id = f"day-{day_number:02d}"

    prev = _get_previous_values("plan", doc_id, [field])
    db.collection("plan").document(doc_id).update({field: value})
    _log_change("plan", doc_id, prev, changed_by, f"Updated Day {day_number} {field}")
    return f"Updated Day {day_number} {field}"


def add_booking(
    booking_id: str,
    booking_data: dict,
    changed_by: str = "cursor",
) -> str:
    """Create a new booking document."""
    db = get_db()
    db.collection("bookings").document(booking_id).set(booking_data)
    _log_change("bookings", booking_id, {k: None for k in booking_data}, changed_by, f"Added booking: {booking_data.get('name', booking_id)}")
    return f"Added booking: {booking_data.get('name', booking_id)}"


def update_booking_field(
    booking_id: str,
    field: str,
    value: Any,
    changed_by: str = "cursor",
) -> str:
    """Update a single field of a booking."""
    db = get_db()

    prev = _get_previous_values("bookings", booking_id, [field])
    db.collection("bookings").document(booking_id).update({field: value})
    _log_change("bookings", booking_id, prev, changed_by, f"Updated {booking_id}.{field}")
    return f"Updated {booking_id}.{field}"


def confirm_booking(
    booking_id: str,
    booking_updates: dict,
    day_number: int,
    activity_slug: str,
    changed_by: str = "cursor",
) -> str:
    """Atomically confirm a booking AND link it to its activity.
    Sets booking status + any other fields, and sets booking_ref on the activity.
    """
    db = get_db()
    batch = db.batch()

    booking_ref = db.collection("bookings").document(booking_id)
    booking_updates.setdefault("status", "confirmed")
    batch.update(booking_ref, booking_updates)

    day_doc_id = f"day-{day_number:02d}"
    day_ref = db.collection("plan").document(day_doc_id)
    batch.update(day_ref, {f"activities.{activity_slug}.booking_ref": booking_id})

    batch.commit()

    _log_change(
        "bookings", booking_id,
        {k: None for k in booking_updates},
        changed_by,
        f"Confirmed booking {booking_id} and linked to Day {day_number}/{activity_slug}",
    )
    return f"Confirmed {booking_id} and linked to Day {day_number}/{activity_slug}"


def confirm_activity_booking(
    day_number: int,
    slug: str,
    confirmation_code: str,
    details: str = "",
    cost: str = "",
    changed_by: str = "cursor",
) -> str:
    """Mark an activity as booked by setting booking_ref + notes atomically.

    Use this for transport bookings (Shinkansen), tickets (USJ, museums),
    and anything that lives on the plan rather than in the bookings collection.
    """
    db = get_db()
    doc_id = f"day-{day_number:02d}"

    updates = {f"activities.{slug}.booking_ref": confirmation_code}
    if details:
        updates[f"activities.{slug}.notes"] = details
    if cost:
        updates[f"activities.{slug}.cost"] = cost

    prev = _get_previous_values("plan", doc_id, list(updates.keys()))
    db.collection("plan").document(doc_id).update(updates)
    _log_change(
        "plan", doc_id, prev, changed_by,
        f"Confirmed activity {slug} on Day {day_number} — ref: {confirmation_code}",
    )
    return f"Confirmed {slug} on Day {day_number} — ref: {confirmation_code}"


def add_note(text: str, author: str = "cursor") -> str:
    """Add a timestamped note."""
    db = get_db()
    db.collection("notes").add({
        "text": text,
        "created_at": datetime.now(timezone.utc),
        "created_by": author,
    })
    logger.info("Note added by %s: %s", author, text[:80])
    return f"Note added: {text}"


# ── Todos ─────────────────────────────────────────────────


def get_todos(include_done: bool = False) -> list[dict]:
    """Get todos from cache. By default only pending items."""
    with _cache_lock:
        if include_done:
            return list(_cache["todos"])
        return [t for t in _cache["todos"] if t.get("status") != "done"]


def add_todo(
    text: str,
    category: str = "prep",
    due_date: str = "",
    changed_by: str = "cursor",
) -> str:
    """Add a todo item."""
    db = get_db()
    doc_data = {
        "text": text,
        "status": "pending",
        "category": category,
        "due_date": due_date,
        "created_at": datetime.now(timezone.utc),
        "created_by": changed_by,
        "completed_at": None,
    }
    db.collection("todos").add(doc_data)
    logger.info("Todo added by %s: %s", changed_by, text[:80])
    return f"Todo added: {text}"


def _pending_todos_text() -> str:
    """Compact catalog of pending todos, including ids — used in error messages."""
    with _cache_lock:
        pending = [t for t in _cache["todos"] if t.get("status") != "done"]
    if not pending:
        return "No pending todos."
    lines = [
        f"  - id={t.get('_id', '?')}  [{t.get('category', '')}] {t.get('text', '?')}"
        for t in pending
    ]
    return "Pending todos:\n" + "\n".join(lines)


def complete_todo(todo_id: str, changed_by: str = "cursor") -> str:
    """Mark a todo as done by exact Firestore document id.

    On miss / already-done, return the current pending catalog so the model can retry
    in the next turn without bothering the user.
    """
    db = get_db()
    doc_ref = db.collection("todos").document(todo_id)
    doc = doc_ref.get()
    if not doc.exists:
        return f"ERROR: todo_id '{todo_id}' not found.\n{_pending_todos_text()}"
    data = doc.to_dict() or {}
    text = data.get("text", todo_id)
    if data.get("status") == "done":
        return f"Already done: {text} (id={todo_id})"
    doc_ref.update({
        "status": "done",
        "completed_at": datetime.now(timezone.utc),
    })
    logger.info("Todo completed by %s: %s", changed_by, text[:80])
    return f"Done: {text}"


def complete_todo_by_text(search_text: str, changed_by: str = "cursor") -> str:
    """Lightweight CLI-only matcher.

    Used by `firestore_client.py complete-todo '<text>'` so humans typing on a
    keyboard get an ergonomic experience. The Stew bot does NOT use this — it
    calls `complete_todo(todo_id=...)` after `get_todos`.

    Behavior:
      1) Lowercased substring match against "category text".
      2) All whitespace tokens from the query appear in that combined string.
    """
    db = get_db()
    with _cache_lock:
        pending = [t for t in _cache["todos"] if t.get("status") != "done"]

    if not pending:
        docs = list(db.collection("todos").stream())
        pending = []
        for doc in docs:
            d = doc.to_dict()
            d["_id"] = doc.id
            if d.get("status") != "done":
                pending.append(d)

    search_lower = search_text.strip().lower()

    for todo in pending:
        combined = f"{todo.get('category', '')} {todo.get('text', '')}".lower()
        if search_lower in combined:
            return complete_todo(todo["_id"], changed_by)

    words = search_lower.split()
    if len(words) > 1:
        for todo in pending:
            combined = f"{todo.get('category', '')} {todo.get('text', '')}".lower()
            if all(w in combined for w in words):
                return complete_todo(todo["_id"], changed_by)

    return f"No pending todo matching '{search_text}'."


# ── Reminders ──────────────────────────────────────────────


def add_reminder(time_iso: str, message: str, chat_id: int) -> dict:
    """Add a reminder to Firestore. Returns the reminder dict."""
    db = get_db()
    fire_at = datetime.fromisoformat(time_iso)
    doc_data = {
        "fire_at": fire_at,
        "message": message,
        "chat_id": chat_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    }
    ref = db.collection("reminders").add(doc_data)
    doc_id = ref[1].id
    logger.info("Reminder added: %s at %s (id=%s)", message[:50], time_iso, doc_id)
    return {"id": doc_id, "time": time_iso, "message": message, "chat_id": chat_id}


def get_due_reminders() -> list[dict]:
    """Get all pending reminders whose fire_at has passed."""
    now = datetime.now(timezone.utc)
    with _cache_lock:
        return [
            r for r in _cache["reminders"]
            if r.get("status") == "pending" and _reminder_is_due(r, now)
        ]


def _reminder_is_due(reminder: dict, now: datetime) -> bool:
    """Check if a reminder's fire_at time has passed."""
    fire_at = reminder.get("fire_at")
    if fire_at is None:
        return False
    if hasattr(fire_at, "timestamp"):
        if not fire_at.tzinfo:
            fire_at = fire_at.replace(tzinfo=timezone.utc)
        return fire_at <= now
    try:
        return datetime.fromisoformat(str(fire_at)) <= now
    except (ValueError, TypeError):
        return False


def mark_reminder_fired(reminder_id: str) -> str:
    """Mark a reminder as fired."""
    db = get_db()
    db.collection("reminders").document(reminder_id).update({
        "status": "fired",
        "fired_at": datetime.now(timezone.utc),
    })
    logger.info("Reminder fired: %s", reminder_id)
    return f"Reminder {reminder_id} fired"


def get_pending_reminder_count() -> int:
    """Count of pending reminders (for /status display)."""
    with _cache_lock:
        return sum(1 for r in _cache["reminders"] if r.get("status") == "pending")


# ── Chat History ──────────────────────────────────────────


_HISTORY_DOC = "chat-history"
_MAX_HISTORY = 20


def load_chat_history() -> list[dict]:
    """Load conversation history from Firestore meta collection."""
    with _cache_lock:
        doc = _cache["meta"].get(_HISTORY_DOC)
    if doc and "messages" in doc:
        return list(doc["messages"])
    return []


def save_chat_history(history: list[dict]):
    """Write conversation history to Firestore meta collection."""
    db = get_db()
    trimmed = history[-_MAX_HISTORY:]
    db.collection("meta").document(_HISTORY_DOC).set({"messages": trimmed})


def append_chat_message(role: str, text: str):
    """Append a message to conversation history and save."""
    history = load_chat_history()
    history.append({"role": role, "text": text, "ts": datetime.now(timezone.utc).isoformat()})
    save_chat_history(history)


# ── Undo ──────────────────────────────────────────────────


def undo_last_change() -> str:
    """Revert the most recent changelog entry."""
    db = get_db()
    query = db.collection("changelog").order_by(
        "timestamp", direction=firestore.Query.DESCENDING
    ).limit(1)
    docs = list(query.stream())

    if not docs:
        return "No changes to undo."

    entry = docs[0].to_dict()
    collection = entry["collection"]
    doc_id = entry["doc_id"]
    changes = entry.get("changes", {})

    doc_ref = db.collection(collection).document(doc_id)
    restore = {}
    for field_path, previous_value in changes.items():
        if previous_value is None:
            restore[field_path] = firestore.DELETE_FIELD
        else:
            restore[field_path] = previous_value

    if restore:
        doc_ref.update(restore)

    db.collection("changelog").document(docs[0].id).delete()

    summary = entry.get("summary", "unknown change")
    logger.info("Undid: %s", summary)
    return f"Undid: {summary}"


# ── CLI Mode ──────────────────────────────────────────────


def _json_serial(obj):
    """JSON serializer for datetime and other non-standard types."""
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return str(obj)
    raise TypeError(f"Type {type(obj)} not serializable")


def _print_json(data):
    print(json.dumps(data, indent=2, default=_json_serial, ensure_ascii=False))


def cli_main():
    """CLI entry point for Cursor agent and manual use."""
    if len(sys.argv) < 2:
        print("Usage: python firestore_client.py <command> [args...]")
        print("")
        print("Read commands:")
        print("  read-plan              Compact plan overview")
        print("  read-day <N>           Full day details")
        print("  read-bookings          All bookings")
        print("  read-booking <id>      Single booking")
        print("  read-family            Family profiles")
        print("  needs-booking          What still needs booking")
        print("  read-notes             Recent notes")
        print("")
        print("Write commands:")
        print("  add-activity <day> <slug> '<json>'")
        print("  remove-activity <day> <slug>")
        print("  update-activity <day> <slug> <field> <value>")
        print("  add-booking <id> '<json>'")
        print("  update-booking <id> <field> <value>")
        print("  confirm-booking <id> --day <N> --activity <slug> --confirmation <code>")
        print("  add-note '<text>'")
        print("  undo")
        print("")
        print("Todo commands:")
        print("  todos                  List pending todos")
        print("  todos --all            List all todos (incl. done)")
        print("  add-todo '<text>' [category] [due-date]")
        print("  complete-todo '<search text>'")
        sys.exit(0)

    get_db()
    cmd = sys.argv[1]

    if cmd == "read-plan":
        init_cache()
        print(get_plan_summary())

    elif cmd == "read-day":
        day_num = int(sys.argv[2])
        init_cache()
        day = get_day(day_num)
        if day:
            _print_json(day)
        else:
            print(f"Day {day_num} not found")

    elif cmd == "read-bookings":
        init_cache()
        _print_json(get_all_bookings())

    elif cmd == "read-booking":
        booking_id = sys.argv[2]
        init_cache()
        b = get_booking(booking_id)
        if b:
            _print_json(b)
        else:
            print(f"Booking '{booking_id}' not found")

    elif cmd == "read-family":
        init_cache()
        _print_json(get_family())

    elif cmd == "needs-booking":
        init_cache()
        needs = get_needs_booking()
        if not needs:
            print("Everything is booked!")
        else:
            _print_json(needs)

    elif cmd == "read-notes":
        init_cache()
        _print_json(get_recent_notes())

    elif cmd == "add-activity":
        day_num = int(sys.argv[2])
        slug = sys.argv[3]
        data = json.loads(sys.argv[4])
        result = add_activity(day_num, slug, data, changed_by="cursor")
        print(result)

    elif cmd == "remove-activity":
        day_num = int(sys.argv[2])
        slug = sys.argv[3]
        result = remove_activity(day_num, slug, changed_by="cursor")
        print(result)

    elif cmd == "update-activity":
        day_num = int(sys.argv[2])
        slug = sys.argv[3]
        field = sys.argv[4]
        value = sys.argv[5]
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
        result = update_activity_field(day_num, slug, field, value, changed_by="cursor")
        print(result)

    elif cmd == "add-booking":
        booking_id = sys.argv[2]
        data = json.loads(sys.argv[3])
        result = add_booking(booking_id, data, changed_by="cursor")
        print(result)

    elif cmd == "update-booking":
        booking_id = sys.argv[2]
        field = sys.argv[3]
        value = sys.argv[4]
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
        result = update_booking_field(booking_id, field, value, changed_by="cursor")
        print(result)

    elif cmd == "confirm-booking":
        booking_id = sys.argv[2]
        args = sys.argv[3:]
        day_num = None
        activity_slug = None
        confirmation = None
        i = 0
        while i < len(args):
            if args[i] == "--day":
                day_num = int(args[i + 1])
                i += 2
            elif args[i] == "--activity":
                activity_slug = args[i + 1]
                i += 2
            elif args[i] == "--confirmation":
                confirmation = args[i + 1]
                i += 2
            else:
                i += 1
        if not all([day_num, activity_slug]):
            print("Error: --day and --activity are required")
            sys.exit(1)
        updates = {"status": "confirmed"}
        if confirmation:
            updates["confirmation"] = confirmation
        result = confirm_booking(booking_id, updates, day_num, activity_slug, changed_by="cursor")
        print(result)

    elif cmd == "confirm-activity":
        day_num = int(sys.argv[2])
        slug = sys.argv[3]
        code = sys.argv[4]
        details = sys.argv[5] if len(sys.argv) > 5 else ""
        cost = sys.argv[6] if len(sys.argv) > 6 else ""
        result = confirm_activity_booking(day_num, slug, code, details=details, cost=cost, changed_by="cursor")
        print(result)

    elif cmd == "add-note":
        text = sys.argv[2]
        result = add_note(text, author="cursor")
        print(result)

    elif cmd == "undo":
        result = undo_last_change()
        print(result)

    elif cmd == "todos":
        init_cache()
        include_done = "--all" in sys.argv
        todos = get_todos(include_done=include_done)
        if not todos:
            print("No pending todos!" if not include_done else "No todos at all.")
        else:
            for t in todos:
                status = "✓" if t.get("status") == "done" else "○"
                cat = t.get("category", "")
                due = f" (due {t['due_date']})" if t.get("due_date") else ""
                print(f"  {status} [{cat}] {t.get('text', '?')}{due}  [{t.get('_id', '?')}]")

    elif cmd == "add-todo":
        text = sys.argv[2]
        category = sys.argv[3] if len(sys.argv) > 3 else "prep"
        due_date = sys.argv[4] if len(sys.argv) > 4 else ""
        result = add_todo(text, category=category, due_date=due_date, changed_by="cursor")
        print(result)

    elif cmd == "complete-todo":
        search = sys.argv[2]
        init_cache()
        result = complete_todo_by_text(search, changed_by="cursor")
        print(result)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli_main()
