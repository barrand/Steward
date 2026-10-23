# Stew — Email Triage, Alerts, and Birthdays

Updated October 21, 2026 after reviewing the existing repository.

## Direction and MVP boundary

Stew reads new work Outlook email, classifies it with TypeSafe AI's Jev, and sends
Telegram alerts. Favor catching important mail over minimizing alerts. Poll every
30 minutes from 8:00 a.m. through 11:00 p.m. America/Denver, including weekends,
without depending on a personal device being online.

**Birthday notifications must keep working.** Preserve the existing morning
check-in, member data, destination, and local-date behavior. Birthday delivery is
an MVP acceptance requirement alongside email triage.

Start with one work mailbox, one user, and one Telegram destination. Outlook is
read-only: no marking read, moving, archiving, deleting, or sending. Generated
summaries, Gmail, behavior learning, a dashboard, and conversational email commands
are post-MVP.

This is the active roadmap. `docs/archive/PLAN.md` is the historical quorum-assistant
design. This revision changes documentation only; email triage is not implemented.

## Repository assessment

These findings describe source, not verified live GCP resources.

| Existing file | Actual behavior | MVP decision |
| --- | --- | --- |
| `bot/main.py` | Starlette/Uvicorn, Telegram webhook/local polling, morning/backup cron routes, cache-based health. Startup loads member caches and Gemini. | Keep as the existing bot and birthday entry point. Add a separate email entry point. |
| `bot/reminders.py` | Morning check-in includes birthdays, follow-ups, and prayers, with a persisted last-check-in date. Weekly backup is a stub. | Preserve the morning job; unrelated reminder/backup work is not an email prerequisite. |
| `bot/firestore_client.py` | Firestore connection, member/history collections, snapshot caches, saved chat history; undo is a stub. | Initially reuse only the connection. Add separate email collections with direct transactional operations. |
| `bot/agent.py` | Gemini conversation/tool loop, member context, persisted history, tool-argument logging. | Keep for existing interactions. Do not route email through this agent. |
| `bot/tools.py` | Member lookups and pastoral/reminder writes. | Keep outside the email execution path. |
| `bot/csv_import.py` | Working standalone member importer; the importer command in `firestore_client.py` is a stub. | Preserve member tooling and data. |
| `bot/google_calendar.py` | Placeholder. | Outside MVP. |
| `bot/Dockerfile` | Python 3.11, built from `bot/`, launches `main.py`. | Reuse layout; explicitly override the email service command. |
| `cloudbuild.yaml` | Builds/deploys `stew-bot` in `us-central1`. | Keep its target; add a separate email deployment definition. |
| `bot/requirements.txt` | Telegram, Gemini, Firestore, Starlette, Google API dependencies. | Keep birthday dependencies; add and pin email dependencies during implementation. |

Missing: Graph access, durable OAuth cache, Jev integration, email checkpoints,
alert retries, email monitoring, and tests. There is no `.env.example` despite the
old README referring to one. In-memory Telegram update deduplication does not
deduplicate email alerts across instances. Existing cron authentication is optional
when its secret is unset; do not copy that behavior into the email service.

## Architecture and proposed files

Deploy two services from the existing flat `bot/` source tree:

```text
Existing morning Scheduler -> stew-bot /cron/morning_checkin
                              -> birthdays + existing check-in -> Telegram

Email Scheduler -> private stew-email /cron/email_triage
                   -> Graph -> durable work records -> Jev -> policy
                   -> pending alert -> Telegram
```

Keep the existing bot, webhook, and birthday schedule. Email only sends outbound
Telegram messages; it needs no webhook or polling consumer. Both services can use
the same bot/destination without registering another webhook. Do not migrate or
delete member collections or import pastoral context into email triage.

Use the existing GCP project if appropriate, with a dedicated email runtime identity
and `email_*` collections. Verify actual deployment names, jobs, IAM, secrets, and
birthday delivery before deployment. If birthdays are not yet deployed, making the
existing path operational is part of MVP setup.

These additions are planned, not present yet:

