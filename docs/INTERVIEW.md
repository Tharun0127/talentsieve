# Interview questions: background jobs and queues, answered with TalentSieve

Each answer is written the way you would say it out loud.

---

### 1. How do you handle long-running tasks in a web app?

"You don't do them inside the request. In TalentSieve, scoring one resume means parsing a PDF plus an LLM call that can take 20 seconds, and a recruiter uploads 50 at once. Done inside the request, that would run for minutes, time out at the proxy, and lose all its work if the connection dropped.

So the upload endpoint only saves the files, inserts rows and puts one job per resume on a queue. It answers in about 50 milliseconds with a batch id. Workers do the slow part in the background, and the browser watches progress over Server-Sent Events."

### 2. What is a message queue, in simple terms?

"A to-do list that lives outside your app, shared between the code that creates work and the code that does it. The API writes 'score resume 42' onto the list; any free worker takes it, does it, and crosses it off. Producers and consumers don't need to know about each other or run at the same speed. If 50 jobs arrive at once, they wait in line. Mine is ARQ, which keeps the list in a Redis sorted set."

### 3. Why one job per resume instead of one job per batch?

"Three reasons. Parallelism: four workers can work on one batch at once, and my benchmark shows 50 resumes going from 75 seconds with one worker to 19.5 with four. Isolation: one corrupt PDF fails on its own and goes to the dead-letter queue, while the other 49 finish. And cheap retries: a timeout retries one resume, not the whole batch."

### 4. What happens if a worker crashes in the middle of a job?

"Nothing is lost, and a test proves it by sending SIGKILL to a real worker process mid-job. In ARQ, starting a job doesn't remove it from the queue. The worker sets an 'in progress' key that expires after the job timeout plus 10 seconds, and the job is only removed when the function finishes.

So when a worker dies, the job is still in the queue. Once that key expires, another worker picks it up. In my test, with a 5-second timeout, the second worker finished the job about 18 seconds after the kill. There was exactly one result, and the attempt count said 2."

### 5. So a job can run twice. How do you make that safe?

"That's at-least-once delivery, and the answer is idempotency: running the job twice has the same effect as running it once. Four things:
- `results.resume_id` is unique and I write with an upsert, so a second run overwrites the same row.
- If the resume is already done, the job returns at once without calling the LLM.
- Progress counters move through a Lua script that does nothing if the status hasn't changed.
- Job ids are deterministic, so enqueueing twice gives one job.

The nastiest case is tested too: the worker saves the result, then crashes before marking the job done."

### 6. How do your retries work?

"Every error is one of two kinds, and the split is explicit in `errors.py`. Transient errors might work later: timeouts, 429 rate limits, 5xx errors, or the model returning broken JSON. Those retry with exponential backoff and full jitter, up to five attempts. Permanent errors fail the same way every time: a corrupt PDF, a scanned PDF with no text, a 400 from the API. Those don't retry at all. In my test, a corrupt PDF is dead-lettered after one attempt with zero LLM calls."

### 7. Why exponential backoff, and why jitter?

"Exponential, because if a service is struggling, retrying every second makes it worse. Waiting 2, then 4, then 8 seconds gives it room. Jitter, because errors come in groups: if 50 jobs hit a rate limit together and all wait exactly 4 seconds, they come back together and hit it again. So each waits a random time between zero and the cap. In my 30% failure benchmark, every one of the 50 resumes still succeeded, using 81 LLM calls instead of 50."

### 8. What is a dead-letter queue?

"It's where jobs go when they won't succeed on their own, so they don't retry forever or vanish silently. Mine has two parts: the resume is marked 'dead' in Postgres with the reason, and its id goes into a Redis list. The Failed jobs page shows the reason with Retry and Retry all. Retrying gives the job a fresh attempt budget and a new job id, which is useful after you fix the cause, for example after an LLM outage."

### 9. At-most-once, at-least-once, exactly-once: what's the difference?

"At-most-once: remove the job before running it. Crash, and it's gone. At-least-once: remove it only after it's done. Crash, and it runs again. True exactly-once across two systems like Redis and Postgres would need them to commit together, which they can't. So in practice you choose at-least-once and make the work idempotent, which gets you the effect of exactly-once. That's what I did."

### 10. Why ARQ and not Celery?

"My whole stack is async: FastAPI, async SQLAlchemy, async Redis. LLM calls are I/O-bound, and ARQ runs on asyncio, so one worker process runs four LLM calls at once at almost no cost. My benchmark shows one worker with four concurrent jobs is as fast as four separate workers. Celery is mainly process- or thread-based, with many more features: workflows, multiple brokers, scheduling, monitoring. I didn't need those, and ARQ is small enough that I read its source to see exactly how crashes behave. With complex workflows or a team already running Celery, I'd choose Celery."

### 11. How do you stop many workers from blowing through the LLM's rate limit?

"The limit has to be global, because each worker is a separate process. I keep a sliding window in a Redis sorted set: one timestamped entry per call. A Lua script atomically drops entries older than a minute, checks the count, and either adds ours or says how long to wait. It uses Redis's own clock, so workers with skewed clocks still agree. With four workers, 16 free slots and a 30-per-minute limit, 50 resumes took 63 seconds: 30 immediately, 20 a minute later. Exactly the contract."

### 12. How do you show progress to the user?

"Each status change moves the resume between counters in Redis (queued, processing, done or dead) with a Lua script, so it's atomic and can't double-count. The browser opens a Server-Sent Events stream, and the server sends the counts whenever they change, ending with a 'complete' event. I chose SSE over WebSockets because progress flows one way, it's plain HTTP, and the browser's EventSource reconnects by itself."

### 13. How do you control LLM costs?

"Caching first. The key is a hash of the prompt version, the provider, the resume text and the job description. Recruiters often re-upload the same CVs, and a cached answer costs nothing: no LLM call and no rate-limit slot, which my tests check. The prompt version is in the key, so changing the prompt invalidates old answers. The cache is in Postgres rather than Redis, because it saves money and should survive a Redis restart. Beyond that: one call per resume, low effort for what is basically classification, and no retries on permanent errors."

### 14. How do you make sure the LLM's output is usable?

"I never trust it directly. I ask for strict JSON, and with the real API I use structured outputs so the response follows my schema. Then a Pydantic model validates it: score 0 to 100, exactly three strengths and three gaps, and a verdict. Prose, a fourth strength or a score of 140 is a validation error, which I treat as transient, because asking again usually works. Nothing invalid reaches the database."

### 15. When would you replace Redis with RabbitMQ or Kafka?

"Redis plus ARQ is right at this size. I'd move to RabbitMQ for stronger delivery guarantees across broker crashes, for routing by priority or tenant, or for a built-in dead-letter exchange. I'd move to Kafka if many services needed the same events, if I wanted to replay history (re-score every resume with a new prompt), or for very high throughput with ordering per key. Two things wouldn't change: consumers must still be idempotent, because those systems are at-least-once too, and you still need the transient-versus-permanent split with a dead-letter queue."
