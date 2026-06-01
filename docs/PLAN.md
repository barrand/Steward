# Elder's Quorum Bot ("Stew") — Deep Dive Plan

## What We're Building

A personal pastoral assistant for an Elder's Quorum president (Bryce, ~100 members).
After annual interviews you capture notes privately, then Stew surfaces the right
prayer requests, follow-ups, and birthdays at the right time via daily Telegram messages.
Private web dashboard lets you browse all member history.

---

## Tech Stack (same as Tanuki)

```
Telegram PTB  ←→  Cloud Run (Python/Starlette)  ←→  Firestore
                          ↕
                   Gemini 2.5 Flash (function calling)
                          ↕
                   Cloud Scheduler (cron triggers)
                          ↕
                   Firebase Hosting (static dashboard)
```

---

## What to Copy vs Rewrite (file by file)

### `main.py` — Copy, light edits
**Copy verbatim:**
- Dual-mode Starlette/polling architecture
- Webhook secret + cron secret validation
- `_processing_lock`, debouncing, dedup logic
- `handle_callback_query` (confirm/cancel/undo)
- `handle_message` routing logic
- `_do_process` (typing indicator, photo/PDF/text routing)
- `_build_ptb_app` scaffold
- Error handling (429, timeout, 503 strings)

**Change:**
- Rename Tanuki → Stew throughout
- Replace command set: `/interview`, `/pray`, `/followup`, `/member`, `/upcoming`, `/status`, `/help`
- Add cron routes: `/cron/morning_checkin`, `/cron/birthday_check` (keep `/cron/reminders`)
- Remove trip-specific fast-path commands (`/plan`, `/hotel`, `/spent`, `/booking`, `/todos`)
- Add CSV file handler (`.csv` mime type)

### `firestore_client.py` — Copy infrastructure, replace domain functions
**Copy verbatim:**
- `configure_logging()` — identical
- `get_db()` — identical (K_SERVICE detection, service-account-key.json fallback)
- `_log_change()` / `_get_previous_values()` — identical
- `undo_last_change()` — identical
- `add_reminder()` / `get_due_reminders()` / `_reminder_is_due()` / `mark_reminder_fired()` / `get_pending_reminder_count()` — identical
- `load_chat_history()` / `save_chat_history()` / `append_chat_message()` — identical
- `init_cache()` pattern (on_snapshot listeners, `_mark_loaded`, `_cache_ready` event) — copy pattern, replace per-collection handlers
- CLI `main()` pattern — adapt for new commands

**Replace entirely (new domain):**
- Cache dict (keys: `members`, `interviews`, `prayer_requests`, `follow_ups`, `meta`, `reminders`)
- All trip-specific functions (get_plan_summary, get_day, get_booking, add_activity, etc.)
- New domain functions: add_member, update_member, save_interview, add_prayer_request, complete_prayer_request, add_follow_up, complete_follow_up, get_members_summary, etc.

### `agent.py` — Copy loop, replace system prompt and tool declarations
**Copy verbatim:**
- `_generate()` with primary/fallback model logic
- `_log_usage()`, `_is_retryable_error()`
- Entire confirmation state machine: `set_pending_confirmation`, `get_pending_confirmation`, `handle_confirmation_callback`, `handle_confirmation_response`
- `history_as_gemini_contents()` / `append_to_history()`
- `process_message()` main loop (function calling rounds, `_dispatch_tool_call`, receipt builder)
- `_build_receipt()` and `_RECEIPT_VERBS`
- `strip_deep_command()` — keep `/deep` for complex queries
- Hallucination detection regex block

**Replace:**
- `build_system_prompt()` — entirely new for Stew
- `FUNCTION_TOOLS` declarations — new tools
- `_dispatch_tool_call()` — new tool routing
- Remove trip-specific helpers (`get_mode`, `is_during_trip`, `get_japan_date`, etc.)

### `tools.py` — Copy PendingWrite, replace implementations
**Copy verbatim:**
- `PendingWrite` class
- `execute_writes()`

**Replace entirely:**
- All `prepare_*` functions → new ones for member/interview/prayer/followup operations
- Fast-path helpers → `get_member_summary()`, `get_pending_follow_ups()`, `get_prayer_requests()`

