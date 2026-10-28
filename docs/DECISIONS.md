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
