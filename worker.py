import sys

from rq import Queue, SimpleWorker, Worker
from app.workers.queue import get_redis

# macOS Objective-C runtime crashes after fork() once any networking lib has
# initialized. SimpleWorker runs jobs in the main process and avoids fork.
# Linux/Docker uses the standard forking Worker for isolation.
WorkerCls = SimpleWorker if sys.platform == "darwin" else Worker


if __name__ == "__main__":
    conn = get_redis()
    w = WorkerCls([Queue("default", connection=conn)], connection=conn)
    w.work(with_scheduler=True)
