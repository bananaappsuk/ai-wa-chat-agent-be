import sys

from rq import Queue, SimpleWorker, Worker
from app.workers.queue import get_redis

# macOS Objective-C runtime crashes after fork() once any networking lib has
# initialized. SimpleWorker runs jobs in the main process and avoids fork.
# Linux/Docker uses the standard forking Worker for isolation.
WorkerCls = SimpleWorker if sys.platform == "darwin" else Worker


if __name__ == "__main__":
    conn = get_redis()
    # job_monitoring_interval raised from the 30s default to cut idle heartbeat
    # commands (matters on Upstash's metered free tier).
    w = WorkerCls(
        [Queue("default", connection=conn)],
        connection=conn,
        job_monitoring_interval=90,
    )
    # with_scheduler=False: no scheduled jobs exist (nothing uses enqueue_at/enqueue_in),
    # and the RQ scheduler polls Redis continuously — pure idle command burn. Re-enable
    # only if/when scheduled campaigns are implemented.
    w.work(with_scheduler=False)
