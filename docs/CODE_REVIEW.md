# Stew code review — email triage and birthday reliability

Reviewed October 21, 2026. Scope: every current Python module, Dockerfile,
requirements, build configuration, and their fit to `STEW_PLAN.md`.

## Assessment

The Python/Starlette/Telegram/Firestore stack is suitable. The current application
needs reliability hardening before it becomes the foundation for unattended email
alerts. Birthday delivery itself has reproducible failure paths. The planned
separate email service remains appropriate, but preserving the old service alone
does not establish that birthdays are reliable.

This is a source review, with synthetic reproductions. It does not establish the
state of deployed GCP resources, actual credentials, IAM, monitoring, or member data.
No runtime source or deployment settings were changed during review.

## Findings that should affect the MVP

### 1. High: unrelated records can stop birthday delivery

Locations: `bot/reminders.py:82`, `:90`, `:97`; `bot/firestore_client.py:173`.

Birthdays are rendered first but sent only after follow-ups and prayers have been
processed. A prayer with `next_remind_date=None` raises `AttributeError` before any
send. The code checks the date before checking `remind_count`, so even a completed
two-touch request fails. This is a state the handler itself writes on its second
touch, while leaving the prayer pending. Mixing null/string dates also raises
`TypeError` in the prayer snapshot sort. A follow-up with an empty due date raises
`ValueError` before sending birthdays.

Reproduced all three with synthetic records. Birthday send count was zero for the
two handler failures. Validate records and isolate sections so malformed pastoral
data cannot suppress a required birthday notification.

### 2. High: incomplete startup can permanently suppress today's birthdays

Locations: `bot/firestore_client.py:226`; `bot/reminders.py:54`, `:62`, `:109`;
`bot/main.py:458`.

Cache startup waits 15 seconds but ignores whether the wait succeeded. The morning
handler does not check readiness. If members have not loaded, it sends an empty
check-in and persists today's successful date. Once the member snapshot arrives,
the next invocation sees the date and skips birthdays for that day. Separately,
health returns HTTP 200/status ok with incomplete caches whenever any member exists.

Reproduced the empty-check-in/day-suppression sequence and the partial-cache 200.
Require the data needed for birthday delivery before marking that day complete;
return a retryable failure when unavailable. A direct authoritative member read
for the scheduled birthday task would simplify this dependency.

### 3. High: daily deduplication does not prevent duplicate birthday sends

Location: `bot/reminders.py:53`, `:123`, `:147`.

The handler reads a cached date, sends, and then writes Firestore. Concurrent
requests can both send. Even sequential calls can duplicate while the metadata
snapshot is stale. A send followed by a failed prayer update or failed completion
write is another retry ambiguity. The process-local chat lock does not cover cron.

Reproduced two sends from concurrent calls and two sequential calls with a stale
cache. Use a durable per-destination/local-date claim, explicit send state, and
recovery for ambiguous outcomes. Do not promise exactly-once external delivery.

### 4. High: webhook acknowledgement is disconnected from finished work

Locations: `bot/main.py:244`, `:257`, `:425`, `:435`.