### `reminders.py` — Copy check_and_fire, replace briefing
**Copy verbatim:**
- `check_and_fire_reminders()` — identical

**Replace:**
- `generate_and_send_briefing()` → split into two new functions:
  - `generate_morning_checkin()` — smart reminder selection (see logic below)
  - `check_and_send_birthdays()` — birthday detection + message

### `requirements.txt` — Same + Calendar
```
google-genai>=1.0.0
python-telegram-bot>=21.0
python-dotenv>=1.0.0
firebase-admin>=6.0.0
starlette>=0.37.0
uvicorn>=0.29.0
google-api-python-client>=2.0.0   # Google Calendar
google-auth-httplib2>=0.2.0
google-auth-oauthlib>=1.0.0
```

### `Dockerfile` — Copy unchanged

### `dashboard/index.html` — Reuse PIN gate + dark theme, replace content

---

## Firestore Data Model

**Decision: FLAT collections (not subcollections)**
Reason: Tanuki's on_snapshot listeners only work cleanly on top-level collections.
Subcollections would require knowing member IDs to set up listeners.
100 members × ~5 interviews = ~500 docs — tiny. Flat is fine.

```
members/{member_id}
  name: "John Smith"
  birthday: "1985-04-22"           # YYYY-MM-DD, no year = "0000-04-22"
  phone: "801-555-1234"            # optional
  email: "john@email.com"          # optional
  status: "active" | "inactive"
  last_interview_date: timestamp
  last_reminded: timestamp         # last time any reminder was sent about them
  created_at: timestamp

interviews/{interview_id}                 # formal annual interviews
  member_id: string
  member_name: string              # denormalized — avoids join for display
  date: timestamp
  work_notes: string
  family_notes: string
  health_notes: string
  faith_notes: string
  prayer_requests: [string]        # raw list extracted from interview
  raw_notes: string                # full transcript / whatever Bryce typed
  created_at: timestamp

notes/{note_id}                    # casual interactions (hallway chats, texts, calls)
  member_id: string
  member_name: string              # denormalized
  context: string                  # "saw at church", "phone call", "text", etc.
  text: string                     # what was discussed / observed
  prayer_requests: [string]        # any prayer requests that came out of it
  created_at: timestamp
  # Notes are lower-fidelity than interviews — used for "Bryce ran into John at the
  # store and he mentioned his daughter is moving" type updates. Surfaced in member
  # detail view alongside interviews, sorted by date.

prayer_requests/{request_id}
  member_id: string
  member_name: string              # denormalized
  request_text: string
  category: "health" | "work" | "family" | "faith" | "other"
  date_created: timestamp
  status: "pending" | "answered"
  answered_date: timestamp         # null until answered
  answered_note: string            # optional note when answered
  remind_count: integer            # 0=never reminded, 1=first done, 2=second done
  last_reminded: timestamp         # when last surfaced
  next_remind_date: timestamp      # pre-calculated, queried by morning checkin
  interview_id: string             # which interview this came from

follow_ups/{follow_up_id}
  member_id: string
  member_name: string              # denormalized
  topic: string
  date_created: timestamp
  due_date: timestamp              # when to remind Bryce
  completed_date: timestamp        # null until done
  notes: string                    # context for the follow-up
  status: "pending" | "done"
  source: "bot" | "manual"

reminders/{reminder_id}           # identical to Tanuki
  type: "morning_checkin" | "birthday" | "follow_up"
  member_id: string                # who this is about
  message: string
  fire_at: timestamp
  status: "pending" | "fired"
  chat_id: integer

meta/{key}
  "reminder-state": {last_morning_checkin_date, last_birthday_check_date}
  "chat-history": {messages: [...]}   # same as Tanuki
```

### Cache design (init_cache)
```python
_cache = {
    "members": {},           # {member_id: member_data} — all 100
    "interviews": [],        # sorted by date desc, last 200
    "notes": [],             # casual notes, sorted by date desc, last 200
    "prayer_requests": [],   # only status="pending", sorted by next_remind_date asc
    "follow_ups": [],        # only status="pending", sorted by due_date asc
    "reminders": [],         # same as Tanuki
    "meta": {},
}
```
**Key**: For prayer_requests, cache only `status="pending"` ones sorted by `next_remind_date`.
The morning checkin simply reads the top of this list to find what's due today.

