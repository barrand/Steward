# Stew — Elder's Quorum Assistant Bot

A personal pastoral assistant for an Elder's Quorum president. Stew helps you capture interview notes, track prayer requests, schedule follow-ups, and receive smart daily reminders via Telegram.

## Overview

- **Interview notes**: Capture work, family, health, and faith updates naturally via Telegram
- **Prayer reminders**: Two-touch cadence (30 days + 240 days from each interview) keeps long-running situations in mind
- **Follow-ups**: Schedule check-ins and see overdue items in the morning briefing
- **Calendar integration**: Get pre-meeting briefings the night before, and today's scheduled visits
- **Birthday reminders**: Never miss reaching out on a member's special day
- **Backup**: Weekly exports to Cloud Storage for safety

## Tech Stack

- **Bot**: Python, Telegram Bot API (python-telegram-bot)
- **API**: Cloud Run (Starlette + Uvicorn)
- **AI**: Google Gemini 2.5 Flash (function calling for natural language understanding)
- **Data**: Firestore (with real-time listeners + in-memory cache)
- **Scheduling**: Cloud Scheduler (cron jobs)
- **Calendar**: Google Calendar API (read-only)
- **Backup**: Google Cloud Storage

## Setup

### 1. Prerequisites

- Python 3.10+
- GCP project with Firestore, Cloud Run, Cloud Scheduler enabled
- Telegram bot token (from BotFather)
- Gemini API key
- Google Cloud service account (for Firestore + Calendar)

### 2. Install dependencies

```bash
cd bot
pip install -r requirements.txt
```

### 3. Configure environment

Copy `.env.example` → `.env` and fill in your secrets:

```bash
cp .env.example .env
# Edit .env with your actual tokens and IDs
```

### 4. Bootstrap member data (first time only)

```bash
# CSV format: Name, Age, Birth Date, Phone Number
# Example: "Barrand, Samuel Allen", 21, "24 Aug 2004", (801) 755-2681
python bot/firestore_client.py import-csv Elders.csv
```

### 5. Run locally (polling mode)

```bash
python bot/main.py
```

Bot connects to Telegram polling and Firestore. Send a message to test.

### 6. Deploy to Cloud Run

```bash
# Push docker image
gcloud builds submit --tag gcr.io/[PROJECT]/stew bot/

# Deploy service
gcloud run deploy stew \
  --image gcr.io/[PROJECT]/stew \
  --set-env-vars TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN,... \
  --region us-central1
```

### 7. Set up Cloud Scheduler jobs

```bash
# Morning checkin (7 AM MT)
gcloud scheduler jobs create http morning-checkin \
  --schedule="0 13 * * *" --uri="https://[SERVICE_URL]/cron/morning_checkin" \
  --http-method=POST --headers="Authorization: Bearer $CRON_SECRET" \
  --location=us-central1

# Birthday check (8 AM MT)
gcloud scheduler jobs create http birthday-check \
  --schedule="0 14 * * *" --uri="https://[SERVICE_URL]/cron/birthday_check" \
  --http-method=POST --headers="Authorization: Bearer $CRON_SECRET" \
  --location=us-central1

# Evening briefing (5:30 PM MT)
gcloud scheduler jobs create http evening-briefing \
  --schedule="30 23 * * *" --uri="https://[SERVICE_URL]/cron/evening_briefing" \
  --http-method=POST --headers="Authorization: Bearer $CRON_SECRET" \
  --location=us-central1

# Weekly backup (Sunday 3 AM MT)
gcloud scheduler jobs create http weekly-backup \
  --schedule="0 9 * * 0" --uri="https://[SERVICE_URL]/cron/backup" \
  --http-method=POST --headers="Authorization: Bearer $CRON_SECRET" \
  --location=us-central1

# Reminders (every 15 min)
gcloud scheduler jobs create http reminders \
  --schedule="*/15 * * * *" --uri="https://[SERVICE_URL]/cron/reminders" \
  --http-method=POST --headers="Authorization: Bearer $CRON_SECRET" \
  --location=us-central1
```

