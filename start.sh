#!/bin/sh
# 1) first-run setup from env vars (no-op after that)
# 2) background loop: stats sync, waivers, lineup locks, weekly scoring
# 3) web server
set -e
mkdir -p /data 2>/dev/null || true
python -m app.bootstrap
# Set RUN_JOBS=0 when a scheduled GitHub Action runs the jobs instead.
if [ "${RUN_JOBS:-1}" = "1" ]; then
  python -m app.jobs loop --minutes "${JOB_MINUTES:-20}" &
fi
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips="*"
