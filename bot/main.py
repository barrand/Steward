"""Stew — Elder's Quorum Assistant Bot

Telegram bot entry point. Dual-mode deployment:
- Webhook (default): Starlette + Uvicorn for Cloud Run deployment
- Polling (--polling): Traditional long-polling for local dev

Features:
- Chat ID whitelist
- Fast-path commands backed by Firestore cache
- Text / photo / CSV handlers routed through Gemini agent
- Inline keyboard confirmation flow for writes
- Typing indicator while processing
- Message debouncing (2s after last message)
- Serialization lock for concurrent messages

Adapted from Stew trip bot (JapanTrip/bot/main.py).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import firestore_client as fsc
from agent import (
    get_now_str,
    has_pending_confirmation,
    handle_confirmation_callback,
    handle_confirmation_response,
    process_message,
    set_pending_confirmation,
)
from tools import (
    get_member_summary,
    get_pending_follow_ups,
    get_upcoming_birthdays,
)

logger = logging.getLogger("stew.main")

CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "stewbot")
START_TIME = time.time()

_processing_lock = asyncio.Lock()
_debounce_tasks: dict[int, asyncio.Task] = {}
_seen_update_ids: set[int] = set()
_MAX_SEEN_IDS = 200

HELP_TEXT = """🙏 *Stew — Elder's Quorum Assistant*

Commands:
`/member <name>` — Member profile + interview history
`/status` — Bot health + pending items
`/upcoming` — Birthdays coming up (7 days)
`/help` — This message

*Or just message naturally:*
• "Just met with John Smith. Works at the bank, stressed about layoffs..."
• "His knee surgery went great!"
• "Remind me to follow up with David in 3 weeks"
• Send a booking screenshot for me to process

