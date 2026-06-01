"""Stew Bot — Reminder and nightly briefing logic.

Cloud Run architecture: no APScheduler. These functions are called by
HTTP endpoints triggered by Cloud Scheduler cron jobs:
- /cron/reminders  -> check_and_fire_reminders()  (every 15 min, Asia/Tokyo)
- /cron/briefing   -> generate_and_send_briefing()
    * `tanuki-nightly-briefing`: 6:00 PM daily, time zone `Asia/Tokyo`
    * `tanuki-pretrip-briefing`: 8:00 PM `America/Denver` — **pause while the
      family is in Japan**; it shares the same Firestore dedup as the Tokyo job
      and would otherwise send (and block the evening send) at the wrong JST time.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, date
from zoneinfo import ZoneInfo

import firestore_client as fsc

logger = logging.getLogger("tanuki.reminders")


async def check_and_fire_reminders(bot) -> int:
    """Check for due reminders and send them via Telegram.

    Returns the number of reminders fired.
    """
    due = fsc.get_due_reminders()
    if not due:
        return 0

    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    fired = 0

    for reminder in due:
        rid = reminder.get("_id", "?")
        message = reminder.get("message", "")
        target_chat = reminder.get("chat_id", chat_id)

        try:
            await bot.send_message(
                chat_id=target_chat,
                text=f"\u23f0 Reminder: {message}",
            )
            fsc.mark_reminder_fired(rid)
            fired += 1
            logger.info("Fired reminder %s: %s", rid, message[:50])
        except Exception as e:
            logger.error("Failed to send reminder %s: %s", rid, e)

    return fired


async def generate_and_send_briefing(bot) -> bool:
    """Generate and send the nightly briefing.

    Uses meta/last-briefing-date to prevent duplicate sends.
    Returns True if a briefing was sent.
    """
    from agent import generate_nightly_briefing, get_mode, is_during_trip

    if get_mode() == "post_trip":
        logger.info("Post-trip mode, skipping nightly briefing")
        return False

    tz = ZoneInfo("Asia/Tokyo") if is_during_trip() else ZoneInfo("America/Los_Angeles")
    today_str = datetime.now(tz).strftime("%Y-%m-%d")

    db = fsc.get_db()
    meta_ref = db.collection("meta").document("briefing-state")
    meta_doc = meta_ref.get()
    if meta_doc.exists:
        last_date = meta_doc.to_dict().get("last_briefing_date", "")
        if last_date == today_str:
            logger.info("Briefing already sent for %s, skipping", today_str)
            return False

    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    if chat_id == 0:
        logger.error("TELEGRAM_CHAT_ID not set, can't send briefing")
        return False

    try:
        briefing = await generate_nightly_briefing()
        if briefing:
            await bot.send_message(chat_id=chat_id, text=briefing)
            meta_ref.set({"last_briefing_date": today_str})
            logger.info("Sent nightly briefing (%d chars)", len(briefing))
            return True
    except Exception as e:
        logger.error("Nightly briefing failed: %s", e, exc_info=True)
        try:
            await bot.send_message(
                chat_id=chat_id,
                text="Something went wrong generating tonight's briefing. Error logged.",
            )
        except Exception:
            pass

    return False
