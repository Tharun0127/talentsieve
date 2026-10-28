#!/bin/sh
set -e

# One image, two roles: the API (accepts work) and the worker (does it).
if [ "${ROLE:-api}" = "worker" ]; then
  echo "[entrypoint] starting ARQ worker"
  exec arq app.worker.WorkerSettings
fi

echo "[entrypoint] running migrations and seed"
alembic upgrade head
python -m scripts.seed

echo "[entrypoint] starting API"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2 --no-access-log