Stew will extract interview notes, track prayers, schedule follow-ups, and give you daily reminders.
"""

CONFIRM_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("✅ Save", callback_data="confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ]
])

UNDO_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("↻ Undo", callback_data="undo")]
])


# ── Authorization ────────────────────────────────────────

def is_authorized(update: Update) -> bool:
    if CHAT_ID == 0:
        return True
    return update.effective_chat and update.effective_chat.id == CHAT_ID


# ── Fast-Path Commands ───────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    uptime_sec = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_sec, 3600)
    minutes, secs = divmod(remainder, 60)

    pending_count = fsc.get_pending_reminder_count()
    now_str = get_now_str()
    members_count = len(fsc._cache.get("members", {}))
    follow_ups = fsc._cache.get("follow_ups", [])
    prayers = fsc._cache.get("prayer_requests", [])

    text = (
        f"*Stew Status* 🙏\n\n"
        f"Uptime: {hours}h {minutes}m {secs}s\n"
        f"Time: {now_str}\n"
        f"Data: Firestore ({members_count} members cached)\n"
        f"Follow-ups: {len([f for f in follow_ups if f.get('status') == 'pending'])}\n"
        f"Prayers: {len([p for p in prayers if p.get('status') == 'pending'])}\n"
        f"Pending reminders: {pending_count}\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Usage: `/member <name>`", parse_mode=ParseMode.MARKDOWN)
        return

    summary = get_member_summary(text)
    if summary:
        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"Member '{text}' not found.")


async def cmd_upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    birthdays = get_upcoming_birthdays(days=7)
    if not birthdays:
        await update.message.reply_text("No upcoming birthdays in the next 7 days. ✓")
        return

    lines = ["🎂 *Upcoming Birthdays (7 days)*\n"]
    for member in birthdays:
        name = member.get("name", "?")
        bday = member.get("birthday", "")
        lines.append(f"• {name} — {bday[-5:]}")  # MM-DD

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Inline Keyboard Callback Handler ─────────────────────

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses for confirmations."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id if query.from_user else 0
    action = query.data

    if action == "undo":
        from firestore_client import undo_last_change
        from agent import append_to_history
        try:
            result = undo_last_change()
        except Exception as e:
            logger.error("Undo failed: %s", e, exc_info=True)
            result = f"Undo failed: {e}"
        append_to_history("assistant", f"[Undo via button] {result}")
        original = query.message.text or ""
        await query.edit_message_text(
            text=f"{original}\n\n↻ {result}",
            reply_markup=None,
        )
        return

    if action not in ("confirm", "cancel"):
        return

    result = handle_confirmation_callback(user_id, action)

    await query.edit_message_text(
        text=result,
        reply_markup=None,
    )


# ── Message Filtering ────────────────────────────────────

def should_respond(update: Update) -> bool:
    msg = update.message
    if not msg:
        return False
    return True


# ── Main Message Handler ─────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not should_respond(update):
        return

    msg = update.message
    user_id = msg.from_user.id if msg.from_user else 0
    user_name = msg.from_user.first_name if msg.from_user else "someone"
    text_preview = (msg.text or "")[:60]
    logger.info("handle_message from user=%d (%s): %s", user_id, user_name, text_preview)

    if has_pending_confirmation(user_id):
        text = msg.text or msg.caption or ""
        logger.info("Routing to text confirmation for user=%d", user_id)
        response, confirmed = handle_confirmation_response(user_id, text)
        await msg.reply_text(response)
        return

    if user_id in _debounce_tasks:
        _debounce_tasks[user_id].cancel()

    async def _process_after_debounce():
        await asyncio.sleep(2)
        if has_pending_confirmation(user_id):
            text = msg.text or msg.caption or ""
            response, confirmed = handle_confirmation_response(user_id, text)
            await msg.reply_text(response)
            return
        async with _processing_lock:
            await _do_process(update, context, user_id, user_name)

    _debounce_tasks[user_id] = asyncio.create_task(_process_after_debounce())


async def _do_process(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, user_name: str):
    msg = update.message
    text = (msg.text or msg.caption or "").replace(f"@{BOT_USERNAME}", "").strip()
    chat_id = msg.chat_id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    image_bytes = None
    csv_bytes = None

    if msg.photo:
        try:
            photo = msg.photo[-1]
            file = await photo.get_file()
            buf = io.BytesIO()
            await file.download_to_memory(buf)
            image_bytes = buf.getvalue()
            if not text:
                text = "What's in this image? If it's a booking confirmation, extract the details."
        except Exception as e:
            logger.error("Photo download failed: %s", e)
            await msg.reply_text("I couldn't download that photo. Try sending it again.")
            return

    elif msg.document:
        doc = msg.document
        if doc.mime_type == "text/csv" or doc.file_name.endswith(".csv"):
            try:
                file = await doc.get_file()
                buf = io.BytesIO()
                await file.download_to_memory(buf)
                csv_bytes = buf.getvalue()
                if not text:
                    text = f"I got a CSV file: {doc.file_name}. Process it for me."
            except Exception as e:
                logger.error("CSV download failed: %s", e)
                await msg.reply_text("I couldn't download that CSV. Try sending it again.")
                return
        else:
            await msg.reply_text(f"I can handle photos and CSVs, but not {doc.mime_type} files.")
            return

    if not text and not image_bytes and not csv_bytes:
        return

    try:
        response_text, pending_writes, show_undo = await process_message(
            user_text=text,
            user_id=user_id,
            chat_id=chat_id,
            user_name=user_name,
            image_bytes=image_bytes,
            csv_bytes=csv_bytes,
        )

        reply_markup = None
        if pending_writes:
            set_pending_confirmation(user_id, pending_writes,
                                     "; ".join(w.summary for w in pending_writes))
            reply_markup = CONFIRM_KEYBOARD
            logger.info("Sending confirmation with inline keyboard (%d writes) for user=%d", len(pending_writes), user_id)
        elif show_undo:
            reply_markup = UNDO_KEYBOARD
            logger.info("Sending receipt with undo button for user=%d", user_id)

        if len(response_text) <= 4096:
            await msg.reply_text(response_text, reply_markup=reply_markup)
        else:
            chunks = [response_text[i:i+4096] for i in range(0, len(response_text), 4096)]
            for i, chunk in enumerate(chunks):
                rm = reply_markup if i == len(chunks) - 1 else None
                await msg.reply_text(chunk, reply_markup=rm)
                await asyncio.sleep(0.5)

    except Exception as e:
        logger.error("Message processing failed: %s", e, exc_info=True)
        err_str = str(e).lower()
        if "429" in err_str or "rate" in err_str or "quota" in err_str:
            await msg.reply_text("Gemini is rate-limited right now. Try again in 30 seconds.")
        elif "timeout" in err_str or "deadline" in err_str:
            await msg.reply_text("Request timed out (Gemini was too slow). Try again — it usually works on retry.")
        elif "503" in err_str or "unavailable" in err_str:
            await msg.reply_text("Gemini is temporarily unavailable. Give it a minute and try again.")
        else:
            await msg.reply_text(
                f"Something went wrong ({type(e).__name__}). "
                "Error logged. Try again — it usually works on retry."
            )


# ── PTB Application Builder ─────────────────────────────

def _build_ptb_app() -> Application:
    """Build and configure the python-telegram-bot Application."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    if not os.environ.get("GEMINI_API_KEY"):
        logger.error("GEMINI_API_KEY not set")
        sys.exit(1)

    if CHAT_ID == 0:
        logger.warning("TELEGRAM_CHAT_ID not set — bot will respond to ALL chats")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("member", cmd_member))
    app.add_handler(CommandHandler("upcoming", cmd_upcoming))

    app.add_handler(CallbackQueryHandler(handle_callback_query))

    app.add_handler(MessageHandler(
        filters.TEXT | filters.PHOTO | filters.Document.ALL,
        handle_message,
    ))

    return app


# ── Webhook Mode (Cloud Run) ────────────────────────────

_ptb_app: Application | None = None

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")