| Proposed file | Responsibility |
| --- | --- |
| `bot/email_main.py` | Email-only Starlette startup, cron route, health/status, client lifecycle. No member caches, chat history, or Gemini startup. |
| `bot/email_config.py` | Validated settings, schedule rules, policy/context versions. |
| `bot/outlook.py` | Read-only Graph, folder paging, immutable IDs, normalized bounded content. |
| `bot/outlook_auth.py` | MSAL enrollment, silent refresh, durable Secret Manager token cache. |
| `bot/email_store.py` | Direct Firestore checkpoints, work states, transactions, leases, retention. Initially reuse `firestore_client.get_db()`. |
| `bot/email_classifier.py` | Stateless TypeSafe integration and normalized typed answers. |
| `bot/email_policy.py` | Pure decision logic and reason codes. |
| `bot/email_alerts.py` | Plain-text rendering, delivery claims, retry recording. |
| `bot/email_triage.py` | Bounded discovery/processing/delivery orchestration. |
| `bot/.env.example`, `bot/user_context.example.json` | Setting names and synthetic context; real secrets/context stay out of Git. |
| `tests/` | Synthetic policy, ingestion, recovery, and birthday regression tests. |
| `cloudbuild.email.yaml` | Explicit `stew-email` target and `python email_main.py` command; existing build remains for `stew-bot`. |
| `docs/EMAIL_RUNBOOK.md` | Enrollment, scheduler/IAM, monitoring, recovery, rollback. |

## Outlook authentication and discovery

First prove single-tenant delegated Graph `Mail.Read` with offline token refresh,
subject to work tenant policy. A fresh cloud instance must refresh/read after the
enrollment device goes offline. `Mail.ReadBasic` cannot supply bodies. No
`Mail.ReadWrite` or `Mail.Send`. Resolve tenant restrictions with the administrator
before selecting an alternative authorization flow.