---

## UX Deep Dive: Interaction Flows

### 1. Primary Data Entry — Free Form (not rigid commands)
The main improvement over the original plan: **Bryce types naturally; Gemini extracts structure**.
No multi-question wizard. The `/interview` command exists as a shortcut but free text is primary.

```
Bryce: "Just met with John Smith. Works at the bank, stressed about layoffs.
        His son Tyler does soccer and just made the A team.
        Knee surgery coming up next month, pray for that and for peace at work."

Stew: "Got it — here's what I captured for John Smith:
💼 Work: Stressed about potential layoffs at the bank
👨‍👩‍👧 Family: Son Tyler made the A team in soccer
🏥 Health: Knee surgery scheduled next month
🙏 Prayer requests:
   • Knee surgery recovery
   • Peace amid work stress

✅ Save  |  ❌ Cancel"
```

This is simpler for Bryce, more natural, and Gemini is very good at extraction.
The confirmation flow is already built in Tanuki — just reuse it with warmer language.

### 2. Pre-Meeting Briefing (calendar-aware)
This is the highest-value calendar integration. Rather than just "show appointments",
Stew proactively surfaces member notes BEFORE a meeting.

Morning checkin fires at 7 AM. If the calendar has an appointment that day containing
a quorum member's name, the message is enriched:

```
Stew: "Good morning Bryce!

📅 You have a visit with John Smith at 2:00 PM today.
Here's a quick refresher from your last interview (March 2025):
• Work: Stressed about layoffs at the bank
• Family: Son Tyler plays soccer
• Health: Knee surgery coming up
🙏 Praying for: Knee surgery recovery, peace at work

Also today: Tyler Park's birthday — he's turning 34!

Have a great day 🙏"
```

**Calendar integration scope**: Read-only. Query events for today → fuzzy match event
titles/descriptions against member names in the cache → if match found, include member
notes in the morning message.

### 3. Prayer Reminder Schedule — Two-Touch System

Each prayer request gets surfaced to Bryce exactly **twice** per interview cycle:

| Touch | When | Purpose |
|-------|------|---------|
| **First** | ~30 days after interview | "It's been a month — how is John doing? Still praying for him." |
| **Second** | ~7 months after the first | "It's been 8 months — still worth keeping in mind before you see him again." |
| After second | No more automatic reminders | If still pending, it'll come up in the next annual interview |

**How `next_remind_date` is set:**
```python
# When a prayer request is created (from interview):
next_remind_date = date_created + timedelta(days=30)
remind_count = 0

# When the first reminder fires:
next_remind_date = date_created + timedelta(days=240)  # ~8 months total
remind_count = 1
last_reminded = now

# When the second reminder fires:
next_remind_date = None  # no more automatic reminders
remind_count = 2
last_reminded = now
```

**Why this cadence works:**
- 30 days → fresh in memory, good time for a check-in call
- 7 months later → keeps long-running situations in mind (health recoveries, job searches)
- Annual interview resets the clock — new interview = new prayer requests = new schedule
- Answered prayers get marked off and never resurface

**Morning Check-In Priority (what gets shown today):**
1. **Follow-ups due today or overdue** — highest priority
2. **Prayer requests where `next_remind_date <= today`** — from the two-touch schedule
3. **Birthdays** — handled by separate cron, always fires

**Message template:**
```
Stew: "Good morning Bryce!

🔔 Follow-up: Ask David Park how his new job is going (due today).

🙏 Time to pray for:
• John Smith — knee surgery recovery
  (Added March 15 · first reminder)
• Maria Gonzalez — husband's job situation
  (Added April 1 · first reminder)"
```

### 4. Prayer Answered Closure (natural flow)

After Stew mentions a prayer request, Bryce can naturally close the loop:

```
[Stew mentions John's knee prayer]
Bryce: "His surgery went great! He's doing really well."

Stew: "That's wonderful news! 🙌
        Mark John's knee surgery prayer as answered?
        ✅ Mark Answered  |  ❌ Keep Active"

[Bryce taps ✅]
Stew: "✓ Marked as answered! Added a note about his recovery."
```

Gemini understands context — it knows from the prior conversation which prayer
was being discussed and calls `complete_prayer_request(request_id, note="Surgery went great, recovering well")`.

