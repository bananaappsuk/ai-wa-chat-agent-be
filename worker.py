import os
import sys
import logging

from rq import Queue, SimpleWorker, Worker
from rq.timeouts import TimerDeathPenalty

from app.config import settings
from app.observability.logging_setup import configure_logging
from app.observability.sentry import init_sentry, capture_exception
from app.workers.queue import get_redis

# Forking Worker uses os.fork(), which is unavailable on Windows and unsafe on
# macOS after networking libs initialize. SimpleWorker runs jobs in-process.
# Linux/Docker/Render keep the standard forking Worker for job isolation.
#
# On Windows, Unix signal-based job timeouts (SIGALRM) are also unavailable, so
# SimpleWorker must use TimerDeathPenalty instead of UnixSignalDeathPenalty.


class WindowsSimpleWorker(SimpleWorker):
    """SimpleWorker with thread-based job timeouts for Windows."""

    death_penalty_class = TimerDeathPenalty


if os.name == "nt":
    WorkerCls = WindowsSimpleWorker
elif sys.platform == "darwin":
    WorkerCls = SimpleWorker
else:
    WorkerCls = Worker


def _listen_queues(conn) -> list[Queue]:
    """
    Listen to priority queues. Override with RQ_LISTEN_QUEUES=high,default,bulk
    Local/dev: single worker can listen to all three.
    Production: run separate workers per queue for isolation.
    """
    raw = (os.environ.get("RQ_LISTEN_QUEUES") or "").strip()
    if raw:
        names = [n.strip() for n in raw.split(",") if n.strip()]
    else:
        names = [
            settings.RQ_HIGH_QUEUE_NAME,
            settings.RQ_DEFAULT_QUEUE_NAME or settings.RQ_QUEUE_NAME or "default",
            settings.RQ_BULK_QUEUE_NAME,
        ]
        # Deduplicate while preserving order
        seen = set()
        names = [n for n in names if not (n in seen or seen.add(n))]
    return [Queue(n, connection=conn) for n in names]


if __name__ == "__main__":
    configure_logging()
    init_sentry(service="worker")
    if settings.is_production_like:
        settings.validate_for_startup()
    logger = logging.getLogger("app.worker")
    try:
        conn = get_redis()
        queues = _listen_queues(conn)
        names = [q.name for q in queues]
        # job_monitoring_interval raised from the 30s default to cut idle heartbeat
        # commands (matters on Upstash's metered free tier).
        w = WorkerCls(queues, connection=conn, job_monitoring_interval=90)
        logger.info("RQ worker listening queues=%s", ",".join(names))
        # with_scheduler=True is required: retries and campaign batching enqueue jobs
        # with enqueue_in (RQ scheduled registry). Do not disable.
        w.work(with_scheduler=True)
    except Exception as exc:
        capture_exception(exc, service="worker")
        raise
