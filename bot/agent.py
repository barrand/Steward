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
    add_reminder,
    get_member_summary,
    get_pending_follow_ups,
    get_upcoming_birthdays,
)

logger = logging.getLogger("stew.agent")

# ── Constants ────────────────────────────────────────────

MT = ZoneInfo("America/Denver")
CONFIRMATION_TIMEOUT_SEC = 300

# ── Gemini Setup ─────────────────────────────────────────

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))

# Model selection. Primary handles the main reasoning; fallback is used
# automatically on 429 / 5xx / timeout from the primary so Stew can keep
# replying even when the frontier model is throttled or down.
#
# Override via env:
#   STEW_MODEL          (default: gemini-2.5-flash)
#   STEW_FALLBACK_MODEL (default: gemini-2.5-flash)
#
# Set them equal to disable fallback entirely.
PRIMARY_MODEL = os.environ.get("STEW_MODEL", "gemini-2.5-flash")
FALLBACK_MODEL = os.environ.get("STEW_FALLBACK_MODEL", "gemini-2.5-flash")

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

# Tool declarations (TODO: implement Stew tools — interview, prayer, followup, member, calendar, reminder)
# For now, minimal stub to allow bot to start
FUNCTION_TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="set_reminder",
            description="Set a timed reminder.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "time": types.Schema(type=types.Type.STRING, description="ISO datetime, e.g. '2026-05-05T14:00:00-07:00'"),
                    "message": types.Schema(type=types.Type.STRING, description="Reminder message"),
                },
                required=["time", "message"],
            ),
        ),
    ]),
]


# ── Date/Time Helpers ────────────────────────────────────

def get_now_str() -> str:
    """Current time in Mountain Time."""
    now = datetime.now(MT)
    return now.strftime("%A %B %d, %Y %I:%M %p MT")


# ── System Prompt ────────────────────────────────────────

def build_system_prompt() -> str:
    """Build system prompt with Firestore cache context."""
    now_str = get_now_str()
    members = fsc._cache.get("members", {})
    follow_ups = fsc._cache.get("follow_ups", [])
    prayers = fsc._cache.get("prayer_requests", [])

    members_list = ", ".join(m.get("name", "?") for m in members.values())[:200]

    return f"""You are Stew, a personal assistant for Bryce, an Elder's Quorum president.

Your job: Help Bryce remember and follow up on his quorum members. He conducts annual interviews where he learns about their work, family, health, faith — and what to pray for. You help him capture these notes naturally, track prayers, and schedule follow-ups.

MEMBER DIRECTORY:
{members_list}

CURRENT TIME: {now_str} (Mountain Time)

PERSONALITY: Warm, spiritually supportive, understanding of LDS pastoral context. Keep responses concise — Bryce is busy.

CAPTURING INTERVIEWS — your most important job:
When Bryce tells you about a meeting with a member, extract:
- Work situation and stress
- Family updates (spouse, kids, activities)
- Health topics
- Faith/testimony notes
- Prayer requests (be specific: "knee surgery" not "health")

Always show a confirmation summary before saving.

TOOL HINTS (implementation in progress):
- set_reminder: Schedule a reminder (ISO format, MT time)
- [TODO] save_interview, save_note, add_prayer_request, schedule_follow_up, etc.

RULES:
- ALWAYS resolve member names against the directory before assuming
- If ambiguous, ask which member
- For prayer requests, extract specific actionable items
- Never fabricate notes — only save what Bryce tells you
- Mark follow-ups done when Bryce says he talked to someone
- Suggest marking prayers answered when Bryce mentions positive outcomes
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
