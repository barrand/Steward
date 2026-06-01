"""Stew Bot — Gemini agent with Firestore-backed function calling.

Handles:
- Lean system prompt built from in-memory Firestore cache
- Gemini function-calling loop with primary + fallback model (google.genai SDK)
- Inline keyboard confirmation flow for writes
- Conversation memory (rolling 20-message buffer, persisted to Firestore)
- Date-aware mode switching (pre-trip / during / post-trip)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time as _time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types

import firestore_client as fsc
from tools import (
    PendingWrite,
    execute_writes,
    prepare_add_activity,
    prepare_remove_activity,
    prepare_update_activity,
    prepare_update_booking,
    prepare_confirm_booking,
    prepare_confirm_activity,
    prepare_add_note,
    prepare_add_todo,
    prepare_complete_todo,
    get_todos_text,
    add_reminder,
    read_plan_summary,
    read_day,
    read_booking,
    get_needs_booking,
)

logger = logging.getLogger("tanuki.agent")

# ── Constants ────────────────────────────────────────────

TRIP_START = date(2026, 4, 22)
TRIP_END = date(2026, 5, 10)
JST = ZoneInfo("Asia/Tokyo")
PST = ZoneInfo("America/Los_Angeles")

CONFIRMATION_TIMEOUT_SEC = 300

# ── Gemini Setup ─────────────────────────────────────────

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

# Model selection. Primary handles the main reasoning; fallback is used
# automatically on 429 / 5xx / timeout from the primary so Stew can keep
# replying even when the frontier model is throttled or down.
#
# Override via env:
#   STEW_MODEL          (default: gemini-3.1-pro-preview) — used for /deep turns
#   STEW_FALLBACK_MODEL (default: gemini-2.5-flash)
#   STEW_SHALLOW_MODEL  (default: gemini-2.5-flash) — default chat (no /deep prefix)
#
# Set them equal to disable fallback entirely.
PRIMARY_MODEL = os.environ.get("STEW_MODEL", "gemini-3.1-pro-preview")
FALLBACK_MODEL = os.environ.get("STEW_FALLBACK_MODEL", "gemini-2.5-flash")
SHALLOW_MODEL = os.environ.get("STEW_SHALLOW_MODEL", "gemini-3.1-pro-preview")

# Leading `/deep` (case-insensitive) opts into PRIMARY_MODEL + extended thinking.
_DEEP_PREFIX_RE = re.compile(r"^\s*/deep(?:\s+|$)", re.IGNORECASE)


def strip_deep_command(user_text: str) -> tuple[bool, str]:
    """If the message starts with /deep, return (True, remainder). Otherwise (False, original)."""
    if not user_text:
        return False, user_text
    m = _DEEP_PREFIX_RE.match(user_text)
    if not m:
        return False, user_text
    rest = user_text[m.end() :].lstrip()
    return True, rest


def _log_usage(model_name: str, resp) -> None:
    """Log per-call token counts so we can see cost burn in the logs."""
    um = getattr(resp, "usage_metadata", None)
    if not um:
        return
    pt = getattr(um, "prompt_token_count", 0) or 0
    ct = getattr(um, "candidates_token_count", 0) or 0
    tht = getattr(um, "thoughts_token_count", 0) or 0
    tt = getattr(um, "total_token_count", 0) or (pt + ct + tht)
    logger.info(
        "tokens[%s] prompt=%d candidates=%d thoughts=%d total=%d",
        model_name, pt, ct, tht, tt,
    )


_RETRYABLE_HINTS = (
    "429", "rate", "quota",
    "500", "502", "503", "504",
    "unavailable", "internal", "deadline", "timeout",
)


def _is_retryable_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(h in s for h in _RETRYABLE_HINTS)


def _generate(contents, config, *, primary_model: str | None = None, fallback_model: str | None = None):
    """Call Gemini with automatic primary→fallback on transient errors.

    Returns the raw response. Raises if both models fail, or if the primary
    raises a non-retryable error (e.g. auth, bad request).

    If primary_model / fallback_model are None, uses module TANUKI_* env defaults.
    """
    primary = primary_model if primary_model is not None else PRIMARY_MODEL
    fallback = fallback_model if fallback_model is not None else FALLBACK_MODEL
    try:
        resp = client.models.generate_content(
            model=primary, contents=contents, config=config,
        )
        _log_usage(primary, resp)
        return resp
    except Exception as e:
        if primary == fallback or not _is_retryable_error(e):
            raise
        logger.warning(
            "Primary model %s failed (%s: %s) — falling back to %s",
            primary, type(e).__name__, str(e)[:200], fallback,
        )
        resp = client.models.generate_content(
            model=fallback, contents=contents, config=config,
        )
        _log_usage(fallback, resp)
        return resp

# Tool declarations — read tools execute immediately, write tools queue for confirmation
FUNCTION_TOOLS = [
    types.Tool(function_declarations=[
        # ── Read tools ──
        types.FunctionDeclaration(
            name="read_plan_summary",
            description="Get a compact one-line-per-day overview of the entire trip plan.",
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
        types.FunctionDeclaration(
            name="read_day",
            description="Get full details for a specific day: activities (with GPS, times, booking status), meals, transport notes.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "day_number": types.Schema(type=types.Type.INTEGER, description="Day number (1-19)"),
                },
                required=["day_number"],
            ),
        ),
        types.FunctionDeclaration(
            name="read_booking",
            description="Get full details for a specific accommodation: address, GPS, PIN, confirmation code, check-in/out times.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "booking_id": types.Schema(
                        type=types.Type.STRING,
                        description="Booking ID: lax-hotel, tokyo-arrival, osaka, kyoto, hiroshima, toyohashi, tokyo-main",
                    ),
                },
                required=["booking_id"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_needs_booking",
            description="List all activities and bookings that still need to be booked/confirmed.",
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),

        # ── Write tools ──
        types.FunctionDeclaration(
            name="add_activity",
            description=(
                "Add a new activity to a day's itinerary. User will be asked to confirm before saving.\n\n"
                "IMPORTANT: ALWAYS call read_day FIRST to see the existing schedule, "
                "then choose a time and sort_order that fit between other activities.\n\n"
                "REQUIRED for ALL activities:\n"
                "  • time — HH:MM that fits the day's schedule. ALWAYS provide a time. "
                "Estimate based on what comes before and after. Never leave it blank.\n"
                "  • gps — [lat, lng] of the location. ALWAYS provide GPS.\n\n"
                "ADDITIONALLY for type='food':\n"
                "  • specialty — what the place is famous for, e.g. 'Crispy tonkotsu ramen'\n"
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "day_number": types.Schema(type=types.Type.INTEGER, description="Day number (1-19)"),
                    "slug": types.Schema(type=types.Type.STRING, description="URL-safe ID like 'ramen-dinner' or 'meiji-shrine'"),
                    "name": types.Schema(type=types.Type.STRING, description="Human-readable activity name"),
                    "activity_type": types.Schema(type=types.Type.STRING, description="Type: food, temple, shrine, museum, shopping, transport, theme_park, nature, cultural, district, art, activity, market, memorial, aquarium, accommodation, travel, personal"),
                    "booking_required": types.Schema(type=types.Type.BOOLEAN, description="Whether this activity needs advance booking"),
                    "time": types.Schema(type=types.Type.STRING, description="Time in HH:MM format, e.g. '14:00'. ALWAYS provide this."),
                    "gps": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.NUMBER), description="[latitude, longitude]. ALWAYS provide this."),
                    "sort_order": types.Schema(type=types.Type.INTEGER, description="Position in the day's timeline. Check read_day output for existing sort_orders, then pick a value that places this activity chronologically. E.g. to insert between sort_order 3 and 4, use 4 and existing items will be bumped."),
                    "booking_ref": types.Schema(type=types.Type.STRING, description="Confirmation/reference number. Set this if the user has already provided a confirmation code — do NOT leave it blank when a confirmation is known."),
                    "notes": types.Schema(type=types.Type.STRING, description="Operational notes (Sam-safe, booking tips, etc.)"),
                    "specialty": types.Schema(type=types.Type.STRING, description="What the place is famous for — 1 sentence describing the food/experience. REQUIRED for food activities."),
                },
                required=["day_number", "slug", "name", "activity_type", "booking_required", "time", "gps"],
            ),
        ),
        types.FunctionDeclaration(
            name="remove_activity",
            description="Remove an activity from a day's itinerary. User will be asked to confirm.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "day_number": types.Schema(type=types.Type.INTEGER, description="Day number"),
                    "slug": types.Schema(type=types.Type.STRING, description="Activity slug to remove"),
                },
                required=["day_number", "slug"],
            ),
        ),
        types.FunctionDeclaration(
            name="update_activity",
            description="Update a single field of an existing activity. User will be asked to confirm.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "day_number": types.Schema(type=types.Type.INTEGER, description="Day number"),
                    "slug": types.Schema(type=types.Type.STRING, description="Activity slug"),
                    "field": types.Schema(type=types.Type.STRING, description="Field to update: name, time, type, notes, gps, gps_verified, booking_required, sort_order, specialty"),
                    "value": types.Schema(type=types.Type.STRING, description="New value (JSON-encoded for arrays/objects)"),
                },
                required=["day_number", "slug", "field", "value"],
            ),
        ),
        types.FunctionDeclaration(
            name="update_booking",
            description="Update a single field of an accommodation booking. User will be asked to confirm.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "booking_id": types.Schema(type=types.Type.STRING, description="Booking ID"),
                    "field": types.Schema(type=types.Type.STRING, description="Field to update"),
                    "value": types.Schema(type=types.Type.STRING, description="New value"),
                },
                required=["booking_id", "field", "value"],
            ),
        ),
        types.FunctionDeclaration(
            name="confirm_booking_link",
            description="Mark an ACCOMMODATION booking as confirmed AND link it to its activity. ONLY for hotels/ryokans in the bookings collection (IDs: lax-hotel, tokyo-arrival, osaka, kyoto, hiroshima, toyohashi, tokyo-main). Do NOT use for Shinkansen, tickets, or activity bookings — use confirm_activity instead.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "booking_id": types.Schema(type=types.Type.STRING, description="Booking ID (must exist in bookings collection)"),
                    "day_number": types.Schema(type=types.Type.INTEGER, description="Day the activity is on"),
                    "activity_slug": types.Schema(type=types.Type.STRING, description="Activity slug to link"),
                    "confirmation_code": types.Schema(type=types.Type.STRING, description="Booking confirmation code"),
                },
                required=["booking_id", "day_number", "activity_slug"],
            ),
        ),
        types.FunctionDeclaration(
            name="confirm_activity",
            description="Mark any activity as booked/confirmed — Shinkansen tickets, museum tickets, theme park passes, tour reservations, etc. Sets confirmation code + details on the activity itself. Use this for EVERYTHING except hotel/accommodation bookings.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "day_number": types.Schema(type=types.Type.INTEGER, description="Day number (1-19)"),
                    "slug": types.Schema(type=types.Type.STRING, description="Activity slug (e.g. 'shinkansen-osaka', 'usj', 'peace-museum'). Call read_day first if unsure of the slug."),
                    "confirmation_code": types.Schema(type=types.Type.STRING, description="Reservation/confirmation number or ID"),
                    "details": types.Schema(type=types.Type.STRING, description="Human-readable booking details: train times, seat numbers, costs, special notes — everything worth remembering"),
                    "cost": types.Schema(type=types.Type.STRING, description="Total cost as a string, e.g. '¥65,330' or '$450'"),
                },
                required=["day_number", "slug", "confirmation_code"],
            ),
        ),
        types.FunctionDeclaration(
            name="add_note",
            description="Save a timestamped note (e.g. a restaurant recommendation, packing reminder, or anything worth remembering).",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "text": types.Schema(type=types.Type.STRING, description="Note text"),
                },
                required=["text"],
            ),
        ),
        types.FunctionDeclaration(
            name="set_reminder",
            description="Set a timed reminder. Time should be ISO format in JST during the trip, PST otherwise.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "time": types.Schema(type=types.Type.STRING, description="ISO datetime, e.g. '2026-05-05T08:00:00+09:00'"),
                    "message": types.Schema(type=types.Type.STRING, description="Reminder message"),
                },
                required=["time", "message"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_todos",
            description="List all pending todo/action items for the trip.",
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
        types.FunctionDeclaration(
            name="add_todo",
            description="Add a todo/action item to the trip checklist. User will be asked to confirm.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "text": types.Schema(type=types.Type.STRING, description="Todo item text"),
                    "category": types.Schema(type=types.Type.STRING, description="Category: booking, prep, packing, transport, or other"),
                    "due_date": types.Schema(type=types.Type.STRING, description="Optional due date like '2026-04-20'"),
                },
                required=["text", "category"],
            ),
        ),
        types.FunctionDeclaration(
            name="complete_todo",
            description=(
                "Mark a todo as done by its exact Firestore document id. "
                "Always call get_todos FIRST in the same turn to get the list of "
                "ids (each line is `id=<id>  [category] text`), pick the one the "
                "user is referring to, then call complete_todo with that id. "
                "Never guess an id. Auto-saves — no user confirmation needed."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "todo_id": types.Schema(
                        type=types.Type.STRING,
                        description="Exact Firestore document id from get_todos output.",
                    ),
                },
                required=["todo_id"],
            ),
        ),
        types.FunctionDeclaration(
            name="undo_last_change",
            description="Undo the most recent data change. User will be asked to confirm.",
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
    ]),
]


# ── Date/Time Helpers ────────────────────────────────────

def get_mode() -> str:
    today = get_japan_date() if is_during_trip() else datetime.now(PST).date()
    if today < TRIP_START:
        return "pre_trip"
    elif today <= TRIP_END:
        return "during_trip"
    return "post_trip"


def is_during_trip() -> bool:
    return TRIP_START <= datetime.now(JST).date() <= TRIP_END


def get_japan_date() -> date:
    return datetime.now(JST).date()


def get_active_tz() -> ZoneInfo:
    return JST if is_during_trip() else PST


def get_now_str() -> str:
    tz = get_active_tz()
    now = datetime.now(tz)
    tz_name = "JST" if tz == JST else "PST"
    return now.strftime(f"%A %B %d, %Y %I:%M %p {tz_name}")


def get_trip_day_number() -> int | None:
    """Return current day number (1-19) if during the trip, else None."""
    today = datetime.now(JST).date()
    delta = (today - TRIP_START).days + 1
    if 1 <= delta <= 19:
        return delta
    return None


# ── System Prompt ────────────────────────────────────────

def build_system_prompt() -> str:
    plan_summary = fsc.get_plan_summary()
    needs = fsc.get_needs_booking()
    mode = get_mode()
    now_str = get_now_str()

    needs_text = ""
    if needs:
        items = [f"  - Day {n.get('day_number')}: {n.get('activity_name', n.get('booking_name', '?'))}" for n in needs]
        needs_text = "STILL NEEDS BOOKING:\n" + "\n".join(items)

    mode_instructions = {
        "pre_trip": (
            "We are BEFORE the trip. Focus on planning, logistics, and booking reminders. "
            "Nag about still-to-book items within 7 days of their date. Times are PST."
        ),
        "during_trip": (
            "We are IN JAPAN right now. Focus on real-time help: transit, food, what's nearby, "
            "today's plan, check-in info. Be concise — the family is on the go. Times are JST."
        ),
        "post_trip": (
            "The trip is over. Answer questions about what we did, costs, memories. No briefings."
        ),
    }

    day_hint = ""
    day_num = get_trip_day_number()
    if day_num:
        day_hint = f"\nToday is Day {day_num} of the trip."

    return f"""You are Tanuki, the Barrand family's Japan trip assistant in a Telegram group chat.