### 5. Scheduling a Follow-Up
Natural language (not command syntax):

```
Bryce: "Remind me to ask John about his knee surgery recovery in 3 weeks"
Stew: "Got it — I'll remind you on June 20 to follow up with John about his knee recovery.
        ✅ Schedule  |  ❌ Cancel"
```

The `/followup` slash command is a shortcut but not required.

### 6. Birthday Reminders
Separate from morning check-in (different cron job, 8 AM):

```
Stew: "Hey Bryce — it's John Smith's birthday today! 🎂
        He's turning 47 if I have his birth year.
        His last interview was 4 months ago — work stress and knee surgery were the main topics.

        Might be a great time to reach out and check in. 😊"
```

### 7. Night-Before Interview Briefing (NEW)
Evening cron at 5:30 PM (early enough Bryce can act tonight — call to confirm,
text to reschedule — without it being a last-minute scramble at 8 PM). Checks
tomorrow's calendar, matches any quorum members, and sends Bryce a briefing.

```
Stew: "Hey Bryce — you have a visit with John Smith tomorrow at 2:00 PM.

Here's a summary from your last interview (March 2025):
💼 Work: Stressed about potential layoffs at the bank
👨‍👩‍👧 Family: Son Tyler plays soccer, made the A team
🏥 Health: Knee surgery scheduled — should be recovered by now
🙏 Prayer requests:
   • Knee surgery recovery (added March 15 — first reminder was April 15)
   • Peace amid work stress

Good luck with the visit! 🙏"
```

If multiple quorum members are on tomorrow's calendar, each gets their own section.
If a calendar event has no matching member in Firestore, Stew skips it silently.

**Matching logic** (guarded against false positives):
- Match the member's **full name** (first + last) against the event title/description
  using word boundaries — not just first or last name alone (avoids "John" matching
  any John in the calendar).
- Recommended convention: prefix EQ-related calendar events with `EQ:` or `Visit:`
  (e.g., `EQ: John Smith`, `Visit: John Smith`). The matcher gives prefixed events
  the highest confidence; plain "John Smith" events match but are flagged as
  "lower confidence" in the briefing ("Looks like you may have a visit with…").
- If an event has a quorum member name AND a non-EQ keyword (`dentist`, `work`,
  `family`), still surface it but with a caveat — better a false positive Bryce
  can dismiss than missing a real visit.
- Multiple member-name matches in one event title → list all candidates and let
  Bryce mentally pick.

### 8. Casual Notes (not formal interviews)
Bryce doesn't only talk to members during scheduled interviews. He runs into them at
church, gets a text, hears something second-hand. These go into the `notes` collection,
not `interviews`.

```
Bryce: "Ran into Aaron Brackett at the store — said his mom is in the hospital."

Stew: "Got it — adding a quick note for Aaron Brackett:
📝 Context: Ran into at the store
📝 Note: Mom is in the hospital
🙏 Want to add a prayer request for his mom's health?
✅ Save  |  ❌ Cancel"
```

The agent decides: is this rich enough to be an `interview` (work/family/health/faith
coverage) or just a `note` (single-topic, casual)? Default to `note` unless Bryce
explicitly says "I interviewed" or covers multiple life areas in one message.

### 9. Name Disambiguation
When multiple members match a name, Stew asks before guessing. The CSV has both
"Brackett, Aaron" and "Brackett, Porter" — and "Burt, Jaxon", "Burt, Noah", "Burt, Ryan".
First-name-only references must be disambiguated.

```
Bryce: "Just talked to Sam — he's loving his new ward calling."

Stew: "Which Sam? I have two:
  1️⃣ Samuel Allen Barrand (21)
  2️⃣ Samuel Bona (19)"

Bryce: "2"

Stew: [proceeds with Samuel Bona]
```

Built via a `disambiguate_member(query)` tool that returns candidates when fuzzy
match yields >1 result. System prompt rule: **never guess between candidates —
always ask**.

### 10. CSV Import (Phase 1)
Source file: `/Users/bbarrand/Documents/Projects/EldersQuorum/Elders.csv`
Format observed:
```csv
Name,Age,Birth Date,Phone Number
"Barrand, Samuel Allen",21,24 Aug 2004,(801) 755-2681
"Bona, Samuel",19,8 May 2007,
```