Persist MSAL's serialized token cache after changes as versions of one dedicated
Secret Manager secret. Read its current version per run and serialize updates
under the mailbox lease. Enrollment uses a tenant-permitted interactive flow;
scheduled runs use silent refresh. Revoked consent/interaction-required responses
mean visible degradation and re-enrollment. See Microsoft's
[token acquisition](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens)
and [cache serialization](https://learn.microsoft.com/en-us/python/api/msal/msal.token_cache?view=msal-py-latest) guidance.

Begin with Inbox plus explicitly configured folders used by work mail rules. Both
read and unread messages qualify. Mail routed to unmonitored folders or removed
before fetching is a documented coverage limitation. Attachments, remote content,
shared mailboxes, and full conversation retrieval wait.

Use one Graph delta cursor per folder. Follow every `@odata.nextLink` and retain
the completed `@odata.deltaLink`. Discover minimal fields, then fetch content for
queued IDs. See [Graph delta guidance](https://learn.microsoft.com/en-us/graph/delta-query-messages).
Request `Prefer: IdType="ImmutableId"` on relevant Graph requests; deduplicate by
mailbox and case-sensitive immutable ID across folders. These IDs survive moves
within a mailbox; archive-mailbox moves are a separate limitation. See
[immutable identifiers](https://learn.microsoft.com/en-us/graph/outlook-immutable-id).

Persist a UTC `monitor_from` cutoff before initial scanning, defaulting to enrollment
time. Enumerating older IDs must not cause historical alerts. An explicit earlier
cutoff enables intentional backfill; arrivals during initialization stay eligible.
Durably queue every page's IDs before saving its continuation/cursor. A crash may
replay a page but cannot skip its messages. Save progress at the run budget. Cursor
expiry triggers resynchronization against retained coverage and dedup records,
never a silent reset to now.

## Jev integration and initial policy

Jev is TypeSafe AI's structured decision model, described in the
[introduction supplied for this project](https://typesafe.ai/blog/introducing-system-one-models-and-jev).
Use `typesafe-sdk`, `AsyncTypeSafeClient`, and `TYPESAFE_API_KEY`. One request sends
one message's state and several narrow questions. Retain returned model/token usage.
See the [Python SDK](https://docs.typesafe.ai/sdk/python) and
[System One API](https://docs.typesafe.ai/api/system-one).

Initially evaluate pinned `jev-1.13.0`, currently documented as the stable alias
target, after confirming account access. Evaluate `jev-1.14.0` separately before
switching. Do not silently follow `jev-latest`; retain requested and actual IDs.
See [TypeSafe's model reference](https://docs.typesafe.ai/models).

State contains sender, recipients, subject, received/current time, age computed in
code, available conversation ID, normalized bounded body, and versioned role/
responsibilities/important-person context. Start with a configurable 12,000-character
body cap and truncation flag. No member data or saved conversation history. Email
is untrusted data; embedded instructions cannot change policy or destination.

| Question | Primitive | Meaning |
| --- | --- | --- |
| `requires_action` | Noul | This user is asked to do something. |
| `direct_request` | Noul | The request targets this user, rather than only a broad audience. |
| `high_consequence` | Noul | Delay could materially affect the user's responsibilities. |
| `has_deadline` | Noul | An obligation has an explicit or strongly implied deadline. |
| `should_notify` | Noul | Interruption is warranted under the supplied attention criteria. |
| `urgency` | Choice | `can_wait`, `today`, `immediate`, `unclear`, with concrete criteria. |

Noul returns a probability; Choice includes a selection, distribution, and
confidence. Noul has no separate confidence field. Validate answer names/types,
finite ranges, known options, and model ID. See [Noul](https://docs.typesafe.ai/primitives/noul),
[Choice](https://docs.typesafe.ai/primitives/choice), and
[confidence](https://docs.typesafe.ai/confidence). Keep arithmetic/comparisons in
code. Typed output does not establish semantic correctness; test incomplete and
adversarial mail using TypeSafe's [Jev 1.13 limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13).

Proposed starting policy, to evaluate before treating it as reliable:

- Alert for a configured important sender, matched by address rather than display name.
- Alert if action/direct-request probability is at least 0.40, consequence at least
  0.35, deadline at least 0.40, or notification probability at least 0.35.
- Alert if combined `today`/`immediate` probability is at least 0.35.
- Alert as `uncertain` for unclear urgency, urgency confidence below 0.50, or
  truncated/missing content that cannot support confident suppression.
- Otherwise record `no_alert`. Invalid/missing answers are failures, not negative decisions.

Code assigns reason codes: `manager_or_key_person`, `direct_request`,
`high_consequence`, `deadline`, `action_required`, `urgent`, `uncertain`. Use fixed
display precedence and retain all matched codes. Version questions, thresholds,
and context independently. No generated explanation or deadline extraction in MVP.

## Durable processing, delivery, and privacy

| Firestore collection | Stored state |
| --- | --- |
| `email_accounts/{key}` | Opaque account key, cutoff, folder cursors/continuations, expiring lease owner/generation, health timestamps. |
| `email_messages/{key}` | Account/provider ID, timestamps, stage, typed answers, model/question/context/policy versions, reason codes, attempts, next retry, safe error code, retention time. |
| `email_alerts/{key}` | Message reference, pending/sending/sent/retry/delivery_unknown, attempt lease, send timestamps, confirmed Telegram message ID. |
| `email_reviews/{key}` | Message reference, manual label/correction, policy version, timestamp; no copied email text. |

Hash mailbox plus case-sensitive provider ID for document keys; retain the provider
ID for refetching. No sender, subject, body, rendered alert, free-form model prose,
or raw provider response is persisted. Refetch sender/subject for retried delivery.

1. Acquire/renew a transactional mailbox lease with ownership/generation checks;
   stale workers cannot commit after takeover.
2. Queue discovery IDs page by page. Drain prior retryable work independently so
   one bad message does not block later mail.
3. Fetch content in memory, call Jev, and atomically save decision plus pending
   alert or `no_alert` state.
4. Claim/send alerts as plain text: sender, bounded subject, received time, reason,
   stable short reference. Disable link previews. Example:
   `From: Alex | Subject: Approval needed | Reason: direct_request`.
5. Record confirmed Telegram message ID and release owned leases.

Use bounded retries/timeouts and provider retry headers. After three classifier
failures send `triage_unavailable` and raise degradation; never silently suppress.
Unreadable/deleted mail becomes `review_needed` with a reference-only unavailable
alert. Permanent auth failures require visible recovery.

Confirmed sends do not repeat on ordinary retries. **Exactly-once Telegram delivery
is not guaranteed:** `sendMessage` has no client idempotency-key parameter. Timeout
or a crash after acceptance can leave an ambiguous outcome. Mark `delivery_unknown`
(including expired sending leases), then retry with the same reference and a
possible-duplicate marker, favoring delivery. This is the application's tradeoff
based on the [Telegram interface](https://core.telegram.org/bots/api#sendmessage).

Telegram retains sender/subject in alerts; transient-content handling applies to
Stew's storage, not Telegram. Confirm employer permission for Graph, TypeSafe body
processing, and Telegram disclosure before real work mail. Check provider retention
settings separately. Logs/traces contain opaque IDs, stages, timing, counts, safe
error codes; redact tokens, bot URLs, OAuth details, and HTTP/SDK request bodies.

Propose configurable 90-day retention for terminal records; never expire unfinished
work. Resynchronization uses the later of enrollment cutoff and retained coverage
start, rejects older candidates, and deduplicates inside the retained window. An
outage beyond that window raises a coverage-gap incident for explicit backfill review.

## Scheduling and operations

Preserve the birthday job. Source implements birthdays in the morning check-in,
documented at 09:00 America/Denver; inspect the deployed schedule first. Do not add
a second birthday sender. If changing that path, test concurrent/retried calls and
use a durable daily claim: the current read-then-write date check is not a lock.

Make `stew-email` private, using Scheduler OIDC and a dedicated `roles/run.invoker`
identity. Preserve the other service's Telegram webhook access. See
[Scheduler authentication](https://docs.cloud.google.com/scheduler/docs/http-target-auth).
Use `*/30 8-23 * * *` in America/Denver, skipping the 23:30 slot in code. Admit
08:00–23:00 scheduled slots and bounded retries using authenticated scheduler
metadata; reject stale retries beyond the run deadline. A 23:00 run may finish
within its timeout. Out-of-hours skips never advance cursors. Overnight mail and
mail after the last scan remain eligible at 08:00. Use `zoneinfo` and UTC storage;
see [timezone behavior](https://docs.cloud.google.com/scheduler/docs/configuring/cron-job-schedules).

Start with a four-minute work budget, five-minute request timeout, one maximum
email instance, concurrency one, and minimum instances zero. Keep leases despite
these limits. Do work within requests, not a background polling loop.

Configuration includes tenant/client/account, folders, token-cache secret, Telegram
destination, TypeSafe key, pinned model, versions/context, hours/timezone, content
limit, retries/timeouts, retention. Fail startup for missing credentials/destination.
Use ADC/IAM in GCP and document local ADC support when adapting the connection
helper. Keep existing Gemini configuration for the birthday service.

`GET /health` reports process/config readiness. Protected `/status` reports last
successful poll/Graph read/Jev call/Telegram delivery, queue counts, oldest work.
Empty polls update health; quiet periods do not require classification or sending.
Monitor missed polls, auth failures, exhausted retries, and stuck work. Alert after
two expected slots are missed; suspend staleness checks overnight and allow the
first morning run its processing budget. Use an independent operations channel so
Telegram failures remain visible. No routine healthy/empty-inbox notifications.
Track usage and set budgets rather than adopting historical README cost estimates.

## Implementation sequence and completion gates

All implementation is pending. Complete in order:

### 0. Baseline, birthdays, and access

- Inventory actual services/jobs/secrets and confirm birthday delivery, timezone,
  destination, member access. Restore the existing birthday path if not operational.
- Confirm tenant/employer permission, TypeSafe access, folders, context, cutoff;
  prove Graph refresh/read plus synthetic Jev and Telegram calls.
- **Done:** unattended access works and the birthday baseline is recorded.

### 1. Email service and deterministic delivery

- Add separate startup/config/example, state store, Telegram sender, durable claims.
- **Done:** a synthetic event alerts once across retries/restarts; existing birthday
  startup is unaffected. This is the first implementation milestone after setup.

### 2. Outlook discovery and recovery

- Add MSAL enrollment/cache, delta paging, immutable IDs, cutoff, leases, retries,
  cursor reset. Add isolated dry-run state with no sends/production cursor changes.
- **Done:** new/read/overnight mail is discovered, crashes cannot lose pages, and
  one unavailable message does not drop the rest.

### 3. Jev and policy

- Add typed questions, versioned policy/context, content bounds, fallback alerts,
  synthetic/authorized local examples with held-out evaluation. No real mail in Git.
- **Done:** authorized Graph -> Jev -> policy -> Telegram works with durable
  decisions and no retained email text.

### 4. Reliability and birthday regression checks

- Test paging/replay, leases/takeover, atomic decision/outbox writes, cursor loss,
  and restart at delivery boundaries.
- Test Graph 401/403/429/5xx, revoked credentials, malformed mail, Jev invalid
  answers/timeouts, Telegram rejection/ambiguity, missing-message fallback.
- Test DST/hours/overnight catch-up, initial cutoff/backfill, retention coverage,
  and log/storage redaction.
- Test birthdays around UTC midnight using Denver dates, known/unknown birth year,
  no birthdays, repeated morning calls, mixed birthday/prayer/follow-up messages.
  Preserve/document current leap-day behavior.
- **Done:** recovery passes and birthdays match the baseline.

### 5. Deployment and trial

- Add separate email build, IAM/secrets, OIDC job, monitoring, budgets, runbook.
  Verify image command/target; smoke-test both email and birthdays after deployment.
- Roll back email by pausing its job/reverting `stew-email`; keep `stew-bot` running.
- Observe at least seven calendar days, devices offline between setup/review.
  Use a birthday fixture/test invocation if no natural birthday occurs.
- **Done:** expected polls accounted for, loss/ordinary duplicates resolved,
  operational failures visible, birthdays reaching their original destination.

## Evaluation and acceptance

Weekly, review all alerts, random `no_alert` records, and user-discovered misses by
opening originals in Outlook. Save labels by ID. Distinguish classification misses,
folder-coverage gaps, and delivery failures. Track reviewed-important recall,
unnecessary-alert rate, confirmed duplicates, ambiguous retries, expected-slot
success, backlog age, confirmed delivery, and TypeSafe usage. Report sample sizes;
sampled non-alerts do not establish inbox-wide recall. Versioning supports comparison;
exact replay still requires the original mail to remain available.

Proposed trial gate: at least 50 reviewed synthetic/authorized examples including
15 important cases, all designated must-catch cases alerting, and at least 90%
recall on reviewed important cases. These are trial criteria, not an accuracy claim.
Review volume before lowering sensitivity. Unexplained loss, silent polling failure,
ordinary duplicate sends, or broken birthdays blocks acceptance regardless of scores.

## Inputs needed before live implementation

- Work tenant/app registration and permitted unattended authorization flow.
- Employer permission for TypeSafe processing and Telegram sender/subject disclosure.
- GCP project/region and current birthday/bot deployment state.
- TypeSafe key and access to the pinned model.
- Role/responsibilities, important people, folder rules, cutoff/backfill preference,
  independent operations alert destination.

Secrets belong in ignored local configuration or Secret Manager, not the plan.

## After MVP

- Tune questions/thresholds from review data.
- Optional standalone birthday endpoint if separating the check-in helps; preserve
  daily deduplication and retire the old sender together.
- Generated summaries using a generative model, Gmail, more mailboxes/folders,
  conversation context, review tools, Telegram “why?”/“snooze” commands.
- Historical behavior learning and mailbox changes under separate policies.
- Broader quorum/calendar/dashboard/backup work as separately prioritized work.
