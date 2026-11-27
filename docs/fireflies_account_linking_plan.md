# Fireflies Account Linking: Implementation Plan

> Status: **draft, not implemented.** Branch: `account-link`. Last updated: 2026-11-27.
> Sections are numbered so they can be revised one at a time. Nothing is built until the plan is approved.
> Also covers a separate change: keeping users logged in across page refreshes (section 22).

---

## 1. Goal

A user links their own Fireflies account to their platform account. The platform then ingests every meeting that account can see, keeps them up to date (automatically or on demand), and the user queries them from the existing Assistant chat.

The chatbot needs **no changes**: synced meetings are written to the same tables, through the same ingestion pipeline, as meetings added by link today.

---

## 2. Decisions Made

| # | Topic | Decision |
|---|-------|----------|
| D1 | Project of synced meetings | When linking, the user may enter a **sync project** name (optional). If given, every **newly** synced meeting goes into that project. If left empty, newly synced meetings get **no project**; they are searchable when no project filter is set. The sync project can be changed or cleared later in the Fireflies panel; the change applies to meetings synced afterwards. Any meeting's project can be changed individually on the Meetings page. |
| D2 | Disconnect | A confirmation popup warns that synced meetings are deleted **7 days** after disconnecting. They are deleted after 7 days. |
| D3 | Ongoing sync | After linking, the user chooses **auto-sync on or off**. On: new meetings are pulled in automatically after Fireflies finishes transcribing them. Off: the user syncs with a **Sync now** button placed with the link ingestion feature. |
| D4 | Which meetings | **Everything the API key can see**, including meetings the user joined but did not organize. |
| D5 | Database | **No new tables.** New columns are added to the existing `users` and `meetings` tables. |
| D6 | Auto-sync method | **Polling** (assumed, see section 17, Q1). |
| D7 | How the account is linked | The user's **Fireflies API key**, not an OAuth "Sign in with Fireflies" redirect. Fireflies documents API keys as the only way to authenticate to its API; its OAuth server issues tokens for its MCP server, which it documents for AI assistants. Evidence in section 20. |
| D8 | Where ingestion status is shown | The **Ingest page** is the single place for ingestion: linking, syncing, link submission, and the status of every meeting (processed, failed, skipped) with retry. The **Meetings page** only shows processed meetings: browse them, read transcripts, assign projects. |
| D9 | Failed and skipped meetings | Failed meetings are retried automatically, at most once an hour, for 7 days after the first failure; then only the **Retry** button retries them. A meeting whose transcript Fireflies no longer has is marked **Skipped**, kept visible with the reason, and not retried automatically. Failure handling never deletes a meeting. Section 10.1. |
| D10 | Meetings deleted or edited in Fireflies | A full sync every `FIREFLIES_FULL_SYNC_DAYS` (7) picks up edited transcripts and schedules synced meetings that are no longer in Fireflies for deletion after the same 7-day retention as a disconnect. They are kept if they reappear (section 9, step 6). |
| D11 | Background processing | Redis plus two arq worker processes replace in-process background work; the API only enqueues (section 23.1). |

---

## 3. Fireflies API Facts This Plan Relies On

Source: docs.fireflies.ai (read 2026-11-25).

- **`user` query** without an `id` returns the owner of the API key (`user_id`, `email`, `name`). Used to validate a key.
- **`transcripts` query**: `limit` (max **50**), `skip`, `fromDate` / `toDate` (ISO 8601; filters on meeting creation date). It can return `sentences` and `summary` for every transcript in the same call. Fireflies documents no response-size limit; 50 full transcripts can be several MB, so this plan requests **10 per page** (section 7).
- **Rate limits**: Free 30/min and 500/day; Pro 60/min and 5,000/day; Business and Enterprise have no daily cap. A 429 response carries `Retry-After`.
- **Webhooks (V2)**: event `meeting.transcribed`. They are configured by hand in each user's Fireflies dashboard (no API), need a public HTTPS URL, and **fire only for meetings the user owns**. Because of the last point they cannot cover D4 on their own, which is why D6 uses polling.

---

## 4. Architecture Rules Kept Unchanged

- **Backend never does AI work; engine never does auth.** The backend fetches transcripts from Fireflies and stores them; all NLP and embeddings still go through the engine via the existing `BackendPipelineOrchestrator.orchestrate_meeting_ingestion`.
- **Frontend has no DB or LLM access**; all new actions go through `services/api_client.py`.
- **Per-account isolation**: every row keeps its `user_id`; a meeting ID owned by another account is never written to.
- **Idempotency**: the existing SHA-256 content-hash check skips unchanged meetings without calling the engine.
- **Atomic writes**: the existing single-transaction 4-tier upsert is reused as is.
- **Migrations**: Alembic, run at startup under the existing advisory lock.
- **FS-AI-Engine:** no changes for Fireflies linking. The production hardening (section 23) changes the engine in four bounded places: vector retrieval uses the index (23.5), request and LLM-concurrency limits (23.4), CORS removed (23.9), query text kept out of INFO logs (23.10). What the chat pipeline answers does not change.
- **Background work runs in worker processes, not in the API process** (section 23.1). The API only validates, writes small rows and enqueues jobs.

---

## 5. Data Model Changes (one migration, columns only)

Migration `FS-backend/alembic/versions/<rev>_fireflies_linking_columns.py`, revising `a3f1c9d27b40`. Written idempotently (each step checks before acting), like the existing migrations.

### 5.1 `users`: 8 new columns (the Fireflies connection)

| Column | Type | Meaning |
|--------|------|---------|
| `fireflies_api_key_encrypted` | `Text`, null | Fernet-encrypted API key. **Not null = linked**, null = not linked. |
| `fireflies_account_email` | `String(255)`, null | Fireflies account email, shown in the UI. |
| `fireflies_auto_sync` | `Boolean`, not null, default `false` | Auto-sync setting. |
| `fireflies_sync_project_id` | `String(100)`, null, FK `projects.id` `ON DELETE SET NULL` | Project that **newly** synced meetings are put in (scoped ID `{user_id}:{name}`). Null = no project. |
| `fireflies_sync_heartbeat_at` | `DateTime(timezone=True)`, null | **Heartbeat** of a running sync: set when the sync starts and refreshed after every meeting it handles; null when no sync is running. A sync is treated as crashed only when the heartbeat is older than **60 minutes** (no progress for an hour), so a long first sync is never mistaken for a crashed one. |
| `fireflies_last_synced_at` | `DateTime(timezone=True)`, null | **Start** time of the last sync that completed without stopping early. Cursor for incremental syncs. |
| `fireflies_last_full_sync_at` | `DateTime(timezone=True)`, null | **Start** time of the last **full** sync that completed. When null or older than `FIREFLIES_FULL_SYNC_DAYS`, the next sync is a full sync (section 9, step 3). |
| `fireflies_last_sync_result` | `JSONB`, null | Last sync attempt's outcome: `{"started_at", "finished_at", "outcome", "new_or_updated", "unchanged", "failed", "busy", "skipped_other_account", "skipped_other_account_meetings", "error", "retry_after"}`. `skipped_other_account_meetings` lists up to 50 `{"id", "title"}` of meetings skipped because another platform account owns them. `busy` counts meetings another ingestion was already processing (section 10). `retry_after` is set only for `stopped_rate_limited`. `outcome` is one of `completed`, `stopped_rate_limited`, `stopped_auth_error`, `stopped_account_error`, `stopped_disconnected`, `stopped_meeting_cap`, `stopped_error` (classification in section 23.2). The result also carries `"full_sync": bool` and `"removed_from_fireflies": int` (section 9, step 6). |

One platform account can link **one** Fireflies account at a time (a consequence of storing the connection on `users`).

### 5.2 `meetings`: 3 new columns

| Column | Type | Meaning |
|--------|------|---------|
| `source` | `String(20)`, not null, server default `'link'` | `link` or `fireflies_sync`. All existing rows become `link`. |
| `purge_at` | `DateTime(timezone=True)`, null, indexed | When this meeting will be permanently deleted. Null = kept. |
| `retry_until` | `DateTime(timezone=True)`, null | Only meaningful when `status = 'FAILED'` (section 10.1): in the future = retried automatically until then; in the past = automatic retries stopped; null = skipped (transcript not available in Fireflies). Set to null when a meeting is processed successfully. **Backfill:** existing `FAILED` rows get the migration time (retries stopped, so they show as failed with a Retry button rather than as skipped). |

`status` keeps its two existing values, `PROCESSED` and `FAILED`. No new status value is added, because the engine excludes meetings with `status = 'FAILED'` from the chatbot's meeting lists (`chat_nodes.py`); a new value would make failed or skipped meetings appear in chatbot answers.

### 5.3 Rules for `source` and `purge_at`

- A meeting created by a sync gets `source = fireflies_sync`; created by link ingestion, `source = link`.
- **Link ingestion of an existing meeting** sets `source = link` and `purge_at = null`, including when the content hash is unchanged. A meeting the user added explicitly is never purged.
- **A sync never changes `source`.**
- **A sync clears `purge_at`** on every meeting it finds in Fireflies, whether processing it then succeeds, is unchanged or fails.

### 5.4 Timestamps and time zones

- Every new timestamp column is `DateTime(timezone=True)` (Postgres `timestamptz`) and holds UTC.
- Every time comparison (`purge_at <= now()`, `retry_until > now()`, heartbeat age, retry spacing) is done **in SQL against `now()`**, never against a Python clock, so backend and database clocks cannot disagree.
- The existing `created_at` / `updated_at` columns are `timestamp without time zone`. The code writes timezone-aware UTC values, and Postgres converts them to the connection's `TimeZone` before dropping the zone. **The development database runs with `TimeZone = Asia/Kolkata`** (checked 2026-11-27), so the stored values are IST wall-clock times, not UTC.
- **Fix (implemented in Part 2 as its own migration, `c4e8a1f20b93_timestamps_to_timestamptz`, ahead of the stage 1 migration):** convert every existing timestamp column (`users.created_at`, `projects.created_at`, `meetings.created_at`, `meetings.updated_at`) to `timestamptz` with `ALTER COLUMN … TYPE timestamptz USING <col> AT TIME ZONE current_setting('TimeZone')`. This reads each stored value in the zone it was written with, so no value shifts. The models declare `DateTime(timezone=True)`. After this every timestamp is an absolute instant, and the connection's time zone only affects how values are displayed, never how they compare.
- The migration must run against the same database it was written to, with its `TimeZone` setting unchanged. A database whose `TimeZone` was changed after rows were written cannot be converted exactly; the migration logs the `TimeZone` it used.

---

## 6. API Key Storage

Why linking uses an API key rather than an OAuth redirect: see decision D7 and the evidence in section 20.

### 6.1 Encryption at rest

- The key is stored only in `users.fireflies_api_key_encrypted`, encrypted. The plaintext never touches the database.
- New module `app/core/crypto.py` with `encrypt_secret(str) -> str` and `decrypt_secret(str) -> str`, using `cryptography.fernet.MultiFernet`. Fernet is AES-128-CBC with a random IV per value, authenticated with HMAC-SHA256: a tampered value fails to decrypt instead of producing garbage.
- Encryption keys come from the env var `FIREFLIES_KEY_ENCRYPTION_KEY`: **one or more Fernet keys, comma-separated**. The **first** key encrypts; **every** listed key can decrypt. Generate a key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- The encryption keys live in the backend environment, never in the database. A leaked database dump or backup therefore contains only ciphertext.
- Passwords are hashed (bcrypt) because the server only verifies them. The Fireflies key must be sent to Fireflies, so it has to be recoverable, which is why it is encrypted rather than hashed.
- **Limit:** whoever controls the running backend (database plus environment) can decrypt the keys. This is true of any service that stores third-party credentials; in production `FIREFLIES_KEY_ENCRYPTION_KEY` belongs in the deployment's secrets store.

### 6.2 Rotating the encryption key