You are an AI with broad knowledge about Japan — weather, food, transit, culture, geography, history, and travel. Use this knowledge confidently. The trip data in Firestore (accessible via tools) contains booking-specific data. For everything else, answer from your general knowledge. NEVER say "I can't look that up" for general Japan questions.

CRITICAL: To update trip data, you MUST use the provided tools. NEVER claim you updated data without calling the tool. Reads are instant (cached). Most writes auto-save immediately — just tell the user what was done. Only booking confirmations (confirm_booking_link, confirm_activity, update_booking) require user confirmation via inline buttons.

MULTI-STEP OPERATIONS: When a user says "move X to Day Y", call BOTH remove_activity AND add_activity in the same turn. Both auto-save. Don't do half the work — complete the full operation in one response.

PERSONALITY: Warm, a little cheeky — like a well-prepared friend who knows Japan. Address family members by name. Light Japanese phrases OK.

BREVITY: Keep answers SHORT — 2-4 sentences for simple questions, bullet points for lists. No preamble, no filler, no restating the question. Get to the point fast. Only give longer answers when the user explicitly asks for detail or the question genuinely requires it.

NEVER generate text that looks like "📝 Proposed changes" or "Done! Added/Updated/Removed..." unless a tool ACTUALLY returned a SAVED result. The confirmation UI is handled by the system. If a user asks you to change something, CALL THE TOOL — do not describe the change in text.

