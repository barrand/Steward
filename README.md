# Stew — A Personal Assistant for What Needs Your Attention

Stew is a personal Telegram assistant intended to help Bryce notice important
messages and remember to reach out to people. It began as an Elders Quorum
assistant for member information, interview notes, prayers, follow-ups, and
birthdays. Its next proposed milestone is an email assistant that watches work
Outlook mail and sends Telegram alerts when something deserves attention.

The project is transitioning from the original quorum bot toward email triage
while preserving existing birthday notifications. Email triage is planned and
has not been implemented yet.

## The email MVP

The current proposal is a focused first version for one user, one work Outlook
mailbox, and one Telegram destination:

- Check Inbox and selected folders every 30 minutes from 8 a.m. through 11 p.m.
  America/Denver, including weekends, without needing a personal device online.
- Use TypeSafe AI's Jev model and user-specific context to identify important
  senders, requests, deadlines, urgency, and uncertain messages worth reviewing.
- Send plain-text Telegram alerts with the sender, subject, received time, and
  reason for the alert.
- Keep durable processing and delivery records so restarts and provider failures
  can be retried, and make operational failures visible.
- Keep Outlook access read-only: no sending, marking read, moving, archiving, or
  deleting mail.

This first version would catch important incoming messages. Tracking unfinished
responsibilities, repeated follow-up reminders, generated summaries, Gmail,
dashboards, and conversational email commands are outside the proposed MVP.
The alert criteria and prerequisites still need validation before building it.

## What exists today

The `bot/` directory contains the original Python Telegram bot, with a
Gemini-powered conversation and tool loop, Firestore storage, member lookup and
CSV import, and a combined morning check-in for birthdays, follow-ups, and
prayer reminders. Birthday matching uses the America/Denver calendar date.

Calendar integration, weekly backup, and undo are placeholders. The archived code
review also identifies reliability gaps in the existing bot, including birthday
delivery. Source code and deployment configuration do not establish that a live
deployment is healthy; the current services and scheduled jobs need verification.

## How the planned system fits together

The email worker would run as a separate Cloud Run service alongside the existing
bot. Cloud Scheduler would trigger it to read Outlook through Microsoft Graph,
classify messages with Jev, record processing state in Firestore, and deliver
alerts through Telegram.

The existing bot would retain its birthday and member-data responsibilities.
Work email would use a separate processing path from the quorum conversation
history and pastoral context.

The plan calls for transient processing of email bodies, with no sender, subject,
or body retained in Stew's email records. Telegram alerts would contain sender and
subject information. These are planned data-handling boundaries, not guarantees
provided by the current bot.

## Before building and going live

- Confirm the permitted Microsoft tenant/app setup and prove unattended Outlook
  access and token refresh.
- Confirm employer permission for TypeSafe processing and Telegram disclosures
  before using real work mail.
- Verify the GCP project, existing bot deployment, birthday schedule, member
  access, and Telegram destination.
- Establish TypeSafe access, monitored folders, user responsibilities, important
  people, and an initial mail cutoff.
- Agree on must-catch examples, acceptable alert noise, a failure-notification
  destination, and spending limits.

The implementation roadmap includes synthetic integration checks, recovery tests,
birthday regression checks, and a seven-day live trial. Keep credentials and real
mail or member data out of Git.

## Repository guide

| Location | Purpose |
| --- | --- |
| [STEW_PLAN.md](STEW_PLAN.md) | Active roadmap: scope, architecture, prerequisites, milestones, and acceptance criteria. |
| [TODO.md](TODO.md) | Implementation checklist and deferred quorum backlog. |
| [bot/](bot/) | Existing Telegram bot source, dependencies, and container definition. |
| [cloudbuild.yaml](cloudbuild.yaml) | Existing build/deploy configuration targeting `stew-bot` in `us-central1`. |
| [docs/archive/PLAN.md](docs/archive/PLAN.md) | Historical quorum-assistant design. |
| [docs/archive/CODE_REVIEW.md](docs/archive/CODE_REVIEW.md) | Source review covering birthday reliability and email implementation gaps. |

Use **STEW_PLAN.md** as the source of truth for the proposed email MVP. The
archived documents provide historical context and review findings.
