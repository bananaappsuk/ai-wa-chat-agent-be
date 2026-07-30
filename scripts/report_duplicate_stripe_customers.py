#!/usr/bin/env python3
"""Report duplicate Stripe customer / subscription IDs on users. Never prints secrets."""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


async def main() -> int:
    from app.db.mongo import get_db, close_client
    from app.observability.logging_setup import configure_logging

    configure_logging()
    db = get_db()

    def collect(field: str):
        groups: dict[str, list[dict]] = defaultdict(list)
        return field, groups

    _, by_customer = collect("stripe_customer_id")
    _, by_sub = collect("stripe_subscription_id")

    cursor = db.users.find(
        {
            "$or": [
                {"stripe_customer_id": {"$type": "string"}},
                {"stripe_subscription_id": {"$type": "string"}},
            ]
        },
        {
            "_id": 1,
            "email": 1,
            "plan": 1,
            "subscription_status": 1,
            "stripe_customer_id": 1,
            "stripe_subscription_id": 1,
        },
    )
    async for doc in cursor:
        cid = (doc.get("stripe_customer_id") or "").strip()
        sid = (doc.get("stripe_subscription_id") or "").strip()
        summary = {
            "user_id": str(doc["_id"]),
            "email": doc.get("email"),
            "plan": doc.get("plan"),
            "status": doc.get("subscription_status"),
        }
        if cid:
            by_customer[cid].append(summary)
        if sid:
            by_sub[sid].append(summary)

    dup_c = {k: v for k, v in by_customer.items() if len(v) > 1}
    dup_s = {k: v for k, v in by_sub.items() if len(v) > 1}

    print("=== Duplicate stripe_customer_id ===")
    if not dup_c:
        print("(none)")
    for cid, users in dup_c.items():
        print(f"customer={cid[:8]}… count={len(users)}")
        for u in users:
            print(f"  user={u['user_id']} plan={u['plan']} status={u['status']} email={u['email']}")

    print("\n=== Duplicate stripe_subscription_id ===")
    if not dup_s:
        print("(none)")
    for sid, users in dup_s.items():
        print(f"subscription={sid[:8]}… count={len(users)}")
        for u in users:
            print(f"  user={u['user_id']} plan={u['plan']} status={u['status']} email={u['email']}")

    print(f"\nSummary: customer_dups={len(dup_c)} subscription_dups={len(dup_s)}")
    close_client()
    return 1 if (dup_c or dup_s) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