FAMILY: Bryce (45, trip organizer, art/photography), Marcia (49, speaks Japanese, healing ACL), Sam (22, One Piece/anime, NO raw fish), Lucas (18, vintage thrift/sewing), Emi (11, sushi/animals/games). From Utah.

CURRENT TIME: {now_str}
MODE: {mode}
{mode_instructions.get(mode, "")}{day_hint}

TRIP OVERVIEW (use read_day for details):
{plan_summary}

{needs_text}

TRIP DASHBOARD: https://barrand-japan-trip.web.app (live checklist, maps, day-by-day view)

TRIP DAYS: 1–19 (Day 1 = Apr 22 SLC→LAX, Day 19 = May 10 LAX→SLC). Always call read_plan_summary if unsure which day a date falls on.

ACCOMMODATION BOOKING IDs: lax-hotel, tokyo-arrival, osaka, kyoto, hiroshima, toyohashi, tokyo-main
Use read_booking to fetch full details (address, PIN, confirmation, GPS).

CONFIRMING BOOKINGS — use the right tool:
- Hotels/accommodations already in the bookings collection → confirm_booking_link
- Shinkansen, museum tickets, USJ, tours, activity bookings → confirm_activity
- When the user pastes a Shinkansen confirmation, ALWAYS call read_day first to find the correct slug, then use confirm_activity with the reservation number and full details.

