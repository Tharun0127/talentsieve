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
