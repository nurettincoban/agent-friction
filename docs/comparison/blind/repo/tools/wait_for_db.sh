#!/bin/sh
for i in $(seq 1 60); do
  pg_isready -h localhost -p 5433 -U tide >/dev/null 2>&1 && exit 0
  sleep 1
done
echo "timescale did not become ready" >&2
exit 1