def _build_starlette_app():
    """Build the Starlette ASGI app for Cloud Run."""
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    @asynccontextmanager
    async def lifespan(app):
        global _ptb_app
        fsc.configure_logging()
        logger.info("Initializing Firestore cache...")
        fsc.init_cache()

        _ptb_app = _build_ptb_app()
        await _ptb_app.initialize()
        await _ptb_app.start()
        logger.info("Stew webhook ready (chat_id=%s)", CHAT_ID)
        yield
        if _ptb_app:
            await _ptb_app.stop()
            await _ptb_app.shutdown()

    async def webhook(request: Request):
        if WEBHOOK_SECRET:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if token != WEBHOOK_SECRET:
                return PlainTextResponse("Forbidden", status_code=403)

        data = await request.json()
        update_id = data.get("update_id")
        if update_id and update_id in _seen_update_ids:
            logger.info("Duplicate update_id %s — skipping", update_id)
            return PlainTextResponse("OK")
        if update_id:
            _seen_update_ids.add(update_id)
            if len(_seen_update_ids) > _MAX_SEEN_IDS:
                oldest = sorted(_seen_update_ids)[:_MAX_SEEN_IDS // 2]
                _seen_update_ids.difference_update(oldest)

        update = Update.de_json(data, _ptb_app.bot)
        await _ptb_app.process_update(update)
        return PlainTextResponse("OK")

    async def cron_morning_checkin(request: Request):
        if CRON_SECRET:
            token = request.headers.get("X-Cron-Secret", "")
            if token != CRON_SECRET:
                return PlainTextResponse("Forbidden", status_code=403)

        from reminders import generate_morning_checkin
        sent = await generate_morning_checkin(_ptb_app.bot)
        return JSONResponse({"sent": sent})

    async def cron_birthday_check(request: Request):
        if CRON_SECRET:
            token = request.headers.get("X-Cron-Secret", "")
            if token != CRON_SECRET:
                return PlainTextResponse("Forbidden", status_code=403)

        from reminders import check_and_send_birthdays
        fired = await check_and_send_birthdays(_ptb_app.bot)
        return JSONResponse({"fired": fired})

    async def cron_evening_briefing(request: Request):
        if CRON_SECRET:
            token = request.headers.get("X-Cron-Secret", "")
            if token != CRON_SECRET:
                return PlainTextResponse("Forbidden", status_code=403)

        from reminders import generate_evening_briefing
        sent = await generate_evening_briefing(_ptb_app.bot)
        return JSONResponse({"sent": sent})

    async def cron_backup(request: Request):
        if CRON_SECRET:
            token = request.headers.get("X-Cron-Secret", "")
            if token != CRON_SECRET:
                return PlainTextResponse("Forbidden", status_code=403)

        from reminders import run_weekly_backup
        result = await run_weekly_backup(_ptb_app.bot)
        return JSONResponse({"result": result})

    async def cron_reminders(request: Request):
        if CRON_SECRET:
            token = request.headers.get("X-Cron-Secret", "")
            if token != CRON_SECRET:
                return PlainTextResponse("Forbidden", status_code=403)

        from reminders import check_and_fire_reminders
        fired = await check_and_fire_reminders(_ptb_app.bot)
        return JSONResponse({"fired": fired})

    async def health(request: Request):
        members = len(fsc._cache.get("members", {}))
        if members == 0 and not fsc._cache_ready.is_set():
            return JSONResponse(
                {"status": "starting", "cache_ready": False},
                status_code=503,
            )
        return JSONResponse({
            "status": "ok",
            "cache_ready": fsc._cache_ready.is_set(),
            "members": members,
            "uptime_s": int(time.time() - START_TIME),
        })

    return Starlette(
        routes=[
            Route("/webhook", webhook, methods=["POST"]),
            Route("/cron/morning_checkin", cron_morning_checkin, methods=["POST"]),
            Route("/cron/birthday_check", cron_birthday_check, methods=["POST"]),
            Route("/cron/evening_briefing", cron_evening_briefing, methods=["POST"]),
            Route("/cron/backup", cron_backup, methods=["POST"]),
            Route("/cron/reminders", cron_reminders, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
        ],
        lifespan=lifespan,
    )


starlette_app = _build_starlette_app()


# ── Polling Mode (Local Dev) ────────────────────────────

def main_polling():
    """Traditional polling mode for local development."""
    fsc.configure_logging()
    logger.info("Initializing Firestore cache...")
    fsc.init_cache()

    app = _build_ptb_app()
    logger.info("Stew starting — polling (chat_id=%s, data=Firestore)", CHAT_ID)
    app.run_polling(drop_pending_updates=True)


# ── Entry Point ──────────────────────────────────────────

if __name__ == "__main__":
    if "--polling" in sys.argv:
        main_polling()
    else:
        fsc.configure_logging()
        import uvicorn
        port = int(os.environ.get("PORT", "8080"))
        uvicorn.run(starlette_app, host="0.0.0.0", port=port)
