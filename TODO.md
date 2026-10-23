# Stew — Active MVP Checklist

Source of truth: [STEW_PLAN.md](STEW_PLAN.md), updated October 21, 2026.
This revision is planning only; all implementation items remain pending.

## 0. Baseline, birthdays, and access

- [ ] Verify GCP services/jobs, Telegram destination, member data, and birthday delivery.
- [ ] Keep the existing birthday path operational; restore/deploy it if needed.
- [ ] Confirm work tenant permissions, folders, cutoff, and employer approval for TypeSafe/Telegram.
- [ ] Prove Graph read/refresh, TypeSafe access, and synthetic Telegram delivery.
- [ ] Define initial user context and independent operations alert destination.

## 1. Email service and delivery

- [ ] Add email-only startup/config/example within `bot/`; retain the birthday service.
- [ ] Add direct Firestore state/claims and deterministic plain-text Telegram alerts.
- [ ] Verify confirmed-send deduplication across retries/restarts using synthetic events.

## 2. Outlook ingestion

- [ ] Implement MSAL enrollment and durable Secret Manager token cache.
- [ ] Add folder delta paging, immutable IDs, cutoff, leases, retries, cursor recovery.
- [ ] Add isolated dry-run mode with no sends or production cursor/decision mutations.

## 3. Jev triage

- [ ] Add TypeSafe SDK, narrow typed questions, and pinned model version.
- [ ] Implement versioned policy/context, content bounds, and unavailable-triage fallback.
- [ ] Verify authorized Graph -> Jev -> Telegram without retaining email text.

## 4. Reliability and birthday checks

- [ ] Test paging/crashes, leases, atomic decisions/outbox, auth/provider failures.
- [ ] Test ambiguous Telegram delivery and reference-marked possible duplicates.
- [ ] Test hours/DST, overnight mail, retention coverage, and log/storage redaction.
- [ ] Verify birthday local dates, formatting, and repeated morning invocations.

## 5. Deployment and trial

- [ ] Add separate `stew-email` deployment, OIDC job, IAM/secrets, budgets, runbook.
- [ ] Configure missed-poll/backlog monitoring and independent failure notifications.
- [ ] Smoke-test email and birthdays; verify email-only rollback.
- [ ] Complete seven-day trial and labeled-example acceptance gates from the plan.

## Historical quorum backlog

The items below are retained from the earlier project direction. Existing birthday
delivery remains required above; broader quorum features are deferred.

### Features & Enhancements

- [ ] **Auto-fetch new members** — Script or endpoint to periodically check for new members added to Firestore and fetch their birthdays from external source (e.g., existing member database, manual sync, or birthday service API)
  - Use case: Keep member data current without manual CSV imports
  - Priority: Medium (can defer after initial deployment)

### Deployment & Infrastructure

- [ ] Deploy to Cloud Run (build + push container)
- [ ] Configure Cloud Scheduler (2 jobs: morning checkin at 9 AM Mountain Time, weekly backup)
- [ ] Set Telegram webhook
- [ ] Test end-to-end with Telegram

### Optional Enhancements

- [ ] Google Calendar integration — Enrich morning checkin with today's scheduled events
- [ ] Interview notes search — Full-text search across all interview history
- [ ] Bulk reminder scheduling — Batch reschedule multiple prayer reminders at once
