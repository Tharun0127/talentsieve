# TalentSieve benchmarks

Setup: Windows 11 laptop, Docker Desktop. Workers scaled with
`docker compose up --scale worker=N`. MockLLM with a **fixed 1.0 s latency**
per call, so the numbers measure the queue, not LLM variance. Every run
uploads the same 50 generated resumes with a **unique job description**, so
the LLM cache cannot answer from earlier runs.

```bash
sh backend/benchmarks/run_benchmarks.sh      # all scenarios below
```

Raw results: [`benchmarks.jsonl`](../backend/benchmarks/results/benchmarks.jsonl).
"Total time" = from the upload request until every job is done or dead.

## 1. Scaling workers (50 resumes, 1.0 s per LLM call, no failures)

| Workers | Jobs per worker at once | Total time | Throughput | Upload response |
|---|---|---|---|---|
| 1 | 1 | 75.2 s | 40 resumes/min | 76 ms |
| 2 | 1 | 37.7 s | 80 resumes/min | 43 ms |
| 4 | 1 | **19.5 s** | 154 resumes/min | 51 ms |
| 1 | 4 | 19.5 s | 154 resumes/min | 47 ms |
| 2 | 4 | 10.2 s | 293 resumes/min | 40 ms |
| 4 | 4 | **5.8 s** | **520 resumes/min** | 62 ms |

- **Linear scaling:** doubling the workers halves the time (75 → 38 → 19.5 s). Jobs are independent, so there is no shared bottleneck at this size.
- **Concurrency inside a worker is as good as more workers** for I/O-bound LLM calls: 1 worker running 4 jobs at once equals 4 workers running 1 each (19.5 s both). That is ARQ's asyncio model paying off: one process waits on four HTTP calls at the same time.
- **The 1 s floor:** 50 calls at 1 s each across 16 concurrent slots is about 4 s of pure LLM time; 5.8 s total means ~2 s of overhead (PDF parsing, DB writes, queue polling).
- **The API response never changes:** 40-80 ms to accept 50 PDFs, whatever the workers are doing. That is the point of the queue.

## 2. Failures: 30% of LLM calls fail (timeouts, 429s, 503s)

| Metric | Result |
|---|---|
| Resumes | 50 |
| **Final success rate** | **100% (50 / 50)** |
| Dead-lettered | 0 |
| Attempts needed | 1 attempt: 31 · 2: 10 · 3: 6 · 4: 3 |
| LLM calls made | 81 (62% more than the 50 needed without failures) |
| Total time (4 workers × 4) | 15.7 s (vs 5.8 s with no failures) |

- With a 30% failure rate and 5 attempts, a job is dead-lettered only if all five fail: 0.3⁵ = **0.24%**. The expected success rate is 99.76%; this run had no job needing a 5th attempt.
- The extra time comes from backoff: retries wait `random(0, 2^attempt s)`, which is the price of not hammering a struggling provider.
- Permanent errors are a different story: a corrupt PDF goes to the DLQ after **1** attempt with **0** LLM calls (tested in `tests/test_retries_dlq.py`).

## 3. Global rate limit: 30 LLM calls per minute across 4 workers

| Metric | Result |
|---|---|
| Workers × concurrency | 4 × 4 (16 slots that *could* run at once) |
| Limit | 30 calls / minute, shared through Redis |
| Total time | **63.1 s** for 50 resumes |

Without a limit the same setup takes 5.8 s. With it, the first 30 calls go through at once, and the remaining 20 wait for the sliding window to free slots ~60 s later: **exactly what a 30/min contract with a provider requires**, even though 16 slots were free. A per-process limiter would have allowed 4 × 30 = 120 calls per minute.

## 4. Crash recovery

From `tests/test_idempotency_crash.py` (a real `SIGKILL` of a worker process mid-job):

| Setting | Recovery after the kill |
|---|---|
| `job_timeout = 5 s` (test) | **18.6 s** (15 s in-progress lock + 3 s mock LLM call), attempts = 2, 1 result |
| `job_timeout = 120 s` (production default) | ~130 s (by the same formula) |
