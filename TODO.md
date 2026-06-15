# Stew Bot — TODO

## Features & Enhancements

- [ ] **Auto-fetch new members** — Script or endpoint to periodically check for new members added to Firestore and fetch their birthdays from external source (e.g., existing member database, manual sync, or birthday service API)
  - Use case: Keep member data current without manual CSV imports
  - Priority: Medium (can defer after initial deployment)

## Deployment & Infrastructure

- [ ] Deploy to Cloud Run (build + push container)
- [ ] Configure Cloud Scheduler (2 jobs: morning checkin at 7 AM MT, weekly backup)
- [ ] Set Telegram webhook
- [ ] Test end-to-end with Telegram

## Optional Enhancements

- [ ] Google Calendar integration — Enrich morning checkin with today's scheduled events
- [ ] Interview notes search — Full-text search across all interview history
- [ ] Bulk reminder scheduling — Batch reschedule multiple prayer reminders at once