The message handler creates a task and returns; the webhook then returns success
before the task completes. Work is neither durable nor awaited. With request-based
Cloud Run billing, CPU is allocated only during requests, making this background
pattern unreliable; instance termination can lose the task under either billing
mode. This follows [Cloud Run's CPU allocation documentation](https://docs.cloud.google.com/run/docs/configuring/billing-settings).

Rapid messages cancel the previous task without preserving its content. Dedup IDs
are recorded before processing and retained after failure: a retried failed update
can be acknowledged without another attempt. The dedup set also disappears on
restart and differs between instances.

Reproduced handler-return-before-processing, loss of the first of two burst
messages, and acknowledgement of a previously failed update without reprocessing.
Email ingestion must use persisted work and await bounded processing inside a
request, or acknowledge only after a durable enqueue. Do not reuse this handler.

### 5. High: authentication fails open on missing configuration

Locations: `bot/main.py:102`, `:179`, `:363`, `:418`, `:439`.

Missing chat ID authorizes every chat. Missing webhook or cron secret disables its
respective check. Callback queries do not call the chat authorization helper at
all. On a publicly reachable service with unset secrets, callers can forge updates
or trigger scheduled sends. Actual deployment exposure was not inspected.

Reproduced webhook acceptance without a header when the secret is empty, and a
callback reaching confirmation logic without a chat authorization check. Require
production configuration, reject unauthenticated requests, and authorize callbacks
consistently. The new private email service should require Scheduler OIDC/IAM.

### 6. High: existing agent handling conflicts with transient email processing

Locations: `bot/main.py:234`; `bot/agent.py:487`, `:682`, `:860`;
`bot/firestore_client.py:466`.

The bot logs message previews and tool arguments and persists conversation text to
Firestore. This may be intended for the existing pastoral assistant, but passing
work email through the same path would retain its contents in logs and history.
This finding does not mean the application currently reads or leaks email; email
ingestion does not exist yet.

Use the planned stateless classifier and a separate redacted logging contract.
Exclude member/pastoral history from the email model state. Audit exception and
HTTP-client logging as part of that boundary.

### 7. High before credentialed builds: container copies the entire bot directory

Location: `bot/Dockerfile:17`; build context at `cloudbuild.yaml:4`.

There is no `bot/.dockerignore` or Dockerfile-specific ignore file. `COPY . .` copies
everything included in the `bot/` build context. Local `.env`, service-account keys,
and future OAuth caches placed there can become image contents. Git ignore rules
do not substitute for Docker build exclusions. No leaked credential or affected
image was observed. Git-triggered source builds may not include untracked local
files, but local context builds remain vulnerable.

Add exclusions at the actual context root and supply secrets at runtime. See
[Docker's build-context exclusion rules](https://docs.docker.com/build/concepts/context/#dockerignore-files).

### 8. Medium: birthdays depend on unrelated Gemini and cache initialization

Locations: `bot/agent.py:43`; `bot/main.py:46`, `:359`, `:402`.

Although birthday generation is deterministic, startup imports the Gemini agent,
requires its API key, subscribes to all seven collections, and initializes the full
Telegram application. Unrelated configuration/cache failures can therefore prevent
the birthday endpoint from becoming usable. An unavailable Gemini inference call
is not itself part of birthday generation, but Gemini configuration remains a
startup dependency.

Keep the new email entry point independent. In a focused birthday hardening change,
make deterministic scheduled delivery independent of the chat agent as well.

### 9. Medium: scheduled-delivery health is not observable

Locations: `bot/main.py:116`, `:458`; `bot/reminders.py:147`.

Status reports cache counts/uptime rather than last successful scheduled run, send
failure, or pending retry. Health does not establish birthday delivery. No checked-in
monitoring policy, independent failure channel, or missed-job detection exists.
That does not prove external monitoring is absent.

Record poll/send timestamps and outcomes. Monitor missed expected jobs separately
from process liveness. A quiet inbox must not be mistaken for a failed classifier.

### 10. Medium: declared confirmations and undo do not match execution

Locations: `bot/agent.py:320`, `:522`, `:537`; `bot/firestore_client.py:495`.

The prompt says to confirm before saving, but domain writes execute immediately in
the dispatcher. Successful writes can show an Undo button even though undo is a
stub. Source comments describing queued writes are misleading. Do not reuse this
agent as an authorization boundary for future mailbox changes.

The email MVP remains read-only. Separately align existing confirmation behavior,
receipts, and available controls with what the application actually implements.

## Additional confirmed defects and maintenance gaps

| Issue | Evidence and consequence |
| --- | --- |
| Prayer creation always fails | `firestore_client.py:378` calls `.isoformat()` on a float. Reproduced `AttributeError`; no record was added. Use datetime arithmetic. |
| Natural-language birthday tool fails | `agent.py:497` passes `past_days` to `tools.py:106`, whose wrapper does not accept it. Reproduced `TypeError`, even for the default zero. Direct `/upcoming` is a different path. |
| Unknown-year birthday inconsistency | Morning matching accepts `0000-MM-DD`; `firestore_client.py:285` parses a full date and drops year zero. Reproduced omission from upcoming birthdays on the birthday itself. |
| Member lookups lose document IDs | Member cache at `firestore_client.py:129` stores data without `_id`, while `tools.py:78` expects it to find interviews. Unless IDs also exist inside documents, member profiles cannot reliably retrieve history. |
| Unbounded/unescaped morning output | `reminders.py:123` sends the combined message with Markdown enabled, without escaping names/topics or limiting message length. Provider rejection of malformed formatting or oversized output can also prevent birthday delivery. This was identified from source, without sending a live Telegram test. |
| Arbitrary reminder delivery is unwired | `add_reminder` persists pending reminders, but no current route or handler calls `get_due_reminders`/`mark_reminder_fired`. A successful scheduling response does not establish eventual delivery. |
| Blocking work inside async paths | Gemini uses synchronous `generate_content`; Firestore operations are synchronous too. They run directly inside async handlers, blocking other requests on that event loop. Use asynchronous clients or bounded thread offloading for the new worker. |
| Weak importer failure semantics | `csv_import.py:101` continues with an empty name index if reading members fails, risking duplicates. Newly added names are not put into that index, so repeated names in one file can duplicate. Existing matches have `created_at` overwritten. |
| Unvalidated dates and writes | Follow-up/reminder APIs accept raw strings, and due reminders compare ISO strings rather than normalized instants. Mixed offsets can misorder time. Interview creation and member update are separate writes, allowing partial success. |
| Dead copied code | `agent.py:865` calls undefined `get_mode`; later it calls undefined `get_japan_date`. The routine is currently unwired, so this is not a current birthday blocker. |
| Build/deploy reproducibility | All dependencies use open lower bounds; no lock or test stage exists. Build deploys only the legacy service and does not declare its full runtime configuration. Treat existing live settings as unknown until inventoried. |

## Missing email capabilities

These are expected implementation gaps, distinct from defects in existing behavior:
Graph enrollment/refresh, monitored-folder discovery, durable cursors/work records,
Jev client/schema/policy, transactional delivery state, retries, content bounds,
retention, email health/monitoring, and evaluation fixtures. None currently exists.
The data volume does not justify a new framework, database, or general agent platform.

## Recommended order

1. Harden birthday delivery before treating its current service as a protected
   baseline: authoritative/readiness-checked input, resilient section processing,
   durable daily claims, plain/escaped output, and fixed-date regression tests.
2. Close production authentication and build-context gaps before new credentials
   or work mail are introduced. Inventory actual deployed settings.
3. Prove permitted unattended Graph access; keep this ahead of classifier tuning.
4. Implement the separate request-driven email service with a durable processing
   lifecycle. Reuse Telegram/Firestore libraries and infrastructure, not the chat
   agent's history, debounce, or in-memory deduplication.
5. Add Jev/policy, provider-failure tests, delivery monitoring, and the trial gates.
   Broader quorum repairs can remain a separate backlog unless they block birthdays.

## Validation performed

All seven Python modules parse. Fifteen isolated synthetic behavior checks
reproduced the conditions described above, using current function bodies extracted
with Python AST and fake storage/bot/clock objects. No provider SDKs were invoked.
The harness is at
`C:/Users/barra/AppData/Local/Temp/stew_review_repro.py` and ran on bundled Python
3.12.14; this is not a Python 3.11 container or end-to-end deployment test.

Live GCP/Telegram/Graph behavior, Firestore security/IAM, installed dependency
versions, and real member data remain unverified. This review changes documentation
only and leaves the existing runtime code untouched.
