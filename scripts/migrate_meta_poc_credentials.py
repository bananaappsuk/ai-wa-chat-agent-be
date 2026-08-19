"""Encrypt env META_ACCESS_TOKEN onto the matching POC tenant. Never run at API startup.

Usage:
  python -m scripts.migrate_meta_poc_credentials --dry-run
  python -m scripts.migrate_meta_poc_credentials --apply
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db.mongo import get_db  # noqa: E402
from app.services.meta_credentials import migrate_legacy_poc_user  # noqa: E402


async def _run(dry_run: bool) -> int:
    env_pnid = (settings.META_PHONE_NUMBER_ID or "").strip()
    if not env_pnid:
        print("No META_PHONE_NUMBER_ID; nothing to migrate.")
        return 1
    db = get_db()
    user = await db.users.find_one({"meta_phone_number_id": env_pnid})
    if not user:
        print("No user with matching meta_phone_number_id.")
        return 1
    # migrate helper uses sync pymongo; copy fields and use motor updates for dry-run display.
    result = {
        "dry_run": dry_run,
        "user_id": str(user["_id"]),
        "eligible": True,
    }
    if dry_run:
        print(f"dry-run: would encrypt env token for user_id={user['_id']} pnid={env_pnid}")
        print("No history/messages would be modified.")
        return 0
    from app.services.meta_credentials import _sync_db, upsert_encrypted_access_token
    from app.models.common import utcnow

    token = (settings.META_ACCESS_TOKEN or "").strip()
    if not token:
        print("META_ACCESS_TOKEN empty; abort.")
        return 1
    upsert_encrypted_access_token(
        user_id=str(user["_id"]),
        access_token=token,
        phone_number_id=env_pnid,
        db=_sync_db(),
    )
    set_doc = {
        "meta_connection_status": "legacy_poc",
        "meta_onboarding_source": "legacy_poc",
        "updated_at": utcnow(),
    }
    waba = (settings.META_WABA_ID or "").strip()
    if waba and not str(user.get("meta_waba_id") or "").strip():
        set_doc["meta_waba_id"] = waba
    if not user.get("meta_connected_at"):
        set_doc["meta_connected_at"] = utcnow()
    await db.users.update_one({"_id": user["_id"]}, {"$set": set_doc})
    print(f"migrated user_id={user['_id']} status=legacy_poc (messages unchanged)")
    print(result)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate env Meta POC token into encrypted tenant credentials")
    parser.add_argument("--apply", action="store_true", help="Write encrypted credentials (default is dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="Show actions only (default)")
    args = parser.parse_args()
    dry = not args.apply
    raise SystemExit(asyncio.run(_run(dry)))


if __name__ == "__main__":
    main()