1. Put a new key **first** in `FIREFLIES_KEY_ENCRYPTION_KEY`, keep the old key after it, and restart the backend.
2. New and updated values are encrypted with the new key; existing values still decrypt with the old one.
3. **Lazy re-encryption:** whenever `decrypt_secret` succeeds with a key other than the first, the value is re-encrypted with the first key and saved.
4. **Background re-encryption:** the hourly `reencrypt_keys` cron job (section 11) re-encrypts every stored value that the first key cannot decrypt, so accounts that never sync (auto-sync off) are migrated too.
5. Remove the old key only after a full hourly tick has run with no value left to re-encrypt (the job logs the count it re-encrypted).

If a stored value cannot be decrypted with **any** listed key, a sync stops with `stopped_auth_error` and the user must reconnect.

### 6.3 Where the plaintext key can appear

- **Request body**: `POST /fireflies/connect` declares `api_key` and `password`, and `POST /fireflies/disconnect` declares `password`, as Pydantic `SecretStr`. If the request model is printed, logged or included in an exception, it shows `**********`; the code reads it only through `.get_secret_value()` at the one place it is used.
- **Backend memory**: decrypted in exactly four places, each only for the duration of its Fireflies calls and never stored on module-level objects:
  1. `connect` (validating a new key; this is the plaintext from the request, then encrypted);
  2. `run_sync` (section 9);
  3. `retry_meeting` (section 10.1);
  4. the link-ingestion background task, when the user is linked (section 13).
- **Logs**: never logged. The existing request logging (`app/core/logging_config.py`) records method, path, status, IP and user agent, not bodies or the `Authorization` header. Errors from Fireflies carry Fireflies' response text, which does not contain the key.
- **API responses**: never returned. `GET /fireflies/status` exposes only the Fireflies account email.
- **Other services**: never sent to FS-AI-Engine or the frontend.
- **Frontend**: the key field is cleared right after **Connect** is pressed (successful or not), and the key is never written to Streamlit session state (section 14.2).
- **In transit**: the key travels frontend → backend → Fireflies. Backend → Fireflies is HTTPS. **Frontend → backend must be HTTPS in production**; local development uses plain HTTP on localhost.
- **After disconnect**: the stored value is set to null immediately (section 12).

### 6.4 When linking is not configured

If `FIREFLIES_KEY_ENCRYPTION_KEY` is **not set**:
- The backend still starts.
- `POST /fireflies/connect` and `POST /fireflies/sync` return **503** "Fireflies account linking is not configured on this server."
- Auto-sync is skipped.
- The purge job still runs.

---

## 7. Fireflies Client Changes (`app/services/fireflies_client.py`)

- `FirefliesClient(api_key=...)` is already supported by the constructor; sync creates one client per user with that user's decrypted key.
- New `get_current_user() -> dict`: runs the `user` query, returns `{"user_id", "email", "name"}`.
- New `list_transcripts(limit=10, skip=0, from_date: datetime | None = None, to_date: datetime | None = None) -> list[dict]`: runs the `transcripts` query (`fromDate`, `toDate`, `limit`, `skip`) with the same fields as the existing `TRANSCRIPT_QUERY` and returns each item in the same normalized shape `fetch_transcript_by_id` returns today. Page size is **10** full transcripts (about 5 requests per 50 meetings). If a page times out, it is requested once more with `limit=5` (same `skip`) before the error counts as a failure. The caller advances with `skip += number of items received`, and stops when a page returns fewer items than the `limit` it asked for.
- **Error classification** reads Fireflies' GraphQL error codes instead of collapsing every GraphQL error to 400 (section 23.2). This also fixes today's link-ingestion messages.
- The normalization in `fetch_transcript_by_id` moves into a shared helper so both methods return identical shapes.
- `fetch_transcript_by_id` gets `save_to_file: bool = False`. Link ingestion passes `settings.save_raw_transcripts` (`SAVE_RAW_TRANSCRIPTS`, default `false`, section 23.7); **sync and synced-meeting retries always pass `False`**, so no synced transcript is stored outside the database and the 7-day deletion is complete.

---

## 8. Backend Endpoints

New router `app/api/v1/endpoints/fireflies.py`, mounted at `/api/v1/fireflies`, protected by `get_current_user` like every other non-auth route. Schemas in `app/schemas/fireflies.py`.

| Method & path | Body | Success | Errors |
|---------------|------|---------|--------|
| `POST /fireflies/connect` | `{"password": str, "api_key": str, "sync_project": str \| null}` (`sync_project` optional) | **200** status object; enqueues a full sync (section 9) | 403 wrong password; 400 key rejected by Fireflies; 409 already linked (disconnect first); 429 too many connect attempts; 503 not configured |
| `GET /fireflies/status` | - | **200** status object | - |
| `PATCH /fireflies/settings` | `{"auto_sync": bool, "sync_project": str \| null}`, both optional; only fields present are changed | **200** status object | 409 not linked |
| `POST /fireflies/sync` | - | **202** `{"status": "queued"}` | 409 not linked; 409 a sync is already queued or running; 503 not configured or job queue unavailable |
| `POST /fireflies/disconnect` | `{"password": str}` | **200** `{"purge_at", "meetings_scheduled"}` | 403 wrong password; 409 not linked |

**Password confirmation** (`connect` and `disconnect`): the user's platform password is checked with the existing `verify_password`. A wrong password returns **403**, not 401, because the frontend treats any 401 as an expired login and logs the user out. This protects against a stolen login token being used to link an attacker's Fireflies key or to schedule the user's synced meetings for deletion (section 21).

