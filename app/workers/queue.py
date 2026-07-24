from redis import Redis
from rq import Queue
from app.config import settings

_redis: Redis | None = None
_queue: Queue | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            settings.REDIS_URL,
            health_check_interval=60,
            socket_keepalive=True,
            retry_on_timeout=True,
        )
    return _redis


def get_queue() -> Queue:
    global _queue
    if _queue is None:
        _queue = Queue("default", connection=get_redis(), default_timeout=300)
    return _queue


def enqueue(func, *args, **kwargs):
    return get_queue().enqueue(func, *args, **kwargs)


def close_redis() -> None:
    global _redis, _queue
    if _redis is not None:
        try:
            _redis.close()
        except Exception:
            pass
        try:
            _redis.connection_pool.disconnect()
        except Exception:
            pass
    _redis = None
    _queue = None