### 8. Set Telegram webhook

```bash
curl -X POST https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://[SERVICE_URL]/webhook"}'
```

## File Structure

```
EldersQuorum/
├── README.md                    # This file
├── .gitignore                   # Git ignore (secrets, member data)
├── .env.example                 # Environment variable template
├── bot/
│   ├── main.py                 # Telegram + Starlette entry point
│   ├── agent.py                # Gemini function-calling loop
│   ├── firestore_client.py     # Firestore + in-memory cache
│   ├── tools.py                # Tool implementations (PendingWrite pattern)
│   ├── reminders.py            # Cron job handlers
│   ├── google_calendar.py      # Google Calendar integration
│   ├── csv_import.py           # CSV bootstrap importer
│   ├── requirements.txt         # Python dependencies
│   ├── Dockerfile              # Cloud Run deployment
│   └── .gitignore              # (optional) local overrides
└── docs/
    └── PLAN.md                 # Deep dive architecture plan
```

## Key Concepts

### Interview Notes vs Casual Notes

- **Interview** (`interviews/`): Annual 1-on-1s with structured categories (work, family, health, faith, prayer requests)
- **Note** (`notes/`): Quick observations from church, phone calls, texts (single topic, informal)

### Prayer Reminder Schedule

Each prayer request is surfaced **exactly twice**:

1. **First reminder**: ~30 days after the interview
2. **Second reminder**: ~240 days (8 months) total from creation
3. **After second**: Only resurfaces in next year's interview

This keeps long-running situations (health recoveries, job searches) in mind without overwhelming.

### Two-Touch Follow-up System

Follow-ups stay in the morning briefing until you mark them done or snooze them. Overdue items are flagged with "🚨 OVERDUE 3 days".

### Calendar Matching

The bot matches member names from Firestore against your Google Calendar events using full-name word-boundary matching. Recommended event naming:

- `EQ: John Smith` (highest confidence)
- `Visit: John Smith` (high confidence)
- `Meeting with John Smith` (fuzzy match, flagged as lower confidence)

## Commands

| Command | Purpose |
|---------|---------|
| `/status` | Quick stats (members, pending follow-ups, prayer requests, reminders) |
| `/upcoming` | Upcoming birthdays (7 days) |
| `/member <name>` | View member profile (latest interview, notes, prayers) |
| `/help` | Show available commands |

## Free-Form Interactions

Most interactions are **natural language**, not rigid commands. Examples:

```
"Just met with John Smith. Works at the bank, stressed about layoffs..."
→ Stew extracts and confirms the notes

"His surgery went great!"
→ Stew recognizes context from prior conversation and offers to mark the prayer as answered

"Remind me to follow up with David in 3 weeks"
→ Stew schedules a follow-up with that due date
```

## Privacy & Security

- **Firestore security rules**: Locked to service account only (no public reads)
- **Secrets**: All stored in `.env` (gitignored), never in code
- **Member data**: Real PII (birthdays, phone numbers) never committed to git
- **Backups**: Weekly exports to Cloud Storage with 12-week retention
- **Compliance**: No logging of message content; only action types logged

## Development

### Local testing

Run in polling mode (no webhook):

```bash
python bot/main.py
```

Send messages to the bot via Telegram. Firestore listeners work in real-time.

### CLI mode

Import members or inspect cache:

```bash
python bot/firestore_client.py import-csv Elders.csv
python bot/firestore_client.py list-members
python bot/firestore_client.py undo-last
```

### Debugging

Check Cloud Run logs:

```bash
gcloud run logs read stew --limit=50
```

Check Firestore:

```bash
gcloud firestore documents list \
  --collection-ids=members,interviews,prayer_requests
```

## Future Enhancements

- **Phase 2**: Web dashboard (PIN-protected, member list, interview history, prayer overview)
- **Phase 3**: Telegram CSV upload (send `.csv` to bot directly instead of CLI import)
- **Phase 4**: Chrome extension to scrape ward directory

## License

MIT

## Contact

Questions? Reach out to Bryce or open an issue on GitHub.
