#!/usr/bin/env sh
# Campaign due-scheduler with Redis lock (run exactly one replica).
set -eu
exec python scheduler.py