CRITICAL — when the user provides a confirmation number for ANYTHING:
- ALWAYS set booking_ref / confirmation_code immediately. Never add something as "needs booking" when the user is handing you proof it's already booked.
- If adding a new hotel/accommodation via add_activity, set booking_ref to the confirmation number in the same call. Do not add first and confirm later in two steps.
- If the hotel should also be in the bookings collection (for /hotel command), call add_activity with booking_ref AND separately note that the bookings collection may need updating.

RULES:
- For food recs, always note Sam-safe options (no raw fish) and what Emi will love.
- Include Google Maps links when you have GPS: https://maps.google.com/?q=LAT,LNG
- When answering from trip data, cite naturally ("Your Kyoto check-in is 3 PM").
- For general questions (weather, food, transit, culture), answer from your knowledge.
- Only say "I don't know" for trip-specific data not in Firestore.
- For transport: If it has a specific time (train departure, bus to USJ), add it as an activity with type="transport". Only use transport_notes for general day context ("local metro", "reserve seats").

COMPLETING A TODO — REQUIRED WORKFLOW:
When the user asks to mark/close/complete/finish a todo, ALWAYS in the same turn:
1) Call get_todos to fetch the live list (each line shows `id=<id>  [category] text`).
2) Pick the single todo that best matches the user's wording — you have the full list, so use your language understanding (synonyms, partial titles, plurals).
3) Call complete_todo with the exact `todo_id` from step 1. NEVER make up an id.
If nothing in the list plausibly matches, tell the user and show the candidates — do not call complete_todo.

ADDING FOOD/RESTAURANT ACTIVITIES — REQUIRED WORKFLOW:
Before adding any food activity, ALWAYS follow these steps:
1. Call read_day first — see what's already scheduled, what times are taken, where the family will be
2. Pick the right time — choose a slot that fits between existing activities. Think about travel time, meal timing (lunch ~12:00, dinner ~18:00), and proximity to nearby activities
3. Look up the restaurant — use your knowledge to find GPS coordinates and what the place is famous for
4. Call add_activity with ALL required fields:
   • time — HH:MM that makes sense in the day's flow
   • gps — [lat, lng] coordinates of the restaurant
   • specialty — 1-sentence blurb: what cuisine, what dish they're known for, why it's worth going
   • notes — Sam-safe status (no raw fish!), Emi/family relevance