CLI-only import for Phase 1 (no Telegram upload, no auto-create-on-mention, no JSON
seed file). One-shot bootstrap script:

```bash
python firestore_client.py import-csv /path/to/Elders.csv
```

Parsing rules:
- Split `"Last, First [Middle]"` → store as `name = "First [Middle] Last"`
- Parse `DD Mon YYYY` → `YYYY-MM-DD` for birthday
- Strip `(801) 755-2681` → `801-755-2681` for phone (keep optional)
- Skip rows with empty phone (still import; phone is optional)
- Detect `Out-of-Unit` suffix in names → set `status: "inactive"`, strip suffix
- Idempotent: if a member with the same name already exists, update fields instead
  of duplicating. Log every change to the changelog so it's undoable.
- Print summary: "Imported 87 members (3 inactive, 84 active). 0 duplicates."

---

## System Prompt Design (Stew)

```
You are Stew, a personal assistant for Bryce, who is the Elder's Quorum President
of his local LDS congregation. He uses you to keep track of his quorum members.

Bryce conducts annual interviews with his ~100 quorum members. During these interviews
he learns about their work situations, family, health, and faith. He asks what to pray
for. You help him remember and follow up on these things.

MEMBER DIRECTORY (for name resolution):
{compact_member_list}   ← injected from cache: "John Smith, David Park, ..."

PENDING FOLLOW-UPS:
{follow_up_list}        ← "Ask David Park about new job (due today)"

PENDING PRAYER REQUESTS (sample):
{prayer_summary}        ← "John Smith: knee surgery; Maria Gonzalez: husband's job"

UPCOMING BIRTHDAYS (7 days):
{birthday_list}         ← "Tyler Park — June 3"

CURRENT TIME: {now_str} (Mountain Time)

PERSONALITY: Warm, spiritually supportive, occasionally encouraging. Address Bryce
by name. Understands LDS context naturally. Keep responses concise — Bryce is busy.

CAPTURING INTERVIEW NOTES — your most important job:
When Bryce tells you about a meeting with a member, extract structured notes:
- Work situation and stress
- Family updates (spouse, kids, activities)
- Health topics
- Faith/testimony notes
- Prayer requests (be specific — "knee surgery" not "health")

Always show a confirmation summary before saving. Frame it warmly, not bureaucratically.
"Here's what I captured" not "PENDING WRITE — confirm to save".

TOOLS: [see declarations below]

RULES:
- ALWAYS resolve member names against the directory before calling tools
- When a name is ambiguous (multiple members match), call `disambiguate_member` and
  ask Bryce which one — NEVER guess between candidates
- Distinguish casual notes from formal interviews: use `save_note` when Bryce
  mentions a single chat/text/observation. Use `save_interview_notes` only when
  multiple life areas (work + family + health + faith) are covered or Bryce
  explicitly says "I interviewed [name]"
- For prayer requests, extract specific actionable items (not vague "family things")
- Never fabricate interview notes — only save what Bryce actually told you
- Mark follow-ups as done when Bryce says something like "I talked to him" or "he's good now"
- Suggest marking prayers as answered when Bryce mentions positive outcomes
```

---

## Tool Declarations (Gemini function calling)

### Read tools (instant, no confirmation):
```python
get_member(name_or_id)              → full member profile + latest interview summary
search_members(query)               → fuzzy search by name
disambiguate_member(query)          → returns candidate list when name is ambiguous
                                      (e.g., "Sam" → [Samuel Barrand, Samuel Bona])
get_member_interviews(member_id)    → list of all interviews with that member
get_member_notes(member_id)         → list of casual notes for member
get_pending_prayer_requests(member_id=None)  → all or per-member
get_pending_follow_ups()            → all pending follow-ups
get_upcoming_birthdays(days=7)      → members with birthdays in next N days
get_calendar_events(days=7)         → read-only calendar events
```

