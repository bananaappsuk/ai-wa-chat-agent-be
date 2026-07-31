from redis import Redis
from rq import Queue
import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

_redis: Redis | None = None
_queues: dict[str, Queue] = {}


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            settings.REDIS_URL,
            health_check_interval=60,
            socket_keepalive=True,
            retry_on_timeout=True,
            socket_connect_timeout=max(1, int(settings.REDIS_CONNECT_TIMEOUT_SECONDS)),
            socket_timeout=max(1, int(settings.REDIS_SOCKET_TIMEOUT_SECONDS)),
        )
        logger.info("Redis client created")
    return _redis


def get_queue(name: Optional[str] = None) -> Queue:
    qname = (name or settings.RQ_DEFAULT_QUEUE_NAME or settings.RQ_QUEUE_NAME or "default").strip()
    if qname not in _queues:
        _queues[qname] = Queue(
            qname,
            connection=get_redis(),
            default_timeout=max(30, int(settings.WORKER_JOB_TIMEOUT)),
        )
    return _queues[qname]


def enqueue(func, *args, queue: Optional[str] = None, **kwargs):
    """Enqueue an RQ job. Prefer keyword args. Optional queue=high|default|bulk."""
    result_ttl = kwargs.pop("result_ttl", max(60, int(settings.WORKER_RESULT_TTL)))
    failure_ttl = kwargs.pop("failure_ttl", max(60, int(settings.WORKER_FAILURE_TTL)))
    job_timeout = kwargs.pop("job_timeout", max(30, int(settings.WORKER_JOB_TIMEOUT)))
    qname = queue
    if qname == "high":
        qname = settings.RQ_HIGH_QUEUE_NAME
    elif qname == "bulk":
        qname = settings.RQ_BULK_QUEUE_NAME
    elif qname in (None, "default"):
        qname = settings.RQ_DEFAULT_QUEUE_NAME or settings.RQ_QUEUE_NAME
    return get_queue(qname).enqueue(
        func,
        *args,
        result_ttl=result_ttl,
        failure_ttl=failure_ttl,
        job_timeout=job_timeout,
        **kwargs,
    )


def close_redis() -> None:
    global _redis, _queues
    if _redis is not None:
        try:
            _redis.close()
        except Exception:
            pass
        try:
            _redis.connection_pool.disconnect()
        except Exception:
            pass
        logger.info("Redis client closed")
    _redis = None
    _queues = {}
