"""Stew Firestore Client

Firestore access + in-memory cache with real-time listeners.
Collections: members, interviews, notes, prayer_requests, follow_ups, reminders, meta.

Copied & adapted from Tanuki bot/firestore_client.py.
"""

# TODO: Copy from Tanuki bot/firestore_client.py and adapt
# - Keep: configure_logging(), get_db(), _log_change(), undo_last_change()
# - Keep: reminder functions, chat history functions, cache pattern (on_snapshot)
# - Replace: all domain functions (get_plan_summary, get_day, etc.)
# - New: add_member, save_interview, save_note, add_prayer_request, schedule_follow_up,
#        complete_prayer_request, complete_follow_up, snooze_follow_up, etc.

if __name__ == "__main__":
    print("Stew Firestore client — TODO: implement scaffold from Tanuki")