### Write tools (most auto-save, some need confirmation):
```python
# Auto-save (safe, additive)
save_interview_notes(member_id, work, family, health, faith, prayer_requests, raw_notes)
save_note(member_id, context, text, prayer_requests=[])   # casual interactions
add_prayer_request(member_id, text, category)
schedule_follow_up(member_id, topic, due_date, notes)

# Needs confirmation (modifying existing data)
complete_prayer_request(request_id, answered_note)   # mark prayer as answered
complete_follow_up(follow_up_id, notes)              # mark follow-up done
snooze_follow_up(follow_up_id, until_date)           # push due_date out
update_member(member_id, field, value)               # edit member profile
add_member(name, birthday, phone)                    # add a new member
```

**Rule**: Same as Tanuki — reads are instant, additive writes auto-save (with receipt),
destructive/modifying writes need confirmation via inline keyboard.

---

## Cron Jobs

### 1. `/cron/morning_checkin` — Daily 7:00 AM MT
```python
async def generate_morning_checkin(bot) -> bool:
    # Dedup: skip if already sent today (same pattern as Tanuki briefing)
    today_str = datetime.now(MT).strftime("%Y-%m-%d")
    if last_checkin_date == today_str: return False

    today = datetime.now(MT).date()
    message_parts = []

    # 1. Follow-ups due today or overdue. Keep showing daily until completed or
    #    snoozed — Bryce can tap "snooze" to push due_date out. Overdue items are
    #    annotated with how many days late so they're visually distinct.
    due_followups = [f for f in cache["follow_ups"]
                     if f.get("due_date") and f["due_date"].date() <= today]
    # Each followup formatted as:
    #   "🚨 OVERDUE 3 days — Ask David Park how his new job is going"
    #   "🔔 Today — Check in with Aaron about his mom"
    # Followups never auto-disappear; only `complete_follow_up` or `snooze_follow_up` removes them.
    if due_followups:
        message_parts.append(format_followup_section(due_followups, today))

    # 2. Prayer requests due per two-touch schedule
    #    Cache is sorted by next_remind_date asc — just take those <= today
    due_prayers = [p for p in cache["prayer_requests"]
                   if p.get("next_remind_date")
                   and p["next_remind_date"].date() <= today
                   and p.get("remind_count", 0) < 2]  # max 2 reminders per request
    prayers_to_show = due_prayers[:3]  # cap at 3 per day to avoid overwhelming

    if prayers_to_show:
        message_parts.append(format_prayer_section(prayers_to_show))

    # 3. Calendar events today → match member names → enriched note if visit found
    cal_events = get_calendar_events_today()
    member_visits = match_events_to_members(cal_events, cache["members"])
    if member_visits:
        message_parts.append(format_visit_section(member_visits))

    if not message_parts:
        # Nothing due today — send a brief heartbeat so Bryce knows Stew is alive
        # and watching. Skip on weekends to avoid noise (configurable).
        if datetime.now(MT).weekday() < 5:  # Mon-Fri only
            await bot.send_message(
                chat_id=CHAT_ID,
                text="Good morning Bryce! All caught up — nothing on the radar today. ✓"
            )
            update_meta("reminder-state", {"last_morning_checkin_date": today_str})
        return False

    message = "Good morning Bryce!\n\n" + "\n\n".join(message_parts)
    await bot.send_message(chat_id=CHAT_ID, text=message)

    # Advance next_remind_date on each prayer request that was shown
    for p in prayers_to_show:
        advance_prayer_reminder(p["_id"])  # sets next_remind_date per schedule above

    update_meta("reminder-state", {"last_morning_checkin_date": today_str})
    return True
```

### 2. `/cron/birthday_check` — Daily 8:00 AM MT
```python
async def check_and_send_birthdays(bot) -> int:
    today = datetime.now(MT)
    today_mmdd = today.strftime("%m-%d")

    birthdays_today = [m for m in cache["members"].values()
                       if m.get("birthday", "")[-5:] == today_mmdd]

    for member in birthdays_today:
        message = f"Hey Bryce — it's {member['name']}'s birthday today! 🎂\n"
        # Enrich with latest interview context if available
        last_interview = get_latest_interview(member["_id"])
        if last_interview:
            message += f"Last talked {time_since(last_interview['date'])} ago.\n"
        await bot.send_message(chat_id=CHAT_ID, text=message)

    return len(birthdays_today)
```

