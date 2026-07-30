#!/usr/bin/env python3
"""
Dry-run capable backfill for new billing fields on users.

Does NOT call Stripe by default. Does not cancel/downgrade subscriptions.

Usage:
  python -m scripts.backfill_billing_fields --dry-run
  python -m scripts.backfill_billing_fields
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from app.db.mongo import get_db, close_client
    from app.models.common import utcnow
    from app.observability.logging_setup import configure_logging

    configure_logging()
    db = get_db()
    updated = 0
    review: list[str] = []

    cursor = db.users.find({})
    async for doc in cursor:
        uid = str(doc["_id"])
        sets: dict = {}
        # Alias trial_end → trial_ends_at
        if doc.get("trial_end") and not doc.get("trial_ends_at"):
            sets["trial_ends_at"] = doc["trial_end"]
        if doc.get("trial_ends_at") and not doc.get("trial_end"):
            sets["trial_end"] = doc["trial_ends_at"]
        if doc.get("stripe_subscription_id") and not doc.get("subscription_status"):
            sets["subscription_status"] = "active"
        if not doc.get("subscription_status"):
            sets["subscription_status"] = "none"
        if doc.get("stripe_customer_id") and doc.get("stripe_subscription_id") and not doc.get("subscription_created_at"):
            # cannot invent; flag for review
            review.append(uid)
        if sets:
            sets["subscription_updated_at"] = utcnow()
            updated += 1
            if not args.dry_run:
                await db.users.update_one({"_id": doc["_id"]}, {"$set": sets})

    print(f"users_needing_field_backfill={updated} dry_run={args.dry_run}")
    print(f"manual_review_missing_subscription_created_at={len(review)}")
    if review[:20]:
        print("sample_review_user_ids:", ", ".join(review[:20]))
    close_client()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
