"""Stew Cron Job Handlers

Implements all scheduled tasks:
- morning_checkin: Prayer reminders + follow-ups (7 AM MT)
- birthday_check: Birthday alerts (8 AM MT)
- evening_briefing: Tomorrow's interview prep (5:30 PM MT)
- weekly_backup: JSON snapshot export (Sunday 3 AM MT)
- check_and_fire_reminders: Generic reminder dispatch (every 15 min)

Adapted from Tanuki bot/reminders.py.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import firestore_client as fsc

logger = logging.getLogger("stew.reminders")

MT = ZoneInfo("America/Denver")


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
                text=f"⏰ Reminder: {message}",
            )
            fsc.mark_reminder_fired(rid)
            fired += 1
            logger.info("Fired reminder %s: %s", rid, message[:50])
        except Exception as e:
            logger.error("Failed to send reminder %s: %s", rid, e)

    return fired


async def generate_morning_checkin(bot) -> bool:
    """Generate and send the morning check-in.

    Shows follow-ups, prayer reminders, and calendar for today.
    Uses meta/last-morning-checkin-date to prevent duplicate sends.
    Returns True if a check-in was sent.
    """
    today_str = datetime.now(MT).strftime("%Y-%m-%d")

    # TODO: Implement actual logic from plan
    # For now, just a stub that doesn't send anything
    return False


async def check_and_send_birthdays(bot) -> int:
    """Check for birthdays today and send notifications.

    Returns the number of birthday messages sent.
    """
    # TODO: Implement birthday detection
    return 0


async def generate_evening_briefing(bot) -> bool:
    """Generate and send the evening briefing (night before interview prep).

    Checks tomorrow's calendar for scheduled visits and surfaces member context.
    Returns True if a briefing was sent.
    """
    # TODO: Implement calendar matching and member briefing
    return False


async def run_weekly_backup(bot) -> str:
    """Export members and interview data to backup.

    Returns a status string.
    """
    # TODO: Implement backup to Cloud Storage
    return "Backup not yet implemented"
