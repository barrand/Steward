"""Stew — Elder's Quorum Assistant Bot

Telegram bot entry point. Dual-mode deployment:
- Local: polling mode (no webhook)
- Production: Cloud Run webhook + Starlette

Copied & adapted from Tanuki trip bot (JapanTrip/bot/main.py).
"""

# TODO: Copy from Tanuki bot/main.py and adapt
# - Rename Tanuki → Stew
# - Replace command set: /interview, /member, /status, /help, /upcoming
# - Add cron routes: /cron/morning_checkin, /cron/birthday_check, /cron/evening_briefing, /cron/backup
# - Remove trip-specific commands
# - Add CSV file handler

if __name__ == "__main__":
    print("Stew bot — TODO: implement main.py scaffold from Tanuki")