### 3. `/cron/evening_briefing` — Daily 5:30 PM MT
```python
async def generate_evening_briefing(bot) -> bool:
    # Dedup: skip if already sent tonight
    tonight_str = datetime.now(MT).strftime("%Y-%m-%d")
    if last_evening_briefing_date == tonight_str: return False

    tomorrow = (datetime.now(MT) + timedelta(days=1)).date()

    # Get tomorrow's calendar events
    cal_events = get_calendar_events_for_date(tomorrow)
    if not cal_events:
        return False  # nothing on calendar → no message

    # Match events to quorum members
    member_visits = match_events_to_members(cal_events, cache["members"])
    if not member_visits:
        return False  # calendar has events but none are quorum members

    # Build briefing for each matched member
    sections = []
    for member, event in member_visits:
        interview = get_latest_interview(member["_id"])
        prayers = get_pending_prayers_for_member(member["_id"])
        sections.append(format_member_briefing(member, event, interview, prayers))

    message = f"Hey Bryce — heads up for tomorrow:\n\n" + "\n\n".join(sections)
    await bot.send_message(chat_id=CHAT_ID, text=message)

    update_meta("reminder-state", {"last_evening_briefing_date": tonight_str})
    return True
```

### 4. `/cron/reminders` — Every 15 min (copied verbatim from Tanuki)

### 5. `/cron/backup` — Weekly (Sunday 3:00 AM MT)
Pastoral data is irreplaceable. A weekly export protects against accidental deletion,
schema mistakes, or a wrong `undo`. Writes a JSON snapshot to Cloud Storage.

```python
async def run_weekly_backup() -> str:
    snapshot = {
        "members": fsc.get_all_members(),
        "interviews": fsc.get_all_interviews(),
        "notes": fsc.get_all_notes(),
        "prayer_requests": fsc.get_all_prayer_requests(include_answered=True),
        "follow_ups": fsc.get_all_follow_ups(include_done=True),
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    # Upload to gs://stew-backups/YYYY-MM-DD.json
    blob_name = f"{datetime.now(MT).date()}.json"
    upload_to_gcs("stew-backups", blob_name, json.dumps(snapshot, default=str))
    # Retain last 12 weeks (lifecycle rule on the bucket handles deletion)
    return f"Backed up {sum(len(v) if isinstance(v, list) else 0 for v in snapshot.values())} records → {blob_name}"
```

Sends Bryce a Telegram confirmation: "📦 Weekly backup complete — 423 records archived."

---

## Web Dashboard _(Future Phase)_

PIN-protected single HTML file (same pattern as Tanuki). Client-side Firestore SDK.
Dark theme, mobile-friendly.

### Planned views:
1. **Today** — Morning check-in summary, upcoming birthdays, overdue follow-ups
2. **Members** — Searchable list, click → Member Detail
3. **Member Detail** — Interview history, prayer requests, follow-ups
4. **Prayer Requests** — All pending across all members, mark answered
5. **Import** — CSV upload instructions

---

## File Structure (New Project)

```
EldersQuorum/
└── bot/
    ├── main.py              # Telegram + Starlette (adapted from Tanuki)
    ├── agent.py             # Gemini loop (adapted from Tanuki)
    ├── firestore_client.py  # Firestore + cache (adapted from Tanuki)
    ├── tools.py             # PendingWrite + tool implementations (adapted)
    ├── reminders.py         # Cron job handlers (adapted from Tanuki)
    ├── google_calendar.py   # NEW — Google Calendar read-only
    ├── requirements.txt
    ├── Dockerfile           # Copied from Tanuki
    ├── .env.example
    └── .gitignore

    └── csv_import.py        # CSV bootstrap importer (Phase 1, CLI-only)

# Future phases:
# └── dashboard/index.html  — PIN-protected web dashboard
```

---

## Privacy Considerations

This data is sensitive (pastoral conversations, health info, faith struggles).
- Firestore security rules: locked to service account only (no public reads)
- Dashboard: PIN-protected, no public URL in code
- `.env` with bot token and service account: gitignored
- No logging of message content to Cloud Run logs (only log action types)
- Data never goes to Google for training (Gemini API policy)
- Consider Firestore encryption at rest (enabled by default on GCP)

---

