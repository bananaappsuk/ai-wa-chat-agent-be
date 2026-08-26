"""Test isolation: never touch live Redis (Upstash) from the suite.

Running the suite against the production Redis both makes tests flaky (rate
limits, shared state) and burns the tenant's Upstash command budget. This
autouse fixture points every ``Redis.from_url(...)`` call at an in-memory
fakeredis for the duration of each test, and clears the cached client
singletons so they rebuild against the fake.
"""
import fakeredis
import pytest
import redis


@pytest.fixture(autouse=True)
def isolate_redis(monkeypatch):
    server = fakeredis.FakeServer()

    def _fake_from_url(*_args, **_kwargs):
        return fakeredis.FakeStrictRedis(server=server)

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(_fake_from_url))

    # Reset cached clients so the next access builds a fake one.
    try:
        import app.workers.queue as _q

        monkeypatch.setattr(_q, "_redis", None, raising=False)
        monkeypatch.setattr(_q, "_queues", {}, raising=False)
    except Exception:
        pass
    try:
        import app.workers.tasks as _t

        monkeypatch.setattr(_t, "_redis_client", None, raising=False)
    except Exception:
        pass

    yield
