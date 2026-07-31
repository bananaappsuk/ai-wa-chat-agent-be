#!/usr/bin/env sh
# RQ worker entrypoint (same image as API, different command).
set -eu
exec python worker.py
