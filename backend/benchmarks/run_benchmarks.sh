#!/bin/sh
# Run every benchmark from the HOST, in the project root:
#     sh backend/benchmarks/run_benchmarks.sh
# Between runs it re-creates the workers with a new count and MockLLM
# settings (env vars are read by docker-compose.yml).
set -e

RESULTS=backend/benchmarks/results
mkdir -p "$RESULTS"
rm -f "$RESULTS/benchmarks.jsonl"

run() {
  label=$1; workers=$2
  # Remaining variables (MOCK_LLM_*, WORKER_CONCURRENCY, LLM_RATE_LIMIT_PER_MINUTE)
  # come from the environment of this call.
  docker compose up -d --no-deps --force-recreate --scale worker="$workers" worker >/dev/null 2>&1
  sleep 4  # let the workers connect to Redis
  docker compose exec -T api python -m benchmarks.measure --label "$label" --count 50
}

# 1) Scaling: fixed 1.0 s LLM latency, no failures, no rate limit pressure.
export MOCK_LLM_MIN_DELAY=1.0 MOCK_LLM_MAX_DELAY=1.0 MOCK_LLM_FAILURE_RATE=0 LLM_RATE_LIMIT_PER_MINUTE=100000
for c in 1 4; do
  for w in 1 2 4; do
    WORKER_CONCURRENCY=$c run "workers=$w concurrency=$c" "$w"
  done
done

# 2) Failures: 30% of LLM calls fail with timeouts / 429 / 503.
WORKER_CONCURRENCY=4 MOCK_LLM_FAILURE_RATE=0.3 run "30% failure rate, workers=4 concurrency=4" 4

# 3) Global rate limit: 4 workers, but only 30 LLM calls per minute in total.
# Clear the shared window first, or calls from the previous run count against it.
docker compose exec -T redis redis-cli DEL llm:ratelimit >/dev/null
WORKER_CONCURRENCY=4 LLM_RATE_LIMIT_PER_MINUTE=30 run "rate limit 30/min, workers=4 concurrency=4" 4

docker compose cp api:/app/benchmarks/results/benchmarks.jsonl "$RESULTS/" >/dev/null

# Back to the defaults from docker-compose.yml / .env.
unset MOCK_LLM_MIN_DELAY MOCK_LLM_MAX_DELAY MOCK_LLM_FAILURE_RATE LLM_RATE_LIMIT_PER_MINUTE
docker compose up -d --no-deps --force-recreate --scale worker=1 worker >/dev/null 2>&1