**Connect rate limit**: at most **5** `connect` attempts per platform account per hour (`RATE_LIMIT_FIREFLIES_CONNECT`), counted whether they succeed or fail; the 6th returns **429** with `Retry-After`. This stops the endpoint being used to test stolen Fireflies keys (a working key reveals its owner's email). The counter is stored in Redis (section 23.3), so it is exact across processes and replicas and survives restarts.

**Sync project** (`sync_project` in `connect` and `settings`): a project name as the user types it. A non-empty name is resolved with the existing `claim_project` (creates the project if new) and its scoped ID is stored in `users.fireflies_sync_project_id`. Empty or `null` stores null (no project). Changing it never moves meetings that are already stored.

**Status object**: `{"linked", "account_email", "auto_sync", "sync_project", "sync_queued", "sync_running", "sync_heartbeat_at", "last_synced_at", "last_sync_result", "pending_deletion": {"meetings", "purge_at"} | null}`. `sync_running` is true when `fireflies_sync_heartbeat_at` is set and less than 60 minutes old. `sync_queued` is true when the job `sync:{user_id}` is waiting in the queue for a free sync slot (section 23.1). `pending_deletion` reports meetings of this user with a non-null `purge_at` (earliest date).

New meetings endpoint in `app/api/v1/endpoints/meetings.py`:

| Method & path | Body | Success | Errors |
|---------------|------|---------|--------|
| `PATCH /meetings/{meeting_id}` | `{"project_id": str \| null}` | **200** updated meeting summary | 404 not found in this account |
| `POST /meetings/{meeting_id}/retry` | - | **202** `{"status": "queued"}`; the retry runs in the background (section 10.1) | 404 not found in this account; 409 meeting is `PROCESSED`; 409 `source = fireflies_sync` and no Fireflies account linked ("Reconnect Fireflies to retry this meeting."); 409 meeting changed in the last 60 seconds ("A retry was just requested. Wait a minute before retrying again.") |

The 60-second rule (`updated_at > now() - 60 s` in SQL) stops repeated clicks from queuing many retries, each of which costs Fireflies requests and Gemini calls.

`project_id` is resolved with the existing `claim_project` (creates the project if new, namespaced `{user_id}:{name}`); `null` removes the meeting from its project. This works for any meeting of the account, whatever its `source` or current project.

In **one transaction** the endpoint updates `meetings.project_id` **and** `metadata.project_id` on every `speaker_turns` row of that meeting (the value the orchestrator writes: the scoped project ID, or `"default-project"` for none). This is required because chat retrieval (`FS-AI-Engine/engine/graphs/chat_retrieval.py`) filters on `t.metadata->>'project_id' = :p_id OR m.project_id = :p_id`: without the turn update, a meeting moved from project A to project B would still be returned by a chat filtered to project A.

`MeetingSummary` and `MeetingResponse` (`app/schemas/models.py`) gain `source`, `purge_at`, `retry_until`, `updated_at` and `ingestion_status`. `updated_at` is the time of the **last change of any kind** to the meeting row: an ingestion attempt, a project change, or deletion being scheduled or cancelled. It is shown as "Last updated", not "Last attempt" (section 14.4). `ingestion_status` is computed by the backend so the frontend shows it as is:

| `status` | `retry_until` | `ingestion_status` |
|----------|---------------|--------------------|
| `PROCESSED` | any | `processed` |
| `FAILED` | in the future | `retrying` |
| `FAILED` | now or in the past | `retries_stopped` |
| `FAILED` | null | `skipped` |

`GET /meetings/` keeps returning every meeting of the account, whatever its status; the Ingest page and the Meetings page filter it (section 14).

---

## 9. Sync Algorithm (`app/services/fireflies_sync.py`)

`run_sync(user_id)` is the worker job `sync:{user_id}` on the `sync` queue (section 23.1). Connect, Sync now and the auto-sync scheduler only **enqueue** it. At most `FIREFLIES_MAX_CONCURRENT_SYNCS` syncs run at once across the whole platform; the rest wait in the queue (shown as "Queued").

1. **Claim**: one `UPDATE users SET fireflies_sync_heartbeat_at = now() WHERE id = :user AND fireflies_api_key_encrypted IS NOT NULL AND (fireflies_sync_heartbeat_at IS NULL OR fireflies_sync_heartbeat_at < now() - interval '60 minutes')`. Zero rows updated means not linked or already running: return without doing anything. (The job ID already prevents two queued or running syncs per account; the claim also covers a crashed worker whose job record was lost.)
2. Record `run_started = now()`. Decrypt the key; build `FirefliesClient(api_key=...)`.
3. **Full or incremental**:
   - **Full sync** when `fireflies_last_full_sync_at` is null or older than `FIREFLIES_FULL_SYNC_DAYS` (default 7): `from_date = None`, `to_date = run_started`. Fixing `to_date` at the start freezes the set being paged, so meetings created during the sync cannot shift the `skip` offsets.
   - **Incremental sync** otherwise: `from_date = fireflies_last_synced_at - 24 h`, `to_date = run_started`. The 24-hour overlap catches meetings created before the last sync but transcribed after it; the content-hash check makes the overlap cheap.
4. **Page** through `list_transcripts(limit=10, skip=n, from_date, to_date)`, advancing `skip` by the number of items received, until a page returns fewer items than requested (section 7). For a full sync, record every transcript ID seen in `seen_ids`. For each transcript, in order:
   1. Re-check that the user is still linked; if not, stop with `stopped_disconnected`.
   2. If the meeting ID belongs to another platform account: count `skipped_other_account`, add `{"id", "title"}` to `skipped_other_account_meetings` (first 50 only), continue.
   3. Clear `purge_at` on this user's meeting with that ID, if it exists.
   4. Choose the project: if this user has **no** meeting with this ID yet, `project_id = users.fireflies_sync_project_id` (read once at the start of the run; may be null). If the meeting already exists, `project_id = None`. Call `orchestrate_meeting_ingestion(transcript, user_id, project_id=<that value>, source="fireflies_sync")`. Count `unchanged` on an idempotent hit, `busy` if the meeting is locked by another ingestion (section 10), else `new_or_updated`.
   5. On failure: call `record_failure(..., project_id=<same value as 4.4>, source="fireflies_sync")` (section 10.1) and count `failed`.
   6. **Heartbeat**: `UPDATE users SET fireflies_sync_heartbeat_at = now()`.
   7. **Meeting cap**: if `FIREFLIES_SYNC_MAX_MEETINGS` is above 0 and `new_or_updated + failed` (the meetings sent to the engine in this run) has reached it, stop with `stopped_meeting_cap`. Unchanged meetings do not count, so the next run passes over them cheaply and continues where this one stopped.
5. **Failed meetings**: a `FAILED` meeting that appears in the listing window is simply processed again by step 4, whatever its `retry_until` (Fireflies has just returned its transcript). Failed meetings outside the window are retried by the scheduled retry job (section 11), not by the sync.
6. **Reconciliation** (full sync with outcome `completed` only): every meeting of this user with `source = 'fireflies_sync'`, `purge_at IS NULL` and an ID **not** in `seen_ids` gets `purge_at = now() + 7 days`. These are meetings deleted in Fireflies, or no longer visible to this key. The number is stored as `removed_from_fireflies`. They follow the same 7-day retention as a disconnect (D2): searchable until then, labelled "No longer in your Fireflies account: will be deleted on {date}" (sections 14.2, 14.3), and kept if they reappear in a later sync (step 4.3 clears `purge_at`). A full sync that stops early never reconciles, so a partial listing can never schedule deletions. Transcripts edited in Fireflies are picked up by the same full sync through the content-hash check.
7. **Stop conditions** (whole sync), from the classified Fireflies error (section 23.2):
   - rate limited: `stopped_rate_limited`, with `retry_after`;
   - key rejected: `stopped_auth_error`, and `fireflies_auto_sync = false` so polling stops using a dead key;
   - account problem (`paid_required`, `account_cancelled`, `feature_disabled`, `require_ai_credits`): `stopped_account_error`, and `fireflies_auto_sync = false`; the panel shows Fireflies' own message;
   - any other unexpected error: `stopped_error`.
8. **Finish** (always, in `finally`): `fireflies_sync_heartbeat_at = null`; write `fireflies_last_sync_result` (with `started_at = run_started` and `full_sync`). When the outcome is `completed`: `fireflies_last_synced_at = run_started`, and for a full sync also `fireflies_last_full_sync_at = run_started`. Otherwise neither changes, so an interrupted sync is fully retried by the next run.

Meetings are processed **one at a time** within a sync, so one account's sync sends the engine one ingestion at a time. The platform-wide limit on concurrent syncs bounds the total.

For an existing meeting a sync passes `project_id=None`, and the orchestrator's existing `meeting.project_id = project_id or meeting.project_id` keeps the current value. So **a sync never changes the project of a meeting already stored**, whether that project came from the sync project, from link ingestion, or from the user on the Meetings page (including "No project"). The sync project only applies to meetings the sync creates.

---

## 10. Orchestrator and Failure-Record Changes

- `orchestrate_meeting_ingestion` gains `source: str = "link"`.
  - New meeting: stored with that `source`.
  - Existing meeting, `source = "link"`: set `meeting.source = "link"` and `purge_at = null`, **also on the idempotent-hit path** (which today returns early).
  - Existing meeting, `source = "fireflies_sync"`: `source` is left unchanged.
- **Turn metadata uses the meeting's effective project.** Today the orchestrator writes `metadata.project_id` from the `project_id` argument (`"default-project"` when none), even when the meeting keeps an existing project. A sync passes `project_id=None` for meetings that already exist, so re-processing a meeting the user filed under project B would write `"default-project"` into its turns. The orchestrator will write the meeting's resulting `project_id` (after `project_id or meeting.project_id`) into turn metadata instead, so `meetings.project_id` and the turns always agree.
- `record_failure` (`app/services/ingestion_failures.py`) gains `source: str = "link"`, used only when it creates a new `FAILED` meeting. Its other changes are in section 10.1.
- On success the orchestrator sets `retry_until = null` (the meeting is `PROCESSED`).
- **One ingestion per meeting at a time.** A link submission, a sync, an automatic retry and a Retry click can reach the same meeting simultaneously; two concurrent runs would both insert the same turn IDs and one would fail with a database error. `orchestrate_meeting_ingestion` therefore takes a **Redis lock** `lock:ingest:{user_id}:{meeting_id}` (redis-py `Lock`, `timeout=1800` s, non-blocking) for its whole run, released in `finally`. If the lock is already held, it does nothing and returns `{"status": "busy"}`: the run already in progress produces the result. `record_failure` is not called for a busy result. A crashed worker cannot leave the meeting locked for more than 30 minutes, because the lock expires on its own. The lock holds **no database connection**, unlike a Postgres advisory lock, which would have kept a pooled connection open for the whole ingestion (section 23.6).
- **Blocking database work runs in a thread.** The orchestrator's synchronous SQLAlchemy phases (idempotency check, the 4-tier transaction, `record_failure`) run through `asyncio.to_thread`, so one ingestion's database writes never stall other jobs in the same worker (section 23.1).
- **Logging (S8):** new log lines, and the existing ingestion log lines in `pipeline_orchestrator.py` and `ingest.py`, log meeting **IDs** and counts only, never titles or transcript text, so purged meetings do not survive in logs by name.

### 10.1 Failed and skipped meetings

**Two kinds of failure**, decided by `record_failure` from the exception:

| Kind | When | `summary` (shown to the user) | `retry_until` |
|------|------|-------------------------------|---------------|
| **Skipped** | Fetching the transcript **by ID** returns `object_not_found`, or returns `forbidden` / `not_in_team` / `require_elevated_privilege` while the same key still passes the `user` query (the key works, this account just cannot see this transcript). See section 23.2. | "Skipped: Fireflies has no transcript with this ID that this account can access. It may have been deleted, access may have been removed, or the link may be wrong. It will not be retried automatically." | `null` |
| **Failed** | Any other error (Fireflies outage or `request_timeout`, rate limit, network, engine error, database error, `invalid_arguments`) | The existing friendly reason from `friendly_failure_reason` | If currently null (new meeting, or previously skipped): `now() + 7 days`. Otherwise unchanged, so later failures do not extend the window. |

Further rules:
- `record_failure` always sets `updated_at = now()`, even when the reason text is unchanged, so every attempt moves it.
- `updated_at` also changes on other edits (project change, deletion scheduled or cancelled). For retry spacing this is safe: such an edit can only **delay** the next automatic retry by up to an hour, never cause an extra one.
- A `PROCESSED` meeting is never turned into `FAILED` (existing rule, kept).
- **Failure handling never deletes a meeting.** Skipped and failed meetings stay listed on the Ingest page with their reason until a retry succeeds. They are removed only by the 7-day purge, which applies to synced meetings only: after a disconnect (section 12), or when a full sync finds them no longer in Fireflies (section 9, step 6). This plan adds no "delete meeting" action.
- "Skipped" can only occur when a transcript is fetched **by ID** (link ingestion, automatic retry, Retry button). The sync's listing only returns transcripts that exist and are visible, so a sync never produces a skipped meeting. (Meetings that disappear from Fireflies are handled by reconciliation, section 9 step 6.)

**Automatic retries** (background job, section 11):
- Candidates: `status = 'FAILED'`, `retry_until > now()`, and `updated_at < now() - 1 hour`.
- At most **20** meetings per run, oldest `updated_at` first, processed one at a time.
- Key used: `source = fireflies_sync` meetings need the owner's linked key; if the owner is not linked they are not retried (they are scheduled for deletion, section 12). `source = link` meetings use the owner's linked key if linked, otherwise the server's `FIREFLIES_API_KEY` (the same rule as link ingestion, section 13).
- Retries run whether auto-sync is on or off; auto-sync only controls fetching **new** meetings.
- A Fireflies **429** ends the current run; the remaining meetings are tried in a later run.

**Retry button** (`POST /meetings/{id}/retry`, section 8):
- Allowed for any `FAILED` meeting (failed, retries stopped or skipped), at any time.
- Worker job `retry:{user_id}:{meeting_id}` on the `ingest` queue (section 23.1): fetch the transcript by ID with the key chosen as above, then `orchestrate_meeting_ingestion(..., project_id=None, source=<the meeting's source>)`. `custom_title` = the stored title for `source = link` (keeps a custom title), `None` for synced meetings (Fireflies title). `save_to_file` = `settings.save_raw_transcripts` for `link`, `False` for `fireflies_sync` (section 7).
- On failure, `record_failure` applies the table above: a retry that fails again with a 404 stays skipped; one that fails with another error gets a new 7-day window only if it was skipped before.

**Shared code**: the automatic retry and the Retry button both call `retry_meeting(user_id, meeting_id)` in the new module `app/services/ingestion_retry.py`.

---

## 11. Scheduled Jobs: Auto-Sync, Retries, Re-Encryption, Purge (`app/worker.py`)

- These run as **arq cron jobs** in the sync worker (section 23.1), not in the API process. arq runs each cron job **once per scheduled time across all worker replicas** (cron jobs are unique by default), so no leader election or advisory lock is needed.
- The scheduling jobs are short: they only decide what is due and **enqueue** work. A long sync never delays them, because syncs run as separate jobs.

| Cron job | Schedule | What it does |
|----------|----------|--------------|
| `enqueue_due_syncs` | every minute | Enqueues `sync:{user_id}` for every user due for auto-sync (rules below). The job ID makes enqueueing an already queued or running sync a no-op. |
| `enqueue_due_retries` | every minute | Enqueues `retry:{user_id}:{meeting_id}` for up to 20 eligible `FAILED` meetings (section 10.1), oldest `updated_at` first. |
| `reencrypt_keys` | hourly | Re-encrypts every `fireflies_api_key_encrypted` value the first encryption key cannot decrypt (section 6.2). |
| `purge_meetings` | hourly | Deletes meetings with `purge_at <= now()`, in batches of 50 per transaction until none remain, so no single transaction holds locks on a very large number of rows. Topics and turns are removed by the existing `ON DELETE CASCADE` foreign keys. Projects are not deleted (they belong to the user). |

- **Auto-sync**: a user is due when **all** of these hold:
  - a key is stored, `fireflies_auto_sync = true`, and no sync is running (`fireflies_sync_heartbeat_at` null or older than 60 minutes);
  - the **last attempt** (`fireflies_last_sync_result.finished_at`, whatever its outcome) is null or older than `FIREFLIES_AUTO_SYNC_INTERVAL_MINUTES` (default **10**, minimum 5). Using the last attempt rather than `fireflies_last_synced_at` (last *completed* sync) stops a failing sync from being retried every 60 s;
  - after a `stopped_rate_limited` outcome, the current time is past `fireflies_last_sync_result.retry_after` (section 23.2), or `finished_at` + interval when Fireflies gave no retry time.

  Skipped entirely when `FIREFLIES_KEY_ENCRYPTION_KEY` is unset.
- **Latency**: a new Fireflies meeting is stored about one interval (10 min by default) plus up to 60 s after Fireflies finishes transcribing it, plus its processing time, plus any wait for a free sync slot. It can be queried as soon as it is stored.
- **Retry budget**: each meeting is retried at most once an hour, for 7 days after its first failure, so at most 24 attempts a day per meeting and none after the window closes.
- Polling cost: one Fireflies request per auto-sync when nothing is new (about 144 requests/day at 10 minutes), plus one full listing per `FIREFLIES_FULL_SYNC_DAYS` (about 1 request per 10 meetings). This fits the Free plan's 500/day for a typical account.

---

## 12. Disconnect and 7-Day Retention

`POST /fireflies/disconnect` (password confirmed, section 8), in one transaction:

1. Set `fireflies_api_key_encrypted`, `fireflies_account_email`, `fireflies_sync_project_id` to null and `fireflies_auto_sync` to false (the project itself and its meetings are not touched). A running sync stops before its next meeting (section 9, step 4.1).
2. `UPDATE meetings SET purge_at = now() + 7 days WHERE user_id = :user AND source = 'fireflies_sync' AND purge_at IS NULL`.
3. Return the purge date and the number of meetings scheduled.

Until `purge_at` the meetings stay searchable in the chat and visible on the Meetings page with their deletion date.

**Reconnecting**: connecting enqueues a full sync, which clears `purge_at` on every meeting it finds (section 5.3).
- Same Fireflies account: all of its meetings are found, so none are deleted (except meetings since deleted in Fireflies).
- Different Fireflies account: only meetings the new account can also see are kept; the rest are deleted on schedule.

`source = link` meetings are never scheduled for deletion.

**The Fireflies key itself stays valid** after disconnecting: the platform deletes its copy, but only Fireflies can revoke a key. The disconnect dialog tells the user to regenerate the key in Fireflies if they want it revoked (section 14.2).

---

## 13. Link Ingestion Changes (`app/api/v1/endpoints/ingest.py`)

- Fireflies dashboard links use the **user's linked key** when linked, otherwise the server's `FIREFLIES_API_KEY` (current behaviour; see Q4).
- The endpoint validates the link, checks ownership, enqueues the worker job `ingest:{user_id}:{meeting_id}` on the `ingest` queue, and returns 202 (section 23.1). FastAPI `BackgroundTasks` is no longer used. A second submission of the same meeting while the first is queued or running is a no-op (same job ID), and the response says "already being processed".
- The job passes `source="link"` (the default), so re-ingesting a synced meeting by link keeps it permanently (section 5.3).
- Raw media links: the `uploadAudio` call is also enqueued as a job, instead of running in the API process.
- Raw transcript files: written only when `SAVE_RAW_TRANSCRIPTS=true` (default `false`), per account (section 23.7).
- Rate limit: `RATE_LIMIT_INGEST_LINK` per user (section 23.3).

---

## 14. Frontend Changes

### 14.1 `services/api_client.py`

New methods, each with the existing 401 handling: `fireflies_connect(password, api_key, sync_project)`, `fireflies_status()`, `fireflies_update_settings(auto_sync=None, sync_project=None)` (sends only the fields given), `fireflies_sync()`, `fireflies_disconnect(password)`, `set_meeting_project(meeting_id, project_id)`, `retry_meeting(meeting_id)`. A 403 (wrong password) and a 429 (too many attempts) are returned as errors to show, not as a logout.

### 14.2 Ingest page (`app_pages/ingest.py`)

The Ingest page holds everything about getting meetings in, in three sections from top to bottom:

1. **Fireflies account** (below)
2. **Add a meeting by link**: the existing form
3. **Ingestion status** (section 14.4)

The page header subtitle changes from "Submit a recording or Fireflies link for background transcription." to "Connect Fireflies, add meetings by link, and track every ingestion."

#### Section 1: Fireflies account

- **Not linked**:
  - A **Get my API key** button (`st.link_button`) that opens `https://app.fireflies.ai/integrations/custom/fireflies` in a new tab. This is the page Fireflies' own docs name for the key (section 20.1).
  - The steps shown next to it: "Log in to Fireflies, copy the API key on that page, paste it here."
  - An optional **Sync project** field (existing projects, "No project" (the default), "Add new project…"), with the caption "All meetings synced from this Fireflies account go into this project. Leave as No project to sort them later on the Meetings page."
  - An API key password field, a **Your platform password** field (confirms it is really the account owner, section 8), and a **Connect** button. Both fields are cleared right after **Connect** is pressed, whether linking succeeds or fails, and neither value is written to Streamlit session state (section 6.3).
- **Linked**:
  - Account email.
  - **Sync project** selector (existing projects, "No project", "Add new project…") showing the current value; saving calls `PATCH /fireflies/settings`. Caption: "New meetings from Fireflies go into this project. Meetings already stored keep their project; change those on the Meetings page."
  - **Auto-sync** toggle (calls `PATCH /fireflies/settings`).
  - **Sync now** button, disabled while a sync is queued or running.
  - Status line: "Queued: waiting for a free sync slot" (`sync_queued`), or "Syncing… last activity {sync_heartbeat_at}", or "Last synced {finished_at}" with the counts from `last_sync_result` (new or updated, unchanged, failed, skipped because another account owns the meeting), or its error. `stopped_meeting_cap` shows "Stopped after {N} meetings (server limit). The next sync continues." `stopped_account_error` shows Fireflies' own message (for example "You need to be subscribed to a paid plan to perform this action") and "Auto-sync was turned off." A full sync with `removed_from_fireflies > 0` adds "{N} meetings are no longer in your Fireflies account and will be deleted on {date}." Details of each failed meeting are in **Ingestion status**.
  - If `skipped_other_account_meetings` is not empty: an expander "{N} meetings skipped: already in another account on this platform" listing their titles. These meetings are not stored for this account, so they cannot appear in **Ingestion status**.
  - While a sync runs, the panel refreshes every 5 s (`st.fragment(run_every=5)`).
  - **Disconnect** button opening a confirmation dialog (`st.dialog`) with this text: "Disconnecting removes your Fireflies API key from this platform now. The {N} meetings synced from Fireflies will be permanently deleted on {date} (7 days from now). Meetings you added by link are not affected. Reconnecting the same Fireflies account before then keeps them. Your key still works at Fireflies: to revoke it, regenerate it in Fireflies (Integrations → Fireflies API)." The dialog has a **Your platform password** field and the buttons **Cancel** and **Disconnect**; a wrong password shows "Incorrect password." and keeps the dialog open.
- If `pending_deletion` is set: a warning "{N} synced meetings will be deleted on {date}." This shows whether or not an account is currently linked.

#### Section 2: Add a meeting by link

- The form is unchanged.
- The message after a successful submission changes from "Processing is asynchronous. Check the **Meetings** page in a moment to see new meetings; a failed ingestion is listed there with its error." to "Processing runs in the background. The meeting appears in **Ingestion status** below when it has finished or failed."
- A submitted link appears in **Ingestion status** only once it has been processed or has failed. A "processing" row is not stored: it would need a new `status` value, which the chatbot would count as a meeting (section 5.2).

### 14.3 Meetings page (`app_pages/meetings.py`)

The Meetings page is for **processed meetings only**: browse them, read their transcripts and assign projects. Questions are asked on the Assistant page, as today.

- Lists `ingestion_status = processed` meetings only (it already hides `FAILED` ones).
- **Removed:** the red "N ingestions failed. Submit the link again on the Ingest page to retry." box and its list of failures. That information is on the Ingest page (section 14.4).
- Project filter gains a **"No project"** option so synced meetings are easy to find and sort.
- Meeting detail gains a **project selector** (existing projects, "No project", "Add new project…") that calls `PATCH /meetings/{id}`.
- Meetings with `purge_at` show "Will be deleted on {date}", followed by the reason: "Fireflies disconnected" when no Fireflies account is linked, otherwise "No longer in your Fireflies account" (section 9, step 6). This stays on the Meetings page because it warns about losing a meeting the user is looking at.
- The source of a meeting (link or Fireflies) is not shown here; it is a column of the Ingest page table.

### 14.4 Ingest page, Section 3: Ingestion status

Data: `GET /meetings/` (every meeting of the account, all statuses).

**Count tiles** (one row): Processed · Retrying · Retries stopped · Skipped, counted by `ingestion_status`.

**Filters**: Status (All, Processed, Retrying, Retries stopped, Skipped) and Source (All, Link, Fireflies). A **Refresh** button reloads the data.

**Table** (`st.dataframe`, which shows values as plain text), sorted by last updated, newest first:

| Column | Content |
|--------|---------|
| Title | Meeting title |
| Source | "Link" or "Fireflies" |
| Project | Project name, or "—" for none |
| Status | "Processed" · "Failed, retrying until {retry_until}" · "Failed, retries stopped" · "Skipped, not available in Fireflies" |
| Reason | The stored `summary` for failed and skipped meetings; empty for processed ones |
| Last updated | `updated_at`: the last change of any kind to the meeting (section 8) |

**Retry**:
- The table uses row selection (`on_select="rerun"`, `selection_mode="multi-row"`). Streamlit tables cannot hold a button per row; selection plus one button is the supported pattern and allows retrying several meetings at once.
- A **Retry selected** button calls `retry_meeting` for each selected row that is not processed.
- Afterwards a message reports the outcome: "{N} queued for retry." plus, when relevant, "{M} already processed, not retried.", "{K} synced meetings need Fireflies reconnected before they can be retried." and "{J} were retried less than a minute ago; try again shortly." (the 409s from section 8). Results appear in the table after processing; use **Refresh**.

**Auto-refresh**: while a sync is running, this section and the Fireflies section refresh every 5 s (`st.fragment(run_every=5)`). Otherwise the data is reloaded with **Refresh** or when the page is opened.

### 14.5 Overview page (`app_pages/overview.py`)

- The warning "{N} meeting ingestion(s) failed. See the **Meetings** page for details." becomes "{N} meeting ingestion(s) failed or were skipped. See **Ingestion status** on the **Ingest** page."

### 14.6 Rendering rule

Every title, project name, reason, email and transcript text that new UI code puts into HTML (`st.markdown(..., unsafe_allow_html=True)`) goes through the existing `esc()` (`utils/formatting.py`), as the current pages already do. Text shown with `st.dataframe`, `st.text`, `st.caption` or widget labels needs no escaping.

---

## 15. Configuration

| Variable | Service | Default | Purpose |
|----------|---------|---------|---------|
| `FIREFLIES_KEY_ENCRYPTION_KEY` | backend | none | One or more comma-separated Fernet keys; the first encrypts, all decrypt (section 6.2). Unset = linking disabled (section 6.4). Keep in the secrets store in production. |
| `FIREFLIES_AUTO_SYNC_INTERVAL_MINUTES` | backend | `10` (min 5) | Auto-sync polling interval. |
| `FIREFLIES_SYNC_MAX_MEETINGS` | backend | `0` (no cap) | Most meetings one sync run sends to the engine before stopping with `stopped_meeting_cap` (section 9, step 4.7). Limits Gemini cost per run for very large accounts. |
| `FIREFLIES_API_KEY` | backend | none | Unchanged: fallback key for link ingestion when the user has not linked an account. |

| `FIREFLIES_FULL_SYNC_DAYS` | backend | `7` (min 1) | How often a sync is a full sync that also reconciles deletions and edits (section 9, steps 3 and 6). |
| `FIREFLIES_MAX_CONCURRENT_SYNCS` | sync worker | `2` | Syncs running at once across the platform (section 23.1). |

New dependency: `cryptography` (backend `requirements.txt`, `pyproject.toml`, `uv.lock`). The retention period is fixed at **7 days** (D2), not configurable. Infrastructure, rate-limit, request-limit and database settings are in section 23.11.

---

## 16. Files to Change

**FS-backend, new**
- `alembic/versions/<rev>_fireflies_linking_columns.py`
- `app/core/crypto.py`
- `app/services/fireflies_sync.py`
- `app/worker.py`: arq worker settings for the `ingest` and `sync` queues, job functions and cron jobs (sections 11, 23.1)
- `app/core/redis.py`: shared Redis connection, rate limiter and lock helpers (section 23.1, 23.3)
- `app/core/ratelimit.py`: route limits (section 23.3)
- `app/services/ingestion_retry.py`
- `app/api/v1/endpoints/fireflies.py`
- `app/schemas/fireflies.py`

**FS-backend, edited**
- `app/core/database.py`: new columns on `UserModel` and `MeetingModel`; `DateTime(timezone=True)` on all timestamp columns (section 5.4); pool settings from env (section 23.6)
- `app/core/config.py`: the settings in sections 15, 22.3 and 23.11
- `app/main.py`: Redis connection pool in the lifespan; body-size middleware (23.4); CORS removed (23.9); rate-limit exception handler (23.3); proxy-header trust for the frontend's address (23.3)
- `app/services/fireflies_client.py`: section 7
- `app/services/pipeline_orchestrator.py`: `source` parameter; turn metadata uses the meeting's effective project; `retry_until = null` on success; per-meeting Redis lock; database phases in `asyncio.to_thread`; log IDs not titles (section 10)
- `app/services/ingestion_failures.py`: `source` parameter; skipped vs failed, `retry_until`, `updated_at` (section 10.1)
- `app/api/v1/router.py`: mount the fireflies router
- `app/api/v1/endpoints/ingest.py`: enqueue jobs instead of `BackgroundTasks`; section 13; log IDs not titles (section 10)
- `app/api/v1/endpoints/query.py`: request limits (23.4); rate limits (23.3); database lookup in a thread pool (23.1)
- `app/api/v1/endpoints/auth.py`: login throttling and sign-up limit (23.3); `POST /auth/logout`, `POST /auth/logout-all` (23.8); orphan-adoption block removed (23.8)
- `app/api/deps.py`: rejects revoked tokens (23.8)
- `app/core/security.py`: `jti` claim (23.8)
- `app/api/v1/endpoints/meetings.py`: `PATCH /meetings/{id}`, `POST /meetings/{id}/retry`
- `app/services/meeting_service.py`: set project on the meeting and its turns' metadata in one transaction; expose `source`, `purge_at`, `retry_until`, `updated_at`, `ingestion_status`; turn counts in one grouped query (23.6)
- `app/schemas/models.py`: the same fields on meeting schemas
- `requirements.txt`, `pyproject.toml`, `uv.lock`: `cryptography`, `arq`, `redis`, `slowapi` (with `limits`)
- `.env.example`: every new backend and worker variable (sections 15, 22.3, 23.11)
- `Dockerfile`: unchanged image; the workers use the same image with a different command (23.9)

**FS-frontend, edited**
- `services/api_client.py`
- `app_pages/ingest.py`: Fireflies section, new link-submission message, Ingestion status section (sections 14.2, 14.4)
- `app_pages/meetings.py`: processed meetings only, failure box removed, project selector, "No project" filter, deletion label (section 14.3)
- `app_pages/overview.py`: failure warning points to the Ingest page (section 14.5)
- `services/api_client.py` also sends the browser's IP as `X-Forwarded-For` (23.3) and shows 429 / 413 messages instead of logging out

**FS-AI-Engine, edited** (section 23)
- `engine/graphs/chat_retrieval.py`: index-backed vector query plus full-text query (23.5)
- `engine/api/routers/engine.py`: request limits (23.4)
- `engine/graphs/chat_synthesis.py`, `engine/graphs/chat_rewrite.py`: history capped to the last 10 messages (23.4)
- `engine/services/llm_client.py`, `engine/services/embedding_service.py`: concurrency limits and explicit SDK retries (23.4)
- `engine/main.py`: CORS removed (23.9); pgvector version check at startup (23.5)
- `engine/services/rag_service.py`: query text out of INFO logs (23.10)
- `engine/core/config.py`, `.env.example`: engine settings in 23.11

**Repository root, new**
- `docker-compose.yml`: all services on an internal network; only the frontend published (23.9)

---

## 17. Open Questions

- **Q1. Auto-sync method.** Polling is assumed (section 11). The alternative is the Fireflies webhook, which is instant but needs per-user setup in the Fireflies dashboard and a public HTTPS backend, and misses meetings the user did not organize. Confirm polling.
- **Q2. Replacing the key while linked.** Currently `POST /fireflies/connect` returns 409 when already linked, so changing the key means disconnect and reconnect (meetings are briefly scheduled for deletion, then the full sync un-schedules them). Alternative: allow replacing the key in place when it belongs to the same Fireflies account.
- **Q3. Meetings shared by several platform accounts (issue 1 / S2).** Meeting IDs are Fireflies transcript IDs and are unique across all platform accounts. When two users of this platform attended the same Fireflies meeting, only the first to ingest it gets it; the other's sync skips it (now listed by title, section 14.2). The same rule lets one user claim a meeting ID first and block another, and the existing 409 message ("Meeting X is already in use") confirms the ID exists in another account. Options: (a) keep the rule as a known limitation; (b) a separate plan for per-account copies of a meeting, which changes the meeting primary key, turn IDs and citations.
- **Q4. Server-key fallback (S1).** Link ingestion and automatic retries of link meetings fall back to the server's `FIREFLIES_API_KEY` when the user has not linked an account, so any signed-up user can import meetings visible to the server owner's Fireflies account by transcript ID, and all such users share that key's rate limit. Proposed: an `ALLOW_SERVER_FIREFLIES_KEY` setting, default `false`, requiring the user's own linked key. Not applied; the current behaviour is kept until decided.

---

## 18. Implementation Stages

Each stage ends with a stop for review.

0. **Infrastructure and hardening** (section 23; prerequisite for stages 1-4): Redis, arq workers, job enqueueing in place of `BackgroundTasks`, rate limits, request limits, Fireflies error classification, timestamp conversion, connection-pool settings, grouped turn counts, raw-transcript setting, engine retrieval and limits, CORS, logging, `docker-compose.yml`.
1. **Backend core**: migration, model columns, `crypto.py`, Fireflies client methods, `fireflies_sync.py` (heartbeat, page size, meeting cap), orchestrator `source` and per-meeting lock, fireflies endpoints (password confirmation, connect rate limit), router, config, dependency.
2. **Failures, retries and scheduled jobs**: skipped/failed handling in `record_failure`, `ingestion_retry.py`, cron jobs in `app/worker.py` (auto-sync, retries, re-encryption, purge), reconciliation, disconnect scheduling.
3. **Link ingestion and meetings API**: user key in link ingestion, `PATCH /meetings/{id}`, `POST /meetings/{id}/retry`, new schema fields including `ingestion_status`.
4. **Frontend**: API client methods; Ingest page (Fireflies section with disconnect dialog, link message, Ingestion status with retry); Meetings page (processed only, project selector, labels); Overview warning.
5. **Login session survives refresh** (section 22): independent of stages 1-4; needs only the token revocation from stage 0 (23.8). Starts with the prerequisite spike in section 22.5. Backend token claims, `POST /auth/refresh`, `GET /auth/session`, idle setting; frontend cookie component, restore, refresh check, logout flag.

---

## 19. Out of Scope

- Fireflies webhooks (unless chosen in Q1).
- More than one linked Fireflies account per platform account.
- Automatic ingestion of raw audio/video uploads (an existing known gap).
- Changes to what the chat pipeline answers. (The engine changes in section 23 affect performance, limits and logging only.)
- OAuth "Sign in with Fireflies" linking (see section 20).
- Per-account copies of meetings shared between platform accounts (Q3), and removing the server-key fallback (Q4), until decided.
- The engine's query-routing limits for very large libraries (only the 50 most recent meetings are offered to the routing prompt; inventory answers list every meeting). They affect answer quality, not stability, and need their own plan.

---

## 20. Evidence: Why Linking Uses an API Key, Not an OAuth Redirect

All pages below were fetched on **2026-11-25**. Quotes are verbatim. Fireflies serves each docs page as Markdown at the same path plus `.md`.

### 20.1 The Fireflies API authenticates with API keys only

Source: [docs.fireflies.ai/fundamentals/authorization](https://docs.fireflies.ai/fundamentals/authorization)

> "The Fireflies API implements token-based authentication, which ensures that only authorized users can access certain data and functionalities."

> "We use a standard bearer token authentication mechanism. This means that to make authorized requests to the API, you must include an `Authorization` header with a valid token."

> "Follow these steps to obtain your API key for the Fireflies API:
> 1. Log in to your account at fireflies.ai
> 2. Navigate to the Integrations section
> 3. Click on Fireflies API (`https://app.fireflies.ai/integrations/custom/fireflies`)
> 4. Copy and store your API key securely"

> "Authorization: Bearer your_api_key"

The page mentions "OAuth" **0 times**. It documents no authorize URL, token URL, client registration or scopes for the API.

### 20.2 Fireflies' OAuth server protects its MCP server, not the GraphQL API

Fireflies does run an OAuth server. Its discovery documents, fetched verbatim:

`GET https://api.fireflies.ai/.well-known/oauth-authorization-server` (HTTP 200):

```json
{"issuer":"https://api.fireflies.ai/","service_documentation":"https://docs.fireflies.ai/","authorization_endpoint":"https://api.fireflies.ai/authorize","response_types_supported":["code"],"code_challenge_methods_supported":["S256"],"token_endpoint":"https://api.fireflies.ai/token","token_endpoint_auth_methods_supported":["client_secret_post","none"],"grant_types_supported":["authorization_code","refresh_token"],"scopes_supported":["profile","email","meetings.upload"],"revocation_endpoint":"https://api.fireflies.ai/revoke","revocation_endpoint_auth_methods_supported":["client_secret_post"],"registration_endpoint":"https://api.fireflies.ai/register","authorization_response_iss_parameter_supported":true,"client_id_metadata_document_supported":true}
```

`GET https://api.fireflies.ai/.well-known/oauth-protected-resource` (HTTP 200):

```json
{"resource":"https://api.fireflies.ai/mcp","authorization_servers":["https://api.fireflies.ai/"],"scopes_supported":["profile","email","meetings.upload"],"resource_documentation":"https://docs.fireflies.ai/"}
```

What this shows:
- The only resource the OAuth server declares is `https://api.fireflies.ai/mcp`, the MCP server. The GraphQL API (`https://api.fireflies.ai/graphql`) is not declared.
- The scopes are `profile`, `email` and `meetings.upload`. None of them grants reading transcripts.
- No Fireflies page says an OAuth token is accepted by the GraphQL API.

### 20.3 Fireflies documents that OAuth for AI assistants

Source: [docs.fireflies.ai/getting-started/mcp-configuration](https://docs.fireflies.ai/getting-started/mcp-configuration)

> Prerequisite: "AI tool that supports MCP (such as Claude, OpenAI Connector, Cursor, Devin, or other MCP-compatible applications)"

> "Add the Fireflies MCP server to your AI tool's configuration. The remote server URL is https://api.fireflies.ai/mcp which uses OAuth with your Fireflies account. The exact steps depend on your specific AI application."

No Fireflies page describes registering a third-party web application for OAuth or offering "Sign in with Fireflies" in one.

### 20.4 Reading transcripts through the MCP server would lose data our pipeline uses

Source: [docs.fireflies.ai/mcp-tools/overview](https://docs.fireflies.ai/mcp-tools/overview)

- Listing tool `fireflies_get_transcripts`: "**Returns:** Meeting IDs, titles, dates, participants, summaries (excludes detailed transcript content)." Each meeting would need a second call to fetch its transcript.
- Transcript tool `fireflies_get_transcript`: "**Returns:** Sentences with speaker attribution and metadata. Each sentence is prefixed with a `[start - end]` timestamp range (e.g. `[00:05 - 00:08] Speaker: text`, switching to `h:mm:ss` past an hour)".

The transcript would arrive as formatted text with second-level times. The GraphQL `sentences` field (used by link ingestion today) returns structured `index`, `speaker_id`, `speaker_name`, `text`, `raw_text`, `start_time`, `end_time`.

### 20.5 Rate limits are shared, so MCP gives no extra capacity

Source: [docs.fireflies.ai/fundamentals/limits](https://docs.fireflies.ai/fundamentals/limits)

> "The MCP server shares these limits: a tool call and a GraphQL request from the same user spend the same budget."

### 20.6 Conclusion

1. The only documented way for an application like ours to read a user's Fireflies meetings is the GraphQL API with that user's API key (20.1).
2. Fireflies' OAuth tokens are issued for its MCP server (20.2), which Fireflies documents for AI assistants (20.3).
3. Using that MCP server would rely on undocumented behaviour for a web app, and it would give lower-fidelity transcripts (20.4) at no rate-limit advantage (20.5).

Linking therefore uses the API key, with a **Get my API key** button that opens the Fireflies page where the key is shown (section 14.2).

**Revisit if** Fireflies documents OAuth access to its GraphQL API for third-party applications. Sections 6 and 14.2 would then switch to a redirect flow, and the rest of this plan would stay as is.

---

## 21. Security Review

Review of this plan and the code it builds on (2026-11-25). S1 and S2 are open questions (section 17, Q4 and Q3); everything else below is addressed in this plan.

| # | Risk | Severity | How this plan handles it |
|---|------|----------|--------------------------|
| S1 | Server-key fallback lets any user import meetings visible to the server owner's Fireflies account | High | **Open**, section 17 Q4 |
| S2 | Meeting-ID claiming across accounts; 409 message confirms an ID exists elsewhere | High | **Open**, section 17 Q3 |
| S3 | `connect` used to test stolen Fireflies keys (a working key reveals its owner's email) | High | 5 attempts per account per hour, then 429 (section 8) |
| S4 | A stolen login token used to link an attacker's key or to schedule the user's meetings for deletion | Medium | Platform password required for `connect` and `disconnect`; wrong password is 403 (section 8) |
| S5 | Cost abuse: repeated Retry clicks, very large accounts | Medium | Retry refused within 60 s of the last change (section 8); optional `FIREFLIES_SYNC_MAX_MEETINGS` cap (sections 9, 15) |
| S6 | The Fireflies key still works after disconnect | Medium | Disconnect dialog tells the user to regenerate the key in Fireflies (sections 12, 14.2) |
| S7 | Prompt injection: synced meetings include text written by other people, which goes into LLM prompts | Medium | **Accepted.** The chatbot cannot take actions and every query is scoped to the user's own data, so the impact is limited to misleading answers. Citations let the user check the source. |
| S8 | Data surviving the 7-day purge | Low | Sync writes no raw files (section 7); logs carry IDs, not titles (section 10). Outside the code: database backups keep deleted rows until they expire, and transcripts were already sent to Google Gemini for processing under Google's terms. Backup retention is a deployment setting to document. |
| S9 | Time-zone mismatch making deletions and retries fire early or late (the development database runs in `Asia/Kolkata`) | Low | All timestamp columns converted to `timestamptz` without shifting stored values; SQL `now()` comparisons (section 5.4) |

The production hardening in section 23 adds further fixes found in the 2026-11-27 review: Fireflies error classification, rate limits, request limits, background workers, index-backed retrieval, connection pools, raw transcript files, token revocation, network exposure and logging.

**Checked and not at risk:**
- **XSS:** existing pages HTML-escape all titles, speaker names, quotes and transcript text (`esc()` in `utils/formatting.py`, `highlight()` in `ui/components.py`); new UI follows the same rule (section 14.6).
- **Cross-account data access:** every engine query filters on `user_id`; this plan adds no query without it.
- **CSRF:** login uses bearer tokens, not cookies.
- **SQL injection:** every query in this plan uses bound parameters.
- **API key exposure:** covered in section 6.3.

---

## 22. Login Session That Survives a Page Refresh

> Separate from Fireflies linking; it can be built and shipped on its own (stage 5, section 18).

### 22.1 Problem

The login token is kept only in `st.session_state` (`FS-frontend/ui/auth.py`). Streamlit creates a new session state on every page load, so a browser refresh discards the token and shows the login page.

### 22.2 Behaviour

- A refresh, a new tab or reopening the page keeps the user logged in while their session is valid.
- The session ends when it expires (section 22.3) or the user logs out. The next action or refresh then shows the login page with "Your session has expired. Please log in again." (expiry) or the plain login page (log out).
- Every time value is an **environment variable**. Changing one means editing `.env` and restarting that service; no code change. Settings are read once at startup (`get_settings()` / `frontend_config`), so a running service keeps its old values until restarted. Tokens issued before the restart keep the expiry they were issued with.

### 22.3 Settings

| Variable | Service (`.env`) | Default | Meaning |
|----------|------------------|---------|---------|
| `ACCESS_TOKEN_EXPIRE_MINUTES` | backend | `720` (existing, unchanged) | **Maximum session length**, counted from login. No session lasts longer, whatever the activity. |
| `SESSION_IDLE_TIMEOUT_MINUTES` | backend | `0` | **Inactivity limit.** `0` = off: the session lasts exactly `ACCESS_TOKEN_EXPIRE_MINUTES` from login (today's behaviour). Above `0`: the session also ends after this many minutes without activity, and every activity extends it, up to the maximum. Must be `0` or between `5` and `ACCESS_TOKEN_EXPIRE_MINUTES`; otherwise the backend refuses to start and names the setting. |
| `SESSION_COOKIE_PERSISTENT` | frontend | `false` | `false`: the login is forgotten when the browser is closed (safer on shared computers); refreshes and new tabs still keep it. `true`: the login also survives closing and reopening the browser, until the session expires. |

Example: `ACCESS_TOKEN_EXPIRE_MINUTES=480` and `SESSION_IDLE_TIMEOUT_MINUTES=30` give "logged out after 30 minutes of inactivity, and in any case 8 hours after login".

### 22.4 Backend (`FS-backend`)

- **Token claims** (`app/core/security.py`): `sub` (user id), `auth_time` (login time, new), `iat` (issue time, new), `jti` (random token ID, new, used for revocation, section 23.8), `exp`.
  - Idle limit off: `exp = auth_time + ACCESS_TOKEN_EXPIRE_MINUTES` (as today).
  - Idle limit on: `exp = min(iat + SESSION_IDLE_TIMEOUT_MINUTES, auth_time + ACCESS_TOKEN_EXPIRE_MINUTES)`.
- **Token responses** (`TokenResponse` in `app/schemas/auth.py`, returned by signup, login and refresh) gain:
  - `expires_at`: the token's `exp`;
  - `refresh_after`: `iat + 60 s` when the idle limit is on, `null` when it is off (the client never refreshes).
- **New `POST /auth/refresh`** (Bearer token): returns a new token with the **same** `auth_time` and a recomputed `exp`. 401 if the token is invalid or expired, or if `auth_time + ACCESS_TOKEN_EXPIRE_MINUTES` has passed. With the idle limit off it returns 409 (nothing to refresh). A token without `auth_time` (issued before this change) gets 401, so each user logs in once after the release. The presented token is revoked when the new one is issued (section 23.8), so a refreshed-away token cannot be reused.
- **New `GET /auth/session`** (Bearer token): `{"user", "expires_at", "refresh_after"}` for the presented token. Used to restore a session from the cookie. The existing `GET /auth/me` is unchanged.
- **Config** (`app/core/config.py`): `session_idle_timeout_minutes` with the validation above. `.env.example` documents both backend variables.

### 22.5 Frontend (`FS-frontend`)

- **Cookie** `mi_session` holding the token, written and deleted by a small Streamlit custom component (CCv2, `st.components.v2.component`, new module `ui/session_cookie.py`). CCv2 components run inside the app page, so the cookie belongs to the app's own origin. Attributes: `Path=/`, `SameSite=Strict`, `Secure` when the page is served over HTTPS. `Max-Age` only when `SESSION_COOKIE_PERSISTENT=true`, set to the seconds until `expires_at` and updated on every refresh; otherwise no `Max-Age` (a browser-session cookie).
- **Restore** (start of `app.py`, before the login check): if the session has no token, the `logged_out` flag (below) is not set, and `st.context.cookies` has `mi_session`:
  - call `GET /auth/session` with it;
  - 200: store token, user, `expires_at`, `refresh_after` in `st.session_state`; the user is logged in;
  - 401: delete the cookie and show the login page with the "session expired" message.
- **After login or sign-up**: store `expires_at` and `refresh_after` with the token. The cookie is written on the next script run, because `_start_session` calls `st.rerun()` immediately and a component mounted in that same run may never render. Rule: on every logged-in run, if `st.session_state["cookie_token"]` differs from the current token, mount the cookie writer and set `cookie_token`.
- **Activity and refresh** (idle limit on only): on each **full** script run (a page load, a click, a chat message), if `refresh_after` is set and has passed, call `POST /auth/refresh` and replace token, `expires_at` and `refresh_after`; the cookie is rewritten by the rule above. Fragment reruns (the Ingest page's 5-second auto-refresh, section 14.4) do not run this check, so leaving a page open does not keep a session alive. Refreshing at most once a minute avoids a request on every rerun.
- **Log out**: call `POST /auth/logout` (revokes the token, section 23.8), clear the session, mount the cookie deleter, and set `st.session_state["logged_out"] = True` before `st.rerun()`. `st.context.cookies` reflects the cookies sent when the session started, so without this flag the next run of the same session would still see the old cookie and log the user back in.
- **Expired during use** (any backend 401, `_expire_session` in `services/api_client.py`): additionally delete the cookie, using the same flag rule.
- **Config** (`config.py`): `SESSION_COOKIE_PERSISTENT: bool = False`; `.env.example` documents it.
- New API client methods: `get_session(token)`, `refresh_token()`, `logout()`.
- **Prerequisite spike** (before building this stage): a throwaway script on Streamlit 1.64 confirming that (1) a CCv2 component can set and delete a cookie readable through `st.context.cookies` after a refresh, (2) `st.context.cookies` holds the values sent when the session started, and (3) `st.context.ip_address` returns the browser's IP (needed by section 23.3). If (1) fails, this section is revised before any code is written.

### 22.6 Security

- The cookie is **not HttpOnly**: Streamlit cannot set HttpOnly cookies from Python, so it is written by page script, and a script running on the page could read it. Mitigation: the app escapes all data it renders as HTML (section 14.6), which is what prevents injected scripts. The cookie holds the same token the app already holds; it grants nothing new.
- `SameSite=Strict`: other sites cannot cause the browser to send it.
- With `SESSION_COOKIE_PERSISTENT=false` (default), closing the browser ends the login on that computer. (Browsers set to "continue where you left off" may restore browser-session cookies; `true` or `false`, the token's own expiry still applies.)
- Tabs share the cookie: logging out in one tab takes effect in other open tabs at their next refresh or request (a 401 there, because logging out revokes the shared token, section 23.8).

### 22.7 Files

**FS-backend, edited**: `app/core/security.py` (claims), `app/core/config.py` (idle setting and validation), `app/schemas/auth.py` (`expires_at`, `refresh_after`, session response), `app/api/v1/endpoints/auth.py` (`POST /auth/refresh`, `GET /auth/session`), `.env.example`.

**FS-frontend, new**: `ui/session_cookie.py`.
**FS-frontend, edited**: `app.py` (restore before the login check; refresh check), `ui/auth.py` (store expiry fields, cookie rule, logout flag), `services/api_client.py` (`get_session`, `refresh_token`, cookie deletion on 401), `config.py` (`SESSION_COOKIE_PERSISTENT`), `.env.example`.

**FS-AI-Engine**: none.

---

## 23. Production Hardening

Issues found in the code review of 2026-11-27, each with the chosen fix, the reason for choosing it, and the alternatives rejected. Several apply to the platform as it is today, independent of Fireflies linking. Sources are in 23.13.

### 23.1 Background work: Redis and worker processes

**Problem.**
- Link ingestion runs in FastAPI `BackgroundTasks` inside the API process. Section 11, as first written, put syncs in an in-process loop too.
- That work is lost when the process restarts. It cannot be limited or de-duplicated.
- Its synchronous database writes block the API's event loop, which stalls every user's async requests, including chat.

**Fix.** A durable job queue on Redis with separate worker processes. This is the standard production pattern for long-running work.
- **Redis 7**, one instance, with append-only persistence (`appendonly yes`) so queued jobs survive a Redis restart. Configured with `REDIS_URL`. Redis is used for the job queue, rate limits (23.3), per-meeting locks (section 10) and token revocation (23.8).
- **Queue library: arq 0.28** (released 2026-04-16):
  - asyncio-native, so jobs are coroutines, matching the async backend code;
  - Redis-based;
  - job IDs for de-duplication;
  - cron jobs that run once per scheduled time across replicas;
  - a per-worker job limit (`max_jobs`) and job timeouts.
- **Alternatives rejected:**
  - **Celery:** synchronous-first, so async code would need an event loop wrapped around every task.
  - **taskiq 0.13:** also suitable, but its pluggable brokers are not needed here, where Redis is the only broker.
  - **Keeping `BackgroundTasks`:** no persistence, no limits, no de-duplication.
- **Two worker processes**, built from the backend image with a different command:

| Worker | Command | Queue | Concurrent jobs | Jobs | Job timeout |
|--------|---------|-------|-----------------|------|-------------|
| ingest | `arq app.worker.IngestWorkerSettings` | `ingest` | `WORKER_INGEST_MAX_JOBS` (default 3) | link ingestion, raw-media upload, retries | 1,200 s |
| sync | `arq app.worker.SyncWorkerSettings` | `sync` | `FIREFLIES_MAX_CONCURRENT_SYNCS` (default 2) | Fireflies syncs, and the cron jobs in section 11 | 21,600 s (6 h) for syncs; cron jobs are short |

- **Job IDs:** `ingest:{user_id}:{meeting_id}`, `retry:{user_id}:{meeting_id}` and `sync:{user_id}`, with `keep_result = 0`. Enqueueing an ID that is already queued or running does nothing, and the ID is free again as soon as the job ends.
- **One retry policy, not two.**
  - Every job function catches its own errors and records them (`record_failure`, sync outcomes), so a normal failure never makes arq retry the job.
  - `max_tries = 2` only lets arq re-run a job whose worker died or timed out mid-run.
  - Everything else follows section 10.1.
- **The API process does no ingestion work.** It validates, writes small rows and enqueues.
  - If Redis is unavailable, enqueueing fails. The endpoint returns **503** "Background processing is temporarily unavailable. Try again shortly." and writes nothing.
  - The database lookups inside the API's `async` endpoints (`ingest-link`, `query`, `query/stream`) run through `run_in_threadpool`, so they no longer block the event loop.
- **Worker database access** runs through `asyncio.to_thread` (section 10), so one job's writes do not stall the worker's other jobs.
- **Deploys:** workers get a 2-minute stop grace period (23.9). Stage 0 tests confirm how arq handles a job cut off by shutdown; `max_tries = 2` allows exactly one re-run.

### 23.2 Fireflies error classification

**Problem.**
- Fireflies reports errors inside the GraphQL response as `errors[].code`, with `extensions.code` and `extensions.status`.
- The existing client turns every GraphQL error into status 400. So "not found", "rate limited" and "forbidden" are never recognised.
- Today that shows the wrong message for link ingestion. In this plan it would break the Skipped, rate-limit and dead-key rules.

**What Fireflies documents** (fetched 2026-11-27):
- Every error carries `message`, `code`, `friendly` (whether the message is safe to show users) and `extensions` (`code`, `status`, optional `metadata`).
- For `too_many_requests` (status 429), the retry time is in `extensions.metadata.retryAfter` (epoch milliseconds). The HTTP response also carries a `Retry-After` header in seconds.

**Fix (`fireflies_client.py`).**
- On a response with `errors`, read the first error:
  - `code` = `extensions.code`, else `code`;
  - `status` = `extensions.status`, else the HTTP status;
  - `retry_after` = the `Retry-After` header, else `extensions.metadata.retryAfter`.
- Raise `FirefliesAPIError(code, status, message, friendly, retry_after)`.
- A non-200 response without a GraphQL body uses the HTTP status alone.

| Fireflies code (status) | Class | Fetch by ID (link, retry) | Sync |
|-------------------------|-------|---------------------------|------|
| `object_not_found` (404) | not found | **Skipped** | n/a (the listing only returns transcripts that exist) |
| `forbidden`, `not_in_team`, `require_elevated_privilege` (403) | no access | Run the `user` query with the same key. If it succeeds: **Skipped**. If it fails: key rejected (next row). | key rejected |
| HTTP 401, or `forbidden` from the `user` query itself | key rejected | **Failed** ("Fireflies rejected the API key. Reconnect Fireflies with a new key.") | `stopped_auth_error`; auto-sync off |
| `paid_required`, `account_cancelled`, `feature_disabled` (403), `require_ai_credits` (402) | account problem | **Failed**, showing Fireflies' message (`friendly = true`) | `stopped_account_error`; auto-sync off |
| `too_many_requests` (429) | rate limited | **Failed**, retried later (10.1) | `stopped_rate_limited` with `retry_after` |
| `request_timeout` (408), `invariant_violation` (500), HTTP 5xx, network errors | temporary | **Failed**, retried (10.1) | `stopped_error` (after the page-size fallback in section 7) |
| `invalid_arguments`, `args_required`, any other 400 | our bug | **Failed**, plus an ERROR log with the code | `stopped_error`, plus an ERROR log |
| any undocumented code | temporary | **Failed**, logged with the code | `stopped_error` |

- Fireflies documents no code specifically for an invalid API key, so the "key rejected" row is an inference. Stage 0 confirms it by calling the API with a deliberately invalid key, and the table is adjusted if the response differs.
- `friendly_failure_reason` maps each class to its message. When Fireflies marks an error `friendly`, its own message is shown.

### 23.3 Rate limits and abuse protection

**Problem.** Nothing is rate limited today:
- login can be brute-forced, and each attempt costs a bcrypt check;
- one user can use up the whole Gemini quota through chat;
- ingestion requests are unlimited.

**Library.** `slowapi` 0.1.10 on top of `limits` 5.8, with **Redis storage** and the **moving-window** strategy, which has no burst at window edges. Counters are shared by every process and replica; in-memory counters would not be once more than one process runs. The login failure counter uses the `limits` API directly, because it counts only failed attempts.

| Setting | Default | Key | Applies to |
|---------|---------|-----|------------|
| `RATE_LIMIT_LOGIN_IP` | `20/minute` | client IP | `POST /auth/login` |
| `RATE_LIMIT_LOGIN_FAILURES` | `5/15 minutes` | email (normalised) | failed logins only; reset by a successful login |
| `RATE_LIMIT_SIGNUP_IP` | `5/hour` | client IP | `POST /auth/signup` |
| `RATE_LIMIT_QUERY` | `20/minute;300/day` | user | `POST /query` and `POST /query/stream` together |
| `RATE_LIMIT_INGEST_LINK` | `30/hour` | user | `POST /meetings/ingest-link` |
| `RATE_LIMIT_FIREFLIES_CONNECT` | `5/hour` | user | `POST /fireflies/connect` (section 8) |
| `RATE_LIMIT_RETRY` | `60/hour` | user | `POST /meetings/{id}/retry`, in addition to the 60-second per-meeting rule (section 8) |

- **Sync now** needs no separate limit: the job ID allows one queued or running sync per account.
- **Responses:** every limit answers **429** with `Retry-After`. The frontend shows the message and never treats it as a logout.
- **Login throttling, not account lockout.**
  - After 5 failed attempts for an email, further attempts get "Too many failed attempts. Try again in {N} minutes." until the window passes.
  - A permanent lockout would let anyone lock a real user out just by knowing their email.
  - The same throttle applies to unknown emails, and the message is identical, so it does not reveal whether an account exists. Generic error responses are an OWASP requirement.
- **Client IP.**
  - The frontend calls the backend server-to-server, so without a fix the backend sees one IP for all users.
  - The frontend sends the browser's IP (`st.context.ip_address`, confirmed in the section 22.5 spike) as `X-Forwarded-For`.
  - The backend runs uvicorn with `--proxy-headers --forwarded-allow-ips=$FORWARDED_ALLOW_IPS`, set to the frontend's address only, so no other caller can set it. Requests from anywhere else are keyed by their own connection IP.
  - If the spike shows the IP is not available, per-IP limits apply to the frontend as a whole, `RATE_LIMIT_LOGIN_IP` defaults to `100/minute` instead, and the per-email limit remains the account protection.
- **Redis down:** the limiter **fails open**, allowing requests and logging an ERROR, so a Redis outage does not lock every user out. The `/health` check reports `degraded`. Endpoints that enqueue jobs return 503 regardless (23.1).

### 23.4 Request size and LLM limits

**Problem.**
- `query` has no maximum length.
- `history` accepts 50 messages of 8,000 characters each, and answer synthesis sends all of it to Gemini (`chat_synthesis.py`). That is about 100k tokens per question from anyone with a login.
- Nothing limits request body size.
- Gemini calls have no shared concurrency limit, so ingestion bursts compete with chat inside the engine.

**Fix.**
- **Schemas** (backend `QueryRequest` and engine `EngineQueryRequest`):
  - `query` at most `QUERY_MAX_CHARS` (2,000);
  - `history` at most `HISTORY_MAX_MESSAGES` (10), the frontend's 5 questions and their answers;
  - each history message at most `HISTORY_MESSAGE_MAX_CHARS` (4,000).

  Larger requests get **422** with a clear message. The frontend cuts long answers to 4,000 characters before putting them in history, so it never trips the limit.
- **Engine defence in depth:** rewrite and synthesis keep only the last 10 history messages, whoever the caller is.
- **Body size:**
  - Backend middleware returns **413** for bodies over `MAX_REQUEST_BODY_BYTES` (1 MiB), checking `Content-Length` and counting bytes of chunked bodies.
  - In production the reverse proxy enforces the same limit (23.9).
- **LLM concurrency (engine, per process).** Separate pools so ingestion cannot starve chat inside the engine:

| Setting | Default | Covers |
|---------|---------|--------|
| `LLM_CHAT_MAX_CONCURRENCY` | 16 | rewrite, analysis, synthesis |
| `LLM_INGEST_MAX_CONCURRENCY` | 8 | topic titles, summary overview (the segmenter's existing 6-per-meeting limit stays inside it) |
| `EMBED_CHAT_MAX_CONCURRENCY` | 8 | query embeddings |
| `EMBED_INGEST_MAX_CONCURRENCY` | 4 | turn-embedding batches, across all meetings (today the limit of 4 is per call) |

- **Retries:**
  - `AsyncOpenAI(max_retries=LLM_MAX_RETRIES, timeout=LLM_TIMEOUT_SECONDS)`, with defaults 3 and 60 s, set explicitly in place of the SDK's implicit default of 2.
  - The SDK retries rate-limit (429), server-error and timeout responses with exponential backoff.
- **Quota:** these limits bound concurrency, not the Gemini quota.
  - Production needs a paid Gemini tier sized for the load.
  - A chat question costs 3 LLM calls and 1 embedding call.
  - A meeting ingestion costs about 1 LLM call per topic segment plus 1 embedding call per 50 turns.

### 23.5 Vector retrieval uses the HNSW index (engine)

**Problem.**
- Retrieval orders by a computed `CASE … (1 - (embedding <=> …))` expression (`chat_retrieval.py`).
- Postgres cannot use the HNSW index for that, so every question scans every turn of the account.
- The cost grows with the library, and full-account sync makes libraries large.

**Background.**
- An HNSW search applies the `WHERE` filter after walking the graph. With the default `hnsw.ef_search = 40`, a selective filter, such as one account in a shared table, can return far fewer rows than requested.
- pgvector 0.8.0 added **iterative index scans** (`hnsw.iterative_scan`). They keep walking until the `LIMIT` is filled, up to `hnsw.max_scan_tuples`.
- **This database runs pgvector 0.8.6** (checked 2026-11-27).

**Fix (`engine/graphs/chat_retrieval.py`).** Two candidate queries, merged in Python, replace the single query:

1. **Vector candidates**, in one transaction:
   - Settings: `SET LOCAL hnsw.iterative_scan = 'relaxed_order'`, `SET LOCAL hnsw.ef_search = $HNSW_EF_SEARCH` (100), and `SET LOCAL hnsw.max_scan_tuples = $HNSW_MAX_SCAN_TUPLES` (20,000).
   - Query: `SELECT * FROM (SELECT <columns>, t.embedding <=> :q AS distance FROM speaker_turns t … WHERE t.user_id = :u AND t.embedding IS NOT NULL AND <scope filters> ORDER BY t.embedding <=> :q LIMIT :k) c ORDER BY c.distance`.
   - `relaxed_order` can return rows slightly out of order, and the outer `ORDER BY` restores the exact order (the pattern pgvector documents).
   - `k` is `RETRIEVAL_CANDIDATE_LIMIT`, unchanged at 1,000. `vector_sim = 1 - distance`.
   - The query uses `<=>` (cosine distance), which matches the existing index's `vector_cosine_ops`.
2. **Keyword candidates**, using the existing `tsv` GIN index (stored today but unused):
   - Filter: `WHERE t.user_id = :u AND <scope filters> AND t.tsv @@ websearch_to_tsquery('english', :text)`.
   - Order: `ORDER BY ts_rank(t.tsv, <query>) DESC LIMIT :k`.
   - Each row also computes `1 - (t.embedding <=> :q)`, which is cheap per row, so keyword-only hits still get a vector score.
3. **Merge** by `turn_id`. The existing scoring and selection (vector weight, keyword hits, speaker boost, filler penalty, speaker-diverse stratified selection) are unchanged.

**Notes.**
- **No new index.** `idx_speaker_turns_embedding_hnsw` and `idx_speaker_turns_tsv` already exist. For accounts with few turns, Postgres may choose an exact scan through the `user_id` index instead; that is correct and fast, and no plan is forced.
- **Startup check:** the engine reads the pgvector version. Below 0.8.0 it logs an ERROR and keeps today's exact query, which is correct but slower.
- **Acceptance tests (stage 0):**
  - on the evaluation set, at least 95% of the top-k turns match an exact search;
  - `EXPLAIN` shows the index in use for an account with 50k turns or more;
  - 95th-percentile retrieval latency is recorded at 1k, 50k and 200k turns.
- **Rejected:** a partial HNSW index per account. It needs DDL for every new account and does not scale with the number of accounts.

### 23.6 Database

- **Connection pools** set explicitly per process: `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT` (30 s) and `DB_POOL_RECYCLE` (1,800 s), with `pool_pre_ping` kept.

| Process | Pool size | Overflow |
|---------|-----------|----------|
| backend API | 5 | 5 |
| ingest worker | 5 | 5 |
| sync worker | 5 | 5 |
| engine | 10 | 10 |

- **Budget rule:** the sum of (pool size + overflow) over every process and replica must stay at or below `max_connections - 10`. This database has `max_connections = 100`, and the defaults use 50. Each process logs its pool settings at startup.
- **Session-level advisory locks** are used only for migrations, on a direct connection. The per-meeting lock moved to Redis (section 10). If PgBouncer is added later in transaction mode, migrations must connect directly, bypassing it.
- **Turn counts** in `GET /meetings/` come from one grouped query (`LEFT JOIN (SELECT meeting_id, count(*) … GROUP BY meeting_id)`) instead of one query per meeting.
- **Timestamps:** section 5.4. **Purge batches:** section 11.

### 23.7 Raw transcript files

**Problem.**
- Every link ingestion writes the full transcript JSON to the repo-level `transcripts/` folder.
- Those files are not separated by account, are never deleted, grow without limit, and keep meeting content outside the database.

**Fix.**
- `SAVE_RAW_TRANSCRIPTS` defaults to `false`.
- When it is `true`, files go to `RAW_TRANSCRIPTS_DIR` (default `transcripts`, the existing gitignored folder) as `{user_id}/{meeting_id}.json`, and the purge (section 11) deletes a meeting's file with the meeting.
- Synced meetings never write files (section 7).
- Existing files in `transcripts/` are not touched by code; delete or archive them by hand.

### 23.8 Authentication and sessions

- **Token revocation:**
  - Tokens carry a `jti`.
  - `POST /auth/logout` stores `revoked:{jti}` in Redis, expiring when the token would.
  - `POST /auth/logout-all` stores `revoked_before:{user_id} = now`, expiring after `ACCESS_TOKEN_EXPIRE_MINUTES`.
  - `get_current_user` rejects either kind of revoked token with 401, and `POST /auth/refresh` revokes the token it replaces (section 22.4).
  - If Redis is down, the revocation check **fails open** (logged as an ERROR). Failing closed would log every user out during an outage. This trade-off is accepted and documented.
- **Login throttling and sign-up limits:** section 23.3.
- **Orphan adoption removed:**
  - The sign-up code that gives meetings without an owner to the next new account is deleted.
  - Such rows can only exist in a database upgraded from before accounts existed. The new migration logs how many remain.
  - They stay invisible, because every query filters by `user_id`. An administrator can assign them to an account with a single SQL `UPDATE` (documented in the migration).
- **Secret strength:** `JWT_SECRET` and `ENGINE_API_KEY` keep their 16-character minimum for existing setups. When `BACKEND_ENV=production`, the backend refuses to start unless each is at least 32 characters.
- **Recommended production session values** (defaults unchanged):
  - `ACCESS_TOKEN_EXPIRE_MINUTES=480` and `SESSION_IDLE_TIMEOUT_MINUTES=30`.
  - These follow the OWASP example of secure defaults (20 minutes idle, 8 hours absolute). Choose 15 to 30 minutes idle depending on how sensitive the meetings are.
- **Sign-up:** `ALLOW_SIGNUP` stays `true` by default. Set it to `false` in production once your users have accounts.

### 23.9 Network exposure and deployment

- **`docker-compose.yml`** at the repo root, with these services:
  - `postgres` (`pgvector/pgvector:pg17`);
  - `redis` (`redis:7`, append-only persistence on);
  - `engine`;
  - `backend`;
  - `worker-ingest` and `worker-sync` (the backend image with the arq commands from 23.1, `stop_grace_period: 2m`);
  - `frontend`.
- **Only the frontend publishes a port** (8501). Everything else is on one internal network:
  - the backend is called only by the frontend, server-side;
  - the engine is called only by the backend and the workers;
  - Postgres and Redis are not reachable from outside.
- **TLS:** in production, a reverse proxy (nginx, Caddy or a cloud load balancer) terminates HTTPS in front of the frontend. It must allow WebSockets, which Streamlit uses, and it enforces the body-size limit (23.4).
- **CORS:**
  - The engine's `CORSMiddleware` is removed; no browser calls the engine.
  - The backend's `CORS_ORIGINS` default changes from `*` to empty, which sends no CORS headers; no browser calls the backend either. It is set only if a browser client is added later.
- **Secrets:** `.env` files are passed to containers with `env_file` and never copied into images. Stage 0 checks each `.dockerignore` excludes `.env`.
- **Health checks:** the existing container health checks stay. The backend `/health` adds a Redis check and reports `degraded` when it fails.

### 23.10 Logging and privacy

- The engine logs user questions verbatim at INFO (`[QUERY] '…'` in `rag_service.py`, and the chat-graph start line). The question text moves to DEBUG; INFO logs only its length and an 8-character hash.
- Ingestion logs meeting IDs, not titles (section 10).
- Tokens, API keys and passwords are never logged (section 6.3).

### 23.11 Settings added by this section

| Variable | Service | Default |
|----------|---------|---------|
| `REDIS_URL` | backend, workers | `redis://redis:6379/0` (compose); required |
| `WORKER_INGEST_MAX_JOBS` | ingest worker | `3` |
| `FIREFLIES_MAX_CONCURRENT_SYNCS` | sync worker | `2` |
| `RATE_LIMIT_LOGIN_IP`, `RATE_LIMIT_LOGIN_FAILURES`, `RATE_LIMIT_SIGNUP_IP`, `RATE_LIMIT_QUERY`, `RATE_LIMIT_INGEST_LINK`, `RATE_LIMIT_FIREFLIES_CONNECT`, `RATE_LIMIT_RETRY` | backend | see 23.3 |
| `FORWARDED_ALLOW_IPS` | backend | the frontend's address (compose: the `frontend` service) |
| `QUERY_MAX_CHARS`, `HISTORY_MAX_MESSAGES`, `HISTORY_MESSAGE_MAX_CHARS` | backend, engine | `2000`, `10`, `4000` |
| `MAX_REQUEST_BODY_BYTES` | backend | `1048576` |
| `LLM_CHAT_MAX_CONCURRENCY`, `LLM_INGEST_MAX_CONCURRENCY`, `EMBED_CHAT_MAX_CONCURRENCY`, `EMBED_INGEST_MAX_CONCURRENCY` | engine | `16`, `8`, `8`, `4` |
| `LLM_MAX_RETRIES`, `LLM_TIMEOUT_SECONDS` | engine | `3`, `60` |
| `HNSW_EF_SEARCH`, `HNSW_MAX_SCAN_TUPLES` | engine | `100`, `20000` |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `DB_POOL_RECYCLE` | backend, workers, engine | see 23.6 |
| `SAVE_RAW_TRANSCRIPTS`, `RAW_TRANSCRIPTS_DIR` | backend, ingest worker | `false`, `transcripts` |
| `CORS_ORIGINS` | backend | empty (was `*`) |

Like every other setting, these are read at startup: change `.env` and restart the service.

### 23.12 Testing

- **Versioning tests:** `.gitignore` excludes `tests/`, `*/tests/`, `test_*.py` and `*_test.py`, so no test is versioned or can run in CI. **Recommended:** remove those four lines. This changes a repository policy, so it is applied only with your explicit OK.
- **Stage 0:**
  - rate limits: 429 with `Retry-After`, the per-email failure throttle, fail-open behaviour;
  - size limits (413 and 422);
  - job de-duplication: a second enqueue does nothing;
  - Fireflies error classification against a fixture for every documented code, plus a live call with an invalid key;
  - the timestamp migration: values unchanged as instants;
  - grouped turn counts equal the old counts;
  - the retrieval acceptance tests (23.5);
  - no CORS headers;
  - revoked tokens get 401.
- **Stages 1-2:**
  - sync paging with `to_date` and the `skip` rule;
  - reconciliation only after a completed full sync;
  - stale-heartbeat recovery;
  - the busy path of the Redis lock;
  - retry spacing and the 7-day window;
  - purge batches;
  - disconnect, then reconnect with the same and with a different account.
- **Stage 3:** a project change updates the turn metadata; the retry endpoint's 409s.
- **Stage 4:** a manual checklist of the UI flows in section 14.
- **Stage 5:** the spike (22.5), then refresh, logout, idle timeout and revocation.
- **Load test before release:**
  - 10 concurrent chat users while 2 syncs run;
  - record 95th-percentile latency, error rate and the number of Gemini 429s;
  - acceptance: 95th-percentile chat answer under 10 s and an error rate under 1%.

### 23.13 Sources (read 2026-11-27)

- Fireflies: [Errors](https://docs.fireflies.ai/fundamentals/errors), [Error codes](https://docs.fireflies.ai/miscellaneous/error-codes), [Limits](https://docs.fireflies.ai/fundamentals/limits).
- pgvector filtered search and iterative scans: [AWS: pgvector 0.8.0 on Aurora PostgreSQL](https://aws.amazon.com/blogs/database/supercharging-vector-search-performance-and-relevance-with-pgvector-0-8-0-on-amazon-aurora-postgresql), [Crunchy Data: hybrid search with pgvector](https://crunchydata.com/blog/hybrid-vector-search), [Render: multi-tenant RAG with pgvector](https://render.com/articles/multi-tenant-rag-postgresql-pgvector), [Filtered ANN search in vector databases (arXiv 2602.11443)](https://arxiv.org/html/2602.11443v1).
- OWASP: [Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html), [Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html), [WSTG: Testing Session Timeout](https://owasp.org/www-project-web-security-testing-guide/v42/4-Web_Application_Security_Testing/06-Session_Management_Testing/07-Testing_Session_Timeout), [Insufficient Session Expiration (20 min idle / 8 h example)](https://owasp.org/index.php/OWASP_Periodic_Table_of_Vulnerabilities_-_Insufficient_Session_Expiration).
- Job queues: [Celery vs RQ vs Dramatiq vs arq, 2026](https://dev.to/datanestdigital/background-jobs-in-python-celery-vs-rq-vs-dramatiq-vs-arq-2026-decision-guide-37m6), [Encore: Celery alternatives](https://encore.dev/articles/celery-alternatives); PyPI release dates checked directly: arq 0.28.0 (2026-04-16), taskiq 0.13.0 (2026-09-26), redis 8.2.0 (2026-11-23).
- Rate limiting: [slowapi](https://docsearch.algolia.com/mcp/docs/repo/laurents/slowapi), [How to rate limit FastAPI in 2026](https://dev.to/zlynv/how-to-rate-limit-fastapi-in-2026-1kbl); PyPI: slowapi 0.1.10 (2026-06-13), limits 5.8.0 (2026-02-05).