The add_activity tool will REJECT food items missing time, gps, or specialty. Do the research first so it succeeds on the first call.
"""


# ── Conversation Memory (Firestore-backed) ───────────────

def append_to_history(role: str, text: str):
    fsc.append_chat_message(role, text)


def history_as_gemini_contents() -> list[types.Content]:
    history = fsc.load_chat_history()
    contents = []
    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg["text"])]))
    return contents


# ── Confirmation State Machine ───────────────────────────

_pending_confirmations: dict[int, dict] = {}


def set_pending_confirmation(user_id: int, writes: list[PendingWrite], summary: str):
    _pending_confirmations[user_id] = {
        "writes": writes,
        "created_at": _time.time(),
        "summary": summary,
    }


def get_pending_confirmation(user_id: int) -> dict | None:
    pending = _pending_confirmations.get(user_id)
    if pending is None:
        return None
    if _time.time() - pending["created_at"] > CONFIRMATION_TIMEOUT_SEC:
        clear_pending_confirmation(user_id)
        return None
    return pending


def clear_pending_confirmation(user_id: int):
    _pending_confirmations.pop(user_id, None)


def has_pending_confirmation(user_id: int) -> bool:
    return get_pending_confirmation(user_id) is not None


def handle_confirmation_callback(user_id: int, action: str) -> str:
    """Handle an inline keyboard callback. action is 'confirm' or 'cancel'."""
    pending = get_pending_confirmation(user_id)
    if pending is None:
        return "That confirmation expired. Ask me again if you still want the change."

    clear_pending_confirmation(user_id)

    if action == "confirm":
        result = execute_writes(pending["writes"])
        failure_phrases = ("no pending todo", "not found", "unknown operation", "failed:")
        if any(p in result.lower() for p in failure_phrases):
            append_to_history("assistant", f"[Confirmed via button] {result}")
            return result
        reply = f"Done! {result}"
        append_to_history("assistant", f"[Confirmed via button] {reply}")
        return reply
    else:
        append_to_history("assistant", "[Cancelled via button] Edit cancelled.")
        return "Got it — edit cancelled."


def handle_confirmation_response(user_id: int, text: str) -> tuple[str, bool]:
    """Fallback: process a text-based confirmation response."""
    pending = get_pending_confirmation(user_id)
    if pending is None:
        return "That confirmation expired. Ask me again if you still want the change.", False

    clear_pending_confirmation(user_id)

    import re
    cleaned = re.sub(r"@\w+", "", text).strip().lower()

    affirmative = {"yes", "y", "yep", "yeah", "sure", "do it", "go ahead",
                   "confirm", "ok", "approve", "lgtm", "save", "save it"}
    negative = {"no", "n", "nope", "cancel", "don't", "stop", "nah", "nevermind"}

    if cleaned in affirmative:
        result = execute_writes(pending["writes"])
        failure_phrases = ("no pending todo", "not found", "unknown operation", "failed:")
        if any(p in result.lower() for p in failure_phrases):
            append_to_history("user", text)
            append_to_history("assistant", result)
            return result, False
        reply = f"Done! {result}"
        append_to_history("user", text)
        append_to_history("assistant", reply)
        return reply, True
    elif cleaned in negative:
        append_to_history("user", text)
        append_to_history("assistant", "Edit cancelled.")
        return "Got it — edit cancelled.", False
    else:
        append_to_history("user", text)
        append_to_history("assistant", "Confirmation unclear — cancelled to be safe.")
        return "Wasn't sure if that was yes or no — cancelled to be safe. Ask again to retry.", False


# ── Gemini Tool Dispatch ─────────────────────────────────

def _auto_execute(pw: PendingWrite) -> str:
    """Execute a safe write immediately and return the result."""
    result = execute_writes([pw])
    logger.info("Auto-executed: %s → %s", pw.summary, result)
    return result


def _format_write_result(result: str, success_instruction: str) -> str:
    """Wrap a Firestore write result with proper success/failure framing for Gemini.

    Ensures the model sees 'FAILED:' and reports the real problem to the user
    instead of cheerfully saying 'Done!' when the underlying operation errored."""
    if result.startswith("ERROR:") or result.lower().startswith("failed"):
        return (
            f"FAILED: {result}. The change was NOT saved. "
            f"Do NOT tell the user it was done. Explain what went wrong and "
            f"(if a slug was wrong) call read_day to get the correct slug, then retry."
        )
    return f"SAVED: {result}. {success_instruction}"


# Mutable log collected by the receipt builder. Each entry is
# ("ok"|"failed", PendingWrite, raw_result_str). Populated by _execute_and_log
# whenever a write is auto-executed (success or failure) within one user turn.
WriteLog = list[tuple[str, "PendingWrite", str]]


def _execute_and_log(pw: PendingWrite, log: WriteLog, success_instruction: str) -> str:
    """Auto-execute a write, append the outcome to the receipt log, and return
    the SAVED/FAILED-framed text the model sees."""
    result = _auto_execute(pw)
    formatted = _format_write_result(result, success_instruction)
    status = "ok" if formatted.startswith("SAVED:") else "failed"
    log.append((status, pw, result))
    return formatted


def _dispatch_tool_call(
    name: str, args: dict, chat_id: int, executed_log: WriteLog | None = None,
) -> tuple[str, list[PendingWrite] | None]:
    """Execute a tool call. Returns (result_text, optional_pending_writes).
    Safe operations (adds, updates) auto-execute immediately and append their
    outcome to executed_log when provided.
    Destructive/high-stakes operations return pending writes for confirmation.
    """
    if executed_log is None:
        executed_log = []
    logger.info("Tool call: %s(%s)", name, str(args)[:200])
    try:
        # ── Read tools (instant) ──
        if name == "read_plan_summary":
            return read_plan_summary(), None

        elif name == "read_day":
            return read_day(args["day_number"]), None

        elif name == "read_booking":
            return read_booking(args["booking_id"]), None

        elif name == "get_needs_booking":
            return get_needs_booking(), None

        # ── Auto-execute (safe, non-destructive) ──
        elif name == "add_activity":
            gps = args.get("gps")
            if isinstance(gps, str):
                gps = json.loads(gps)
            atype = args["activity_type"]
            if atype == "food":
                missing = []
                if not args.get("time"):
                    missing.append("time (HH:MM)")
                if not gps:
                    missing.append("gps ([lat, lng])")
                if not args.get("specialty"):
                    missing.append("specialty (what the place is famous for)")
                if missing:
                    return (
                        f"REJECTED — food activities require: {', '.join(missing)}. "
                        "Look up the restaurant and call add_activity again with all fields filled in."
                    ), None
            pw = prepare_add_activity(
                day_number=args["day_number"],
                slug=args["slug"],
                name=args["name"],
                activity_type=args["activity_type"],
                booking_required=args["booking_required"],
                time=args.get("time", ""),
                gps=gps,
                notes=args.get("notes", ""),
                specialty=args.get("specialty", ""),
                sort_order=args.get("sort_order"),
                booking_ref=args.get("booking_ref", ""),
            )
            return _execute_and_log(pw, executed_log, "Tell the user what was added. Mention they can say 'undo' to revert."), None

        elif name == "update_activity":
            value = args["value"]
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
            pw = prepare_update_activity(args["day_number"], args["slug"], args["field"], value)
            return _execute_and_log(pw, executed_log, "Tell the user what changed. Mention they can say 'undo' to revert."), None

        elif name == "add_note":
            pw = prepare_add_note(args["text"])
            return _execute_and_log(pw, executed_log, ""), None

        elif name == "add_todo":
            pw = prepare_add_todo(
                text=args["text"],
                category=args.get("category", "prep"),
                due_date=args.get("due_date", ""),
            )
            return _execute_and_log(pw, executed_log, "Tell the user the todo was added."), None

        elif name == "remove_activity":
            pw = prepare_remove_activity(args["day_number"], args["slug"])
            return _execute_and_log(pw, executed_log, "Tell the user what was removed. Mention they can say 'undo' to revert."), None

        elif name == "complete_todo":
            pw = prepare_complete_todo(args["todo_id"])
            return _execute_and_log(pw, executed_log, ""), None

        # ── Needs confirmation (booking/financial) ──
        elif name == "update_booking":
            value = args["value"]
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
            pw = prepare_update_booking(args["booking_id"], args["field"], value)
            return "PENDING — NOT YET SAVED. Show the change and wait for confirmation.", [pw]

        elif name == "confirm_booking_link":
            pw = prepare_confirm_booking(
                booking_id=args["booking_id"],
                day_number=args["day_number"],
                activity_slug=args["activity_slug"],
                confirmation_code=args.get("confirmation_code", ""),
            )
            return "PENDING — NOT YET SAVED. Confirm with user before linking.", [pw]

        elif name == "confirm_activity":
            pw = prepare_confirm_activity(
                day_number=args["day_number"],
                slug=args["slug"],
                confirmation_code=args["confirmation_code"],
                details=args.get("details", ""),
                cost=args.get("cost", ""),
            )
            return "PENDING — NOT YET SAVED. Show the user what will be confirmed and wait for confirmation via the inline buttons.", [pw]

        elif name == "get_todos":
            return get_todos_text(include_ids=True), None

        elif name == "set_reminder":
            reminder = add_reminder(args["time"], args["message"], chat_id)
            return f"Reminder set for {args['time']}: {args['message']}", None

        elif name == "undo_last_change":
            pw = PendingWrite(
                operation="undo_last_change",
                args={},
                summary="Undo last change",
            )
            return "PENDING — Confirm undo?", [pw]

        else:
            return f"Unknown tool: {name}", None

    except ValueError as e:
        logger.error("Validation error for %s: %s", name, e)
        return f"Validation error: {e}", None
    except Exception as e:
        logger.error("Tool dispatch error for %s: %s", name, e, exc_info=True)
        return f"Tool error: {e}", None


# ── Main Processing Loop ─────────────────────────────────

# Action-verb prefix per PendingWrite.operation. Drives the receipt's first word
# so the user sees what actually happened, not LLM prose.
_RECEIPT_VERBS = {
    "add_activity": "Added",
    "remove_activity": "Removed",
    "update_activity_field": "Updated",
    "update_booking_field": "Updated",
    "confirm_booking": "Confirmed",
    "confirm_activity_booking": "Confirmed",
    "add_note": "Noted",
    "add_todo": "Added todo",
    "complete_todo": "Completed todo",
    "undo_last_change": "Undid",
}


def _build_receipt(executed_log: WriteLog) -> str:
    """Format a deterministic per-turn receipt from the dispatcher's write log.

    Each entry becomes one bullet:  ✓ <Verb>: <PendingWrite.summary>
    Failures get a ✗ prefix, the verb 'Failed', and a short reason from the result."""
    if not executed_log:
        return ""
    lines: list[str] = []
    for status, pw, raw in executed_log:
        verb = _RECEIPT_VERBS.get(pw.operation, "Did")
        if status == "ok":
            lines.append(f"\u2713 {verb}: {pw.summary}")
        else:
            reason = raw.replace("ERROR:", "").strip()
            if len(reason) > 140:
                reason = reason[:137] + "..."
            lines.append(f"\u2717 Failed ({verb.lower()}): {pw.summary} — {reason}")
    return "\n".join(lines)


async def process_message(
    user_text: str,
    user_id: int,
    chat_id: int,
    user_name: str = "someone",
    image_bytes: bytes | None = None,
    pdf_bytes: bytes | None = None,
) -> tuple[str, list[PendingWrite], bool]:
    """Send a message through Gemini with function calling.
    Returns (final_text, pending_writes, show_undo).
    show_undo is True when at least one write was successfully auto-executed
    this turn (the caller should attach an Undo button).

    Start the message with ``/deep`` to use the primary (Pro) model with extended
    thinking. All other messages use the shallow (flash) model without thinking.
    The ``/deep`` prefix is stripped before history and model context.
    """
    deep_requested, visible_text = strip_deep_command(user_text)

    append_to_history("user", f"[{user_name}] {visible_text}")

    system_prompt = build_system_prompt()
    history = history_as_gemini_contents()

    parts = []
    if image_bytes:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        parts.append(types.Part.from_text(text=visible_text or "What's in this image? If it's a booking confirmation, extract the details."))
    elif pdf_bytes:
        parts.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
        parts.append(types.Part.from_text(text=visible_text or "What's in this document? If it's a booking confirmation, extract the details."))
    else:
        parts.append(types.Part.from_text(text=f"[{user_name}] {visible_text}"))

    contents = history + [types.Content(role="user", parts=parts)]

    if deep_requested:
        gen_primary = PRIMARY_MODEL
        gen_fallback = FALLBACK_MODEL
        # -1 = dynamic: Pro uses this for multi-step itinerary edits.
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=FUNCTION_TOOLS,
            thinking_config=types.ThinkingConfig(thinking_budget=-1),
        )
        logger.info("Stew mode=deep primary=%s", gen_primary)
    else:
        # Fast path: flash, no thinking budget — use /deep for hard reasoning.
        gen_primary = SHALLOW_MODEL
        gen_fallback = PRIMARY_MODEL if SHALLOW_MODEL != PRIMARY_MODEL else FALLBACK_MODEL
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=FUNCTION_TOOLS,
        )
        logger.info("Stew mode=shallow primary=%s fallback=%s", gen_primary, gen_fallback)

    all_pending_writes: list[PendingWrite] = []
    executed_log: WriteLog = []

    try:
        response = _generate(
            contents, config,
            primary_model=gen_primary, fallback_model=gen_fallback,
        )
    except Exception as e:
        logger.error("Gemini API error: %s", e, exc_info=True)
        error_str = str(e).lower()
        if "429" in error_str or "rate" in error_str or "quota" in error_str:
            return "Both Gemini models are being rate-limited — try again in a minute.", [], False
        if "timeout" in error_str or "deadline" in error_str:
            return "Gemini is taking too long — try a simpler question.", [], False
        return f"Gemini error. Try again in a minute. ({type(e).__name__})", [], False

    max_rounds = 5
    for round_num in range(max_rounds):
        if not response.candidates:
            return "Gemini returned an empty response — try rephrasing.", [], False

        candidate = response.candidates[0]

        if not candidate.content or not candidate.content.parts:
            finish = getattr(candidate, 'finish_reason', '?')
            logger.warning("Gemini candidate has no content (finish_reason=%s) — retrying once", finish)
            if all_pending_writes:
                break
            # For MALFORMED_FUNCTION_CALL, add a corrective nudge so Gemini can try again cleanly.
            retry_contents = contents
            if str(finish).endswith("MALFORMED_FUNCTION_CALL"):
                retry_contents = contents + [types.Content(
                    role="user",
                    parts=[types.Part.from_text(
                        text="Your previous function call was malformed. Please try again with properly formatted arguments, or respond in plain text if no tool is needed."
                    )],
                )]
            try:
                response = _generate(
                    retry_contents, config,
                    primary_model=gen_primary, fallback_model=gen_fallback,
                )
                candidate = response.candidates[0] if response.candidates else None
                if not candidate or not candidate.content or not candidate.content.parts:
                    logger.warning("Retry also returned empty content (finish=%s)",
                                   getattr(candidate, 'finish_reason', '?') if candidate else 'no-candidate')
                    return "Gemini gave me an empty response twice in a row. Try rephrasing the question.", [], False
                contents = retry_contents
            except Exception as e:
                logger.error("Retry after empty content failed: %s", e)
                return "Gemini returned an incomplete response — try again.", [], False

        fn_calls = [p.function_call for p in candidate.content.parts if p.function_call]

        if not fn_calls:
            break

        logger.info("Round %d: %d function call(s)", round_num, len(fn_calls))

        fn_response_parts = []
        for fc in fn_calls:
            args = dict(fc.args) if fc.args else {}
            result_text, pending = _dispatch_tool_call(fc.name, args, chat_id, executed_log)
            if pending:
                all_pending_writes.extend(pending)
            fn_response_parts.append(
                types.Part.from_function_response(
                    name=fc.name,
                    response={"result": result_text},
                )
            )

        contents.append(candidate.content)
        contents.append(types.Content(role="user", parts=fn_response_parts))

        try:
            response = _generate(
                contents, config,
                primary_model=gen_primary, fallback_model=gen_fallback,
            )
        except Exception as e:
            logger.error("Gemini follow-up error: %s", e, exc_info=True)
            if all_pending_writes:
                break
            return "Gemini hit an error during follow-up. Try again.", [], False

    # Extract Gemini's free-form text (only used for read-only chat turns now)
    model_text = ""
    if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
        for part in response.candidates[0].content.parts:
            if part.text:
                model_text += part.text

    # ── Three-way reply branch ─────────────────────────────
    # 1. Pending writes → confirmation prompt (deterministic, ignore model prose)
    # 2. Auto-executed and/or failed writes → deterministic receipt
    # 3. Pure read/chat turn → return model_text as-is
    show_undo = False
    has_ok = any(s == "ok" for s, _, _ in executed_log)

    if all_pending_writes:
        bullet_list = "\n".join(f"  \u2022 {w.summary}" for w in all_pending_writes)
        final_text = (
            f"\U0001f4dd Proposed changes:\n{bullet_list}\n\n"
            "\u26a0\ufe0f Nothing saved yet. Tap Save below, or reply \"yes\" / \"no\"."
        )
    elif executed_log:
        final_text = _build_receipt(executed_log)
        show_undo = has_ok
    else:
        final_text = model_text or "I got a response but couldn't extract text. Try asking again."

        # Read-only chat turn: log-only hallucination detection (no override).
        # Past-tense write-intent regex. Catches "I've added/updated/saved/marked/set/
        # removed/deleted/scheduled/booked/confirmed/verified X" and similar.
        text_lower = final_text.lower()
        fake_confirmation = "\U0001f4dd proposed changes" in text_lower or "proposed changes:\n" in text_lower
        write_verb_re = re.compile(
            r"\b(?:i(?:'ve| have)?|we(?:'ve| have)?)\s+(?:just\s+|now\s+|already\s+|successfully\s+)?"
            r"(?:added|updated|saved|set|marked|removed|deleted|scheduled|booked|confirmed|verified|"
            r"created|inserted|fixed|edited|changed|modified|written|recorded|logged|noted|stored|"
            r"committed|pushed|persisted|patched|amended|cleared|cancelled|rescheduled)\b",
            re.IGNORECASE,
        )
        passive_re = re.compile(
            r"\b(?:has|have|is|are)\s+(?:been\s+|now\s+been\s+|already\s+been\s+)?"
            r"(?:added|updated|saved|set|marked|removed|deleted|verified|confirmed|"
            r"booked|scheduled|fixed|changed|modified|recorded|stored|patched)\b",
            re.IGNORECASE,
        )
        write_claim = bool(write_verb_re.search(final_text) or passive_re.search(final_text))
        if fake_confirmation or write_claim:
            logger.warning(
                "POSSIBLE HALLUCINATION on read-only turn (writes=0, claim=%s, fake_confirm=%s): %s",
                write_claim, fake_confirmation, final_text[:300],
            )

    # When the user invoked /deep, prepend a marker so they know the heavier
    # thinking budget was actually used. Stored in history too so the model
    # can see its own past depth signals.
    if deep_requested:
        final_text = "\U0001f9e0 I thought deeply about it...\n\n" + final_text

    append_to_history("assistant", final_text)
    return final_text, all_pending_writes, show_undo


async def generate_nightly_briefing() -> str:
    mode = get_mode()
    if mode == "post_trip":
        return ""

    now_str = get_now_str()

    if mode == "pre_trip":
        prompt = (
            f"It is {now_str}. Generate a brief pre-trip nightly briefing for the Barrand family. "
            "Include: days until departure, any still-to-book items that are urgent (within 7 days), "
            "and one helpful prep tip. Keep it under 200 words."
        )
    else:
        tomorrow = get_japan_date() + timedelta(days=1)
        prompt = (
            f"It is {now_str}. Tomorrow is {tomorrow.strftime('%A %B %d')}. "
            "Generate a nightly briefing. Include: tomorrow's plan summary, "
            "check-in/check-out times, food notes for Sam (no raw fish), "
            "any reminders for tomorrow. Keep it under 250 words."
        )

    text, _, _ = await process_message(
        user_text=prompt,
        user_id=0,
        chat_id=int(os.environ.get("TELEGRAM_CHAT_ID", "0")),
        user_name="Stew (auto-briefing)",
    )
    return text