## Environment Variables

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
GEMINI_API_KEY=
WEBHOOK_SECRET=          # random string for Telegram webhook auth
CRON_SECRET=             # random string for Cloud Scheduler auth
STEW_MODEL=gemini-2.5-flash
STEW_FALLBACK_MODEL=gemini-2.5-flash
GOOGLE_CALENDAR_ID=      # which calendar to read
GOOGLE_APPLICATION_CREDENTIALS=service-account-key.json
```

---

## Implementation Order

### Step 1 — Scaffold (copy from Tanuki)
- [ ] Copy `main.py`, `agent.py`, `firestore_client.py`, `tools.py`, `reminders.py` into new project
- [ ] Do global rename: Tanuki → Stew, tanuki → stew
- [ ] Strip all trip-specific content (tool declarations, system prompt, domain functions)
- [ ] Verify bot starts in polling mode with no errors

### Step 2 — Data Model + Firestore client
- [ ] Implement cache with new collections (members, interviews, notes, prayer_requests, follow_ups)
- [ ] Write all domain functions: add_member, save_interview, save_note, add_prayer_request, etc.
- [ ] Add changelog + undo support for all write operations
- [ ] Test with CLI mode (`python firestore_client.py add-member ...`)

### Step 2.5 — CSV Bootstrap Import
- [ ] Write `csv_import.py` to parse `/Users/bbarrand/Documents/Projects/EldersQuorum/Elders.csv`
- [ ] Parse `"Last, First [Middle]"` → `"First [Middle] Last"`, `DD Mon YYYY` → ISO date
- [ ] Detect `Out-of-Unit` → mark inactive, strip suffix
- [ ] Idempotent: update existing matches instead of duplicating
- [ ] Print summary and dry-run mode before committing

### Step 3 — Tools + Agent
- [ ] Write all tool declarations (Gemini function schemas)
- [ ] Implement `_dispatch_tool_call` for all new tools
- [ ] Write `build_system_prompt()` with dynamic member directory + prayer/followup context
- [ ] Test free-form interview capture end-to-end

### Step 4 — Cron Jobs
- [ ] `generate_morning_checkin()` with two-touch prayer priority logic
- [ ] `check_and_send_birthdays()`
- [ ] `generate_evening_briefing()` — night-before interview prep
- [ ] Wire up all Cloud Scheduler endpoints in `main.py`
- [ ] Test locally by calling the HTTP endpoint manually

### Step 5 — Commands + Fast-Path
- [ ] `/status`, `/help`, `/upcoming` commands
- [ ] `/member <name>` fast-path (show member summary without Gemini)

### Step 6 — Google Calendar
- [ ] `google_calendar.py` with service account auth
- [ ] `get_calendar_events(days=7)` function
- [ ] `match_events_to_members()` fuzzy name-matching logic
- [ ] Wire into morning checkin enrichment + evening briefing + `get_calendar_events` tool

### Step 7 — Deploy to GCP
- [ ] Create new GCP project or reuse Tanuki's
- [ ] Set up Cloud Run service
- [ ] Configure Cloud Scheduler jobs (morning checkin 7 AM MT, birthday check 8 AM MT, evening briefing 5:30 PM MT, reminders every 15 min, weekly backup Sunday 3 AM MT)
- [ ] Create GCS bucket `stew-backups` with 12-week lifecycle rule
- [ ] Set Telegram webhook
- [ ] Smoke test full flow

---

## Future Phases

### Phase 2 — Web Dashboard
- PIN-protected single HTML file (same pattern as Tanuki)
- Member list, member detail (interviews + prayer requests), prayer requests overview

### Phase 3 — Telegram CSV Upload + Chrome Extension
- Send `.csv` to Stew via Telegram → bulk load members + birthdays (Phase 1 is CLI-only)
- Preview + confirm before importing
- Chrome extension for scraping ward directory (longer term)

---

## Open Questions for Bryce

1. **Bot name**: "Stew" is the working name — any preferences? Other ideas?
2. **Reminder time**: 7 AM Mountain Time for morning check-in?
3. **GCP project**: New project or reuse the Tanuki one?
4. **Calendar**: Which Google Calendar account? Personal Gmail or a work account?
5. **Birthdays without year**: Some members you may only know the month/day. Store as `0000-MM-DD` — Stew will still remind, just won't know the age.
6. **Interview cadence reminder**: Should Stew remind you when a member hasn't been interviewed in 12+ months? ("Hey, it's been 14 months since you met with John Smith.")
