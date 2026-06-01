"""Stew Cron Job Handlers

Implements all scheduled tasks:
- morning_checkin: Prayer reminders + follow-ups (7 AM MT)
- birthday_check: Birthday alerts (8 AM MT)
- evening_briefing: Tomorrow's interview prep (5:30 PM MT)
- weekly_backup: JSON snapshot export (Sunday 3 AM MT)
- check_and_fire_reminders: Generic reminder dispatch (every 15 min)

Copied & adapted from Tanuki bot/reminders.py.
"""

# TODO: Copy from Tanuki bot/reminders.py
# - Keep: check_and_fire_reminders() (identical)
# - Replace: generate_and_send_briefing() → split into:
#     generate_morning_checkin() — prayer + follow-up + calendar today
#     check_and_send_birthdays() — birthday check
#     generate_evening_briefing() — tomorrow's calendar + member prep (5:30 PM)
#     run_weekly_backup() — export to GCS
#
# Key logic:
# - Morning: show overdue follow-ups (with 🚨 OVERDUE X days), due prayers (two-touch),
#   today's calendar matches, heartbeat if empty
# - Birthday: enrich with last interview date
# - Evening (5:30 PM): tomorrow's calendar matches with member context
# - Backup: weekly JSON snapshot to gs://stew-backups/YYYY-MM-DD.json

if __name__ == "__main__":
    print("Stew reminders — TODO: implement scaffold from Tanuki")
