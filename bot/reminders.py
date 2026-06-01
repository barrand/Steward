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
from datetime import datetime, timedelta
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

    Shows follow-ups due today, prayer reminders on schedule, and calendar matches.
    Returns True if a check-in was sent.
    """
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    today = datetime.now(MT).date()
    today_str = today.isoformat()

    # Dedup: skip if already sent today
    meta = fsc._cache.get("meta", {})
    reminder_state = meta.get("reminder-state", {})
    if reminder_state.get("last_morning_checkin_date") == today_str:
        return False

    message_parts = []

    # 1. Follow-ups due today or overdue
    follow_ups = fsc._cache.get("follow_ups", [])
    due_followups = [f for f in follow_ups if f.get("due_date", "").startswith(today_str) or f.get("due_date", "") < today_str]
    if due_followups:
        message_parts.append("🔔 *Follow-ups due:*")
        for f in due_followups:
            name = f.get("member_name", "?")
            topic = f.get("topic", "?")
            due = f.get("due_date", "?")
            if due < today_str:
                days_overdue = (today - datetime.fromisoformat(due).date()).days
                message_parts.append(f"  🚨 OVERDUE {days_overdue} days — Ask {name} about {topic}")
            else:
                message_parts.append(f"  • Ask {name} about {topic}")

    # 2. Prayer reminders on two-touch schedule
    prayers = fsc._cache.get("prayer_requests", [])
    due_prayers = [p for p in prayers if p.get("next_remind_date", "").split("T")[0] <= today_str and p.get("remind_count", 0) < 2]
    due_prayers = due_prayers[:3]  # Cap at 3 per day
    if due_prayers:
        message_parts.append("🙏 *Time to pray for:*")
        for p in due_prayers:
            name = p.get("member_name", "?")
            text = p.get("request_text", "?")
            remind = p.get("remind_count", 0)
            remind_text = "first reminder" if remind == 0 else "second reminder"
            message_parts.append(f"  • {name} — {text} ({remind_text})")

    # 3. Heartbeat if nothing else
    if not message_parts:
        await bot.send_message(
            chat_id=chat_id,
            text="Good morning Bryce! All caught up — nothing on the radar today. ✓",
        )
        # Update meta with today's date
        db = fsc.get_db()
        db.collection("meta").document("reminder-state").set({
            "last_morning_checkin_date": today_str,
        }, merge=True)
        return True

    # Send the message
    message = "Good morning Bryce!\n\n" + "\n\n".join(message_parts)
    await bot.send_message(
        chat_id=chat_id,
        text=message,
        parse_mode="Markdown",
    )

    # Update prayer reminder dates (advance to next touch)
    db = fsc.get_db()
    for p in due_prayers:
        pid = p.get("_id")
        remind_count = p.get("remind_count", 0)
        if remind_count == 0:
            # First touch → schedule second touch 240 days from creation
            next_date = (datetime.fromisoformat(p.get("date_created", datetime.now(MT).isoformat())) + timedelta(days=240)).isoformat()
        else:
            # Second touch → no more reminders
            next_date = None
        db.collection("prayer_requests").document(pid).update({
            "remind_count": remind_count + 1,
            "last_reminded": datetime.now(MT).isoformat(),
            "next_remind_date": next_date,
        })

    # Update meta with today's date
    db.collection("meta").document("reminder-state").set({
        "last_morning_checkin_date": today_str,
    }, merge=True)

    logger.info("Sent morning check-in")
    return True


async def check_and_send_birthdays(bot) -> int:
    """Check for birthdays today and send notifications.

    Returns the number of birthday messages sent.
    """
    chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
    today = datetime.now(MT)
    today_mmdd = today.strftime("%m-%d")

    members = fsc._cache.get("members", {})
    birthdays_today = [m for m in members.values() if m.get("birthday", "")[-5:] == today_mmdd]

    for member in birthdays_today:
        name = member.get("name", "?")
        bday = member.get("birthday", "")
        try:
            # Try to extract age if we have full birth year
            if bday and bday != "0000-" + today_mmdd:
                year = int(bday[:4])
                age = today.year - year
                age_text = f" He's turning {age}!"
            else:
                age_text = ""
        except (ValueError, IndexError):
            age_text = ""

        message = f"🎂 *{name}'s birthday today!*{age_text}\n"

        # Enrich with latest interview or note
        interviews = [i for i in fsc._cache.get("interviews", []) if i.get("member_id") == member.get("_id")]
        if interviews:
            latest = interviews[0]
            message += f"Last talked {_time_ago(latest.get('date', ''))} ago.\n"

        message += "Might be a great time to reach out! 😊"

        await bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")

    return len(birthdays_today)


async def generate_evening_briefing(bot) -> bool:
    """Generate evening briefing for tomorrow's interviews.

    Checks tomorrow's calendar for member visits and surfaces context.
    Returns True if a briefing was sent.
    """
    # TODO: Integrate Google Calendar when available
    # For now, skip
    return False


async def run_weekly_backup(bot) -> str:
    """Export all data as JSON snapshot.

    Returns a status string.
    """
    # TODO: Implement Cloud Storage upload
    # For now, just log
    logger.info("Weekly backup: would export to Cloud Storage")
    return "Backup not yet implemented"


def _time_ago(iso_date: str) -> str:
    """Convert ISO date to 'X days ago' string."""
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        delta = datetime.now(dt.tzinfo) - dt
        days = delta.days
        if days == 0:
            return "today"
        elif days == 1:
            return "yesterday"
        else:
            return f"{days} days"
    except (ValueError, AttributeError, TypeError):
        return "some time"
