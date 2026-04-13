"""One-shot smoke test: verifies Mongo, Redis, OpenAI, Twilio creds work."""
import asyncio
import sys
from app.config import settings


async def test_mongo() -> tuple[bool, str]:
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
        info = await client.admin.command("ping")
        db = client[settings.MONGO_DB]
        cols = await db.list_collection_names()
        client.close()
        return True, f"ping={info.get('ok')} db={settings.MONGO_DB} collections={len(cols)}"
    except Exception as exc:
        return False, str(exc)


def test_redis() -> tuple[bool, str]:
    try:
        from redis import Redis
        r = Redis.from_url(settings.REDIS_URL, socket_timeout=5)
        r.ping()
        r.set("smoke", "ok", ex=10)
        val = r.get("smoke")
        return True, f"ping=ok set/get={val.decode() if val else None}"
    except Exception as exc:
        return False, str(exc)


def test_openai() -> tuple[bool, str]:
    try:
        from openai import OpenAI
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[{"role": "user", "content": "Say 'ok' in one word."}],
            max_tokens=5,
        )
        return True, f"model={settings.OPENAI_MODEL} reply={resp.choices[0].message.content!r}"
    except Exception as exc:
        return False, str(exc)


def test_twilio() -> tuple[bool, str]:
    try:
        from twilio.rest import Client
        c = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        acc = c.api.v2010.accounts(settings.TWILIO_ACCOUNT_SID).fetch()
        return True, f"account={acc.friendly_name!r} status={acc.status}"
    except Exception as exc:
        return False, str(exc)


async def main() -> int:
    print("=" * 60)
    print("Smoke testing external services")
    print("=" * 60)
    results: list[tuple[str, bool, str]] = []

    ok, msg = await test_mongo()
    results.append(("MongoDB Atlas", ok, msg))

    ok, msg = test_redis()
    results.append(("Redis", ok, msg))

    ok, msg = test_openai()
    results.append(("OpenAI", ok, msg))

    ok, msg = test_twilio()
    results.append(("Twilio", ok, msg))

    print()
    failures = 0
    for name, ok, msg in results:
        mark = "OK  " if ok else "FAIL"
        print(f"[{mark}] {name}: {msg}")
        if not ok:
            failures += 1
    print()
    if failures:
        print(f"{failures}/{len(results)} services failed")
        return 1
    print("All services reachable.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
