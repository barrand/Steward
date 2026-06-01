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

    def _on_members(col_snapshot, changes, read_time):
        with _cache_lock:
            for change in changes:
                doc = change.document
                if change.type.name in ("ADDED", "MODIFIED"):
                    _cache["members"][doc.id] = doc.to_dict()
                elif change.type.name == "REMOVED":
                    _cache["members"].pop(doc.id, None)
        _mark_loaded("members")
        logger.debug("Members cache updated: %d members", len(_cache["members"]))

    def _on_interviews(col_snapshot, changes, read_time):
        with _cache_lock:
            all_interviews = []
            for doc in col_snapshot:
                d = doc.to_dict()
                d["_id"] = doc.id
                all_interviews.append(d)
            _cache["interviews"] = sorted(
                all_interviews,
                key=lambda i: i.get("date", ""),
                reverse=True,
            )[:200]
        _mark_loaded("interviews")
        logger.debug("Interviews cache updated: %d interviews", len(_cache["interviews"]))

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
            )[:200]
        _mark_loaded("notes")
        logger.debug("Notes cache updated: %d notes", len(_cache["notes"]))

    def _on_prayers(col_snapshot, changes, read_time):
        with _cache_lock:
            all_prayers = []
            for doc in col_snapshot:
                d = doc.to_dict()
                if d.get("status") == "pending":  # Only cache pending prayers
                    d["_id"] = doc.id
                    all_prayers.append(d)
            _cache["prayer_requests"] = sorted(
                all_prayers,
                key=lambda p: p.get("next_remind_date", ""),
            )
        _mark_loaded("prayer_requests")
        logger.debug("Prayers cache updated: %d pending", len(_cache["prayer_requests"]))

    def _on_followups(col_snapshot, changes, read_time):
        with _cache_lock:
            all_followups = []
            for doc in col_snapshot:
                d = doc.to_dict()
                if d.get("status") == "pending":  # Only cache pending follow-ups
                    d["_id"] = doc.id
                    all_followups.append(d)
            _cache["follow_ups"] = sorted(
                all_followups,
                key=lambda f: f.get("due_date", ""),
            )
        _mark_loaded("follow_ups")
        logger.debug("Follow-ups cache updated: %d pending", len(_cache["follow_ups"]))

    def _on_meta(col_snapshot, changes, read_time):
        with _cache_lock:
            for change in changes:
                doc = change.document
                if change.type.name in ("ADDED", "MODIFIED"):
                    _cache["meta"][doc.id] = doc.to_dict()
                elif change.type.name == "REMOVED":
                    _cache["meta"].pop(doc.id, None)
        _mark_loaded("meta")

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

    _listeners.append(db.collection("members").on_snapshot(_on_members))
    _listeners.append(db.collection("interviews").on_snapshot(_on_interviews))
    _listeners.append(db.collection("notes").on_snapshot(_on_notes))
    _listeners.append(db.collection("prayer_requests").on_snapshot(_on_prayers))
    _listeners.append(db.collection("follow_ups").on_snapshot(_on_followups))
    _listeners.append(db.collection("meta").on_snapshot(_on_meta))
    _listeners.append(db.collection("reminders").on_snapshot(_on_reminders))

    _cache_ready.wait(timeout=15)
    logger.info(
        "Cache ready: %d members, %d interviews, %d prayers, %d follow-ups",
        len(_cache["members"]),
        len(_cache["interviews"]),
        len(_cache["prayer_requests"]),
        len(_cache["follow_ups"]),
    )


# ── Read Layer (from cache) ───────────────────────────────

def get_member(name_or_id: str) -> dict | None:
    """Get member by name or ID."""
    with _cache_lock:
        # First try ID
        if name_or_id in _cache["members"]:
            return _cache["members"][name_or_id]
        # Fuzzy match by name
        for mid, m in _cache["members"].items():
            if name_or_id.lower() in m.get("name", "").lower():
                return m
    return None


def get_all_members() -> dict:
    """All members from cache."""
    with _cache_lock:
        return dict(_cache["members"])


def get_member_interviews(member_id: str) -> list[dict]:
    """All interviews for a member."""
    with _cache_lock:
        return [i for i in _cache["interviews"] if i.get("member_id") == member_id]


def get_pending_follow_ups() -> list[dict]:
    """All pending follow-ups, sorted by due date."""
    with _cache_lock:
        return list(_cache["follow_ups"])


def get_upcoming_birthdays(days: int = 7) -> list[dict]:
    """Members with birthdays in next N days."""
    # TODO: implement
    with _cache_lock:
        return [m for m in _cache["members"].values() if m.get("birthday")][:10]


def get_pending_reminder_count() -> int:
    """Count of pending reminders."""
    with _cache_lock:
        return len([r for r in _cache["reminders"] if r.get("status") == "pending"])


def get_due_reminders() -> list[dict]:
    """Reminders due now or earlier."""
    now = datetime.now(timezone.utc)
    with _cache_lock:
        return [
            r for r in _cache["reminders"]
            if r.get("status") == "pending" and r.get("fire_at", "") <= now.isoformat()
        ]


# ── Write Layer (all log to changelog) ────────────────────

def add_reminder(time_iso: str, message: str, chat_id: int) -> dict:
    """Add a reminder to Firestore."""
    db = get_db()
    reminder = {
        "fire_at": time_iso,
        "message": message,
        "chat_id": chat_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    ref = db.collection("reminders").add(reminder)
    logger.info("Added reminder: %s", message[:50])
    return reminder


def mark_reminder_fired(reminder_id: str):
    """Mark a reminder as fired."""
    db = get_db()
    db.collection("reminders").document(reminder_id).update({"status": "fired"})
    logger.info("Marked reminder %s as fired", reminder_id)


def save_chat_message(role: str, text: str):
    """Append a message to chat history (last 20)."""
    db = get_db()
    meta_ref = db.collection("meta").document("chat-history")
    meta_doc = meta_ref.get()
    history = []
    if meta_doc.exists:
        history = meta_doc.get("messages", [])
    history.append({
        "role": role,
        "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    history = history[-20:]  # Keep last 20
    meta_ref.set({"messages": history}, merge=True)


def load_chat_history() -> list[dict]:
    """Load chat history from cache."""
    with _cache_lock:
        meta = _cache.get("meta", {}).get("chat-history", {})
        return meta.get("messages", [])


def append_chat_message(role: str, text: str):
    """Append to chat history."""
    save_chat_message(role, text)


def undo_last_change() -> str:
    """Undo last change (stub)."""
    return "Undo not yet implemented."


# ── CLI Mode ──────────────────────────────────────────────

def cli_main():
    """Command-line interface for manual operations."""
    if len(sys.argv) < 2:
        print("Usage: python firestore_client.py <command>")
        print("Commands: import-csv <file>, list-members, undo-last")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "import-csv":
        print("CSV import not yet implemented")

    elif cmd == "list-members":
        init_cache()
        members = get_all_members()
        if not members:
            print("No members in cache.")
        else:
            for mid, m in members.items():
                name = m.get("name", "?")
                bday = m.get("birthday", "")
                print(f"  {name} ({bday})")

    elif cmd == "undo-last":
        result = undo_last_change()
        print(result)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli_main()
