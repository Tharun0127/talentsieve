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
