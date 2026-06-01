"""Stew Google Calendar Integration

Read-only access to Google Calendar for event matching.
Used by morning_checkin (today's events) and evening_briefing (tomorrow's events).

Functions:
- get_calendar_events(days=7) — fetch events for next N days
- match_events_to_members() — fuzzy match event titles to member names (full-name, word boundary)
"""

# TODO: Implement Google Calendar service account auth
# - Service account credentials from GOOGLE_APPLICATION_CREDENTIALS env var
# - get_calendar_events(date) — query Google Calendar API
# - match_events_to_members(events, members_dict) — full-name word-boundary matching
#   Confidence levels:
#   - HIGH: "EQ: John Smith", "Visit: John Smith"
#   - MEDIUM: "John Smith" (plain, no prefix)
#   - LOW: "John" (first name only, ambiguous)
#   Return: [(member_id, member_name, event, confidence), ...]

if __name__ == "__main__":
    print("Stew Google Calendar — TODO: implement")
