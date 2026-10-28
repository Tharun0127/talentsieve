# Decisions

Every important choice in TalentSieve, the alternatives, and why.

## Shape of the system

- **One image, two roles** (`ROLE=api` or `ROLE=worker`). The API accepts uploads and enqueues; workers parse PDFs and call the LLM. The `worker` service has no `container_name` and no ports, so `docker compose up --scale worker=N` works.
- **Uploads go to a shared volume** (`uploads`) mounted by the api and every worker. In production this would be object storage (S3) with the job carrying the object key.
- **The API validates only what it can check cheaply** (count ≤ 50, size ≤ 5 MB, `.pdf` name, non-empty). Content checks belong to the worker: a corrupt PDF is accepted at upload and ends up in the dead-letter queue as a *permanent* failure, which is what would happen in production anyway.
- **Postgres is the source of truth** for every resume's status and result. Redis holds the queue, the progress counters, the DLQ list and the rate limiter.
- **Redis runs with AOF persistence** here (unlike a pure cache): the job queue lives in Redis, so a Redis restart must not drop queued jobs.

## Auth

- JWT bearer tokens for recruiters; bcrypt in a thread. The SSE progress stream takes the token in the query string because the browser's `EventSource` cannot send headers.
- Batches are private: every batch query filters by the owner, and unknown or foreign batches return 404 (not 403, which would reveal that the id exists).

## Why a queue at all (and not work in the request)

- Scoring one resume means parsing a PDF and a 2-20 s LLM call. 50 resumes in one HTTP request would take minutes: the request would time out at the proxy, the browser would show nothing, and a dropped connection would waste all the work done so far.
- With a queue, the upload returns **202 with a batch id** in milliseconds. One job per resume means a slow or broken CV never holds up the others, failures are retried individually, and throughput scales by adding workers instead of making requests longer.
- **The API never calls the LLM.** It saves files, inserts rows and enqueues. That keeps API latency flat and makes the LLM rate limit a worker-side concern only.

## Why ARQ (not Celery) for this project

| | ARQ | Celery |
|---|---|---|
| Model | Native `asyncio`; one worker process runs many jobs concurrently | Prefork processes or threads; asyncio support is limited |
| Fit for LLM calls | Excellent: I/O-bound, so 4+ concurrent jobs per process at almost no cost | Needs many processes or gevent to get the same concurrency |
| Broker | Redis only (already in the stack) | Redis, RabbitMQ, SQS... |
| Features | Retries via `Retry(defer=...)`, job ids for de-duplication, timeouts | Much more: canvas/chords, routing, rate limits, beat, flower |
| Size | ~2k lines; easy to read the source to understand the exact semantics | Large; behaviour depends on broker and configuration |

The whole codebase (FastAPI, SQLAlchemy async, redis.asyncio) is async, the work is I/O-bound, and we need only simple fan-out with retries. That is exactly ARQ's niche. Celery would win for complex workflows, multiple brokers, or a team that already runs it.

## Jobs and job ids

- **Deterministic job id** `score:<resume_id>:<generation>`. ARQ refuses to enqueue an id that already exists, so a double-submitted request cannot create two jobs for one resume. `generation` changes only when the DLQ retry endpoint deliberately re-runs a dead job.
- **The job payload is just the resume id.** The worker loads everything else from Postgres, so a job never carries stale data, and the queue entry stays tiny.
- **`max_tries` in ARQ = our limit + 1.** ARQ fails a job *without calling the function* once `job_try > max_tries`. Our code needs to see the final attempt to route it to the DLQ with a reason.

## LLM output

- Providers return raw text. We parse it ourselves and validate with Pydantic (`score` 0-100, exactly 3 strengths and 3 gaps, a verdict). A model that rambles, adds a 4th strength or scores 140 is rejected **before** anything reaches the database. A ```json fence is tolerated, because that is the most common harmless deviation.
- Invalid output counts as **transient**: models are non-deterministic, so asking again usually works.
- **MockLLM** (the default without a key) has random latency and transient failures at a configurable rate, and answers in the same JSON format. Its score is keyword overlap, so the ranking in the demo is meaningful rather than random.

## Progress counters that cannot drift

- `progress:<batch>:status` (resume → status) and `progress:<batch>:counts` (status → n), changed **only** by one Lua script that moves a resume from its old status to its new one. Repeating a move is a no-op, so a job that runs twice cannot double-count. Plain `HINCRBY` counters would drift in exactly the crash/retry cases this project is about.

## Retries: transient vs permanent, made explicit

- `app/errors.py` defines two base classes. Every failure is one or the other:
  - **TransientError:** LLM timeout, 429, 5xx, invalid JSON from the model. Retried.
  - **PermanentError:** corrupt or encrypted PDF, no extractable text, resume deleted, 400/401 from the provider. **Not** retried.
- **Unknown exceptions are treated as transient.** A retry costs a little; wrongly dead-lettering a recoverable job loses a candidate. The error message says `unexpected` so bugs stand out in the DLQ.
- **Up to 5 attempts, exponential backoff with full jitter:** `random(0, min(60 s, 2 s × 2^(attempt-1)))`. Exponential gives a struggling API room; *full* jitter keeps 50 jobs that failed together from retrying together and failing together again.
- **Mechanism:** ARQ's `Retry(defer=delay)` puts the **same** job back in the queue with a future run time. The resume shows `queued` with the last error while it waits.
- **Proven in tests:** a provider that fails twice then works ends `done` after 3 attempts; one that always fails is dead-lettered after exactly 5 LLM calls; a corrupt PDF is dead-lettered after 1 attempt with **0** LLM calls.

## Dead-letter queue

- **Two records:** Postgres (`status = 'dead'`, `error` = reason, `attempts`) is the durable, queryable truth shown in the UI. A Redis list `<queue>:dlq` of resume ids, newest first, is the operational view.
- **The list holds ids, not JSON blobs,** so `LREM` + `LPUSH` makes dead-lettering idempotent (a duplicate run cannot add a second entry), and details are never out of sync with the database.
- **Retry endpoint** (`POST /admin/dlq/{id}/retry`, `POST /admin/dlq/retry-all`): resets the attempt budget, bumps `generation` (so ARQ gets a new job id; the old id may still have a stored result), removes the id from the list and enqueues. The `UPDATE ... WHERE status = 'dead' RETURNING` makes a double click a no-op.
- **Scoped per recruiter:** you see and retry only your own batches' jobs.

## At-least-once delivery, and why idempotency is required

- A queue can give you **at-most-once** (remove the job before running it; a crash loses it) or **at-least-once** (remove it only after it finished; a crash runs it again). "Exactly once" between two systems (Redis and Postgres) is not available without distributed transactions, so you pick at-least-once and make the work **idempotent**: running it twice has the same effect as running it once.
- ARQ is at-least-once: the job leaves the queue only in `finish_job`, after the function returns.
- **How TalentSieve makes a repeated job harmless:**
  1. `results.resume_id` is `UNIQUE`, and the worker writes with `INSERT ... ON CONFLICT (resume_id) DO UPDATE`. A second run overwrites the same row.
  2. A job whose resume is already `done` with a result returns `already_done` without calling the LLM, so a duplicate does not cost money.
  3. Progress counters move through a Lua script that is a no-op when the status is unchanged, so counts never double.
  4. Deterministic job ids make a double enqueue a no-op, and the DLQ list cannot hold the same id twice.
- **Tests:** a run that crashes after saving the result but before marking it done, then runs again (1 result, counts correct); a duplicate delivery of a finished job (no second LLM call); 5 concurrent runs of the same job (1 result); a duplicate enqueue (1 job).

## Worker crash safety (how it works in ARQ)

Read from ARQ 0.26's source (`arq/worker.py`), and demonstrated by a test that `SIGKILL`s a real worker process:

1. A job is a member of a Redis **sorted set** (the queue), scored by when it should run. Enqueueing never removes it.
2. To start a job, a worker atomically sets `arq:in-progress:<job_id>` with an expiry of `job_timeout + 10 s` (`in_progress_timeout_s`). Other workers skip jobs whose in-progress key exists.
3. `arq:retry:<job_id>` is incremented on every start; that number is `ctx["job_try"]`, so a crash counts as an attempt.
4. Only when the function returns (or raises) does `finish_job` remove the job from the sorted set and delete the in-progress key.
5. **If the worker dies mid-job**, step 4 never happens. The job is still in the queue; when the in-progress key expires, any worker claims it and runs it again (with `job_try + 1`).

**Measured:** with `job_timeout = 5 s`, the job was re-run by a second worker and finished **18.6 s** after the first was killed (15 s lock + the 3 s mock LLM call). It finished with `attempts = 2` and exactly one result. With the production setting (`job_timeout = 120 s`), recovery takes about 130 s.

**Trade-off:** a shorter `job_timeout` means faster recovery, but jobs legitimately slower than the timeout get cancelled and retried. 120 s is roughly 6× a slow LLM call.

**Graceful shutdown** (`SIGTERM`, for example a deploy) is different: ARQ cancels running jobs and they are retried, so nothing waits for the lock to expire.

## Progress streaming: Server-Sent Events

- `GET /batches/{id}/events?token=...` streams the batch's counts (`queued`, `processing`, `done`, `dead`, `total`) whenever they change, and ends with `event: complete`.
- **Why SSE, not WebSockets:** progress flows one way (server → browser). SSE is plain HTTP that works through any proxy, and the browser's `EventSource` reconnects on its own. WebSockets would add a bidirectional protocol we do not need. (The sibling project Pulsewire uses WebSockets because chat is two-way.)
- **Polling Redis every 500 ms inside the stream** instead of pub/sub: one `HGETALL` per open progress page is trivial, it cannot miss an update (it always reads the current state), and the stream needs no per-connection subscription bookkeeping.
- A `: keep-alive` comment every 15 s keeps idle-connection timeouts in proxies from closing a quiet stream; `X-Accel-Buffering: no` stops nginx-style proxies from buffering it.
- The token is in the query string because `EventSource` cannot set headers.

## Global LLM rate limit

- **A sliding-window log in a Redis sorted set**, checked and updated in one Lua script. Every worker process shares it, so 4 workers together still make at most `LLM_RATE_LIMIT_PER_MINUTE` calls. A per-process limiter would allow 4×.
- **Redis's own clock (`TIME`)**, so workers on machines with skewed clocks still agree on the window.
- **Sliding, not fixed, window:** a fixed "per calendar minute" counter lets 2× the limit through around the minute boundary.
- **Waiting, not failing:** a worker that gets no slot sleeps until the oldest call leaves the window (plus jitter) and tries again. Nothing is dead-lettered for being throttled locally. A 429 from the real provider is still a transient error and goes through the retry path.
- **Test:** 4 independent Redis connections × 5 jobs with a limit of 5 per second: no 1-second window ever contains more than 5 grants, and the 20 calls take at least 3 seconds.

## Cost control: LLM result cache

- Key = `sha256(prompt version, provider, normalised resume text, normalised job description)`. A re-uploaded CV for the same role is answered from Postgres: **no LLM call and no rate-limit slot used** (both tested).
- **Prompt version and provider are part of the key:** change the prompt or the model and old answers are no longer reused.
- **Stored in Postgres, not Redis:** it saves money, so it must survive a Redis restart. `hits` counts reuse for reporting.
- A result served from the cache is still saved per resume (with `cached = true`) and shown like any other.

## Real LLM provider (Anthropic)

- **Official `anthropic` SDK** (`AsyncAnthropic`), selected automatically when `ANTHROPIC_API_KEY` is set. Without a key the project runs entirely on MockLLM. Keys live only in `.env` (git-ignored); `.env.example` documents the variables.
- **SDK retries disabled (`max_retries=0`).** The queue already retries with backoff, counts attempts, respects the global rate limit and dead-letters. A second, hidden retry layer inside the SDK would multiply calls and make attempt counts lie.
- **Structured outputs** (`output_config.format` with a JSON schema) make the API return JSON in our exact shape; Pydantic still validates it (exactly 3 strengths and gaps, score 0-100).
- **Model `claude-opus-5-5` by default with effort `low`.** Scoring one resume is close to classification, so low effort keeps cost and latency down. Both are environment variables (`ANTHROPIC_MODEL`, `ANTHROPIC_EFFORT`) if a team wants a cheaper model or deeper reasoning.
- **Server-side refusal fallbacks on** (`fallbacks: "default"`, toggle `ANTHROPIC_FALLBACKS`). If the model declines and the fallback also declines, it is a **permanent** error, because asking again would decline again.
- **Error policy in one function** (`classify_status`): 429 → rate limited, 5xx including 529 overloaded → server error, 408 → timeout (all transient); 400/401/403/404/422 → permanent. Connection errors and timeouts are transient; a `max_tokens` cut-off or missing text counts as bad output (transient).

## Frontend

- Upload form (title, job description, up to 50 PDFs). After upload it navigates to the batch page immediately, because the API already returned.
- **Live progress bar via `EventSource`** with per-status counts; the table refreshes as results land, ranked by score, with expandable strengths and gaps and a "from cache" marker.
- **Failed-jobs page** = the DLQ, with each job's reason, Retry per job and Retry all. Failed rows on a batch page also have a Retry button.
- The nginx proxy turns off response buffering for the API (`proxy_buffering off`), otherwise SSE events arrive in bursts instead of live, and allows ~260 MB request bodies (50 × 5 MB).

## When we would move to RabbitMQ or Kafka

ARQ on Redis is right for **this** workload: thousands of independent, I/O-bound jobs a day, one team, Redis already running. We would move when one of these becomes true:

| Need | Why Redis + ARQ falls short | Move to |
|---|---|---|
| Strong delivery guarantees across broker failures | One Redis node (even with AOF) can lose the last second of writes on a crash; failover needs Sentinel/Cluster | **RabbitMQ** (quorum queues, publisher confirms, per-message acks) |
| Complex routing (by type, priority, tenant), many consumer types | ARQ has one sorted set per queue name; no exchanges or routing keys | **RabbitMQ** (exchanges, bindings, priority queues, built-in dead-letter exchange) |
| Many services consuming the same events, and replaying history ("re-score every resume with the new prompt") | A job is deleted once done; there is no log to replay | **Kafka** (durable partitioned log, consumer groups, replay by offset, retention for days) |
| Very high throughput (100k+ messages/s) or strict ordering per key | Redis is single-threaded per shard; ARQ polls | **Kafka** (partitioned by key, e.g. batch id, for ordering) |
| Workers in several languages | ARQ is Python-only | RabbitMQ or Kafka (clients everywhere) |

Two things stay the same after any such move: **idempotent consumers** (RabbitMQ and Kafka are at-least-once in practice too) and an **explicit transient/permanent split with a dead-letter queue** (RabbitMQ's dead-letter exchange, or a Kafka retry/DLQ topic pattern).
