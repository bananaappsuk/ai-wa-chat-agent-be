"""Encrypt env META_ACCESS_TOKEN onto the matching original POC tenant.

Never run at API startup. Default is dry-run.

This is only for migrating the original POC tenant whose current
meta_phone_number_id equals META_PHONE_NUMBER_ID. It is not general
multi-tenant production onboarding.

Usage:
  python -m scripts.migrate_meta_poc_credentials --dry-run
  python -m scripts.migrate_meta_poc_credentials --apply

Staging/production refuse unless:
  python -m scripts.migrate_meta_poc_credentials --apply --allow-production-poc-tenant
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


async def _run(*, dry_run: bool, allow_production_poc_tenant: bool) -> int:
    if settings.is_production_like and not allow_production_poc_tenant:
        print(
            "Refusing: APP_ENV is staging/production. This script targets only the "
            "original POC tenant. Pass --allow-production-poc-tenant to override."
        )
        return 2
    if settings.is_production_like and allow_production_poc_tenant:
        print(
            "WARNING: --allow-production-poc-tenant is set. Migrating only the unique "
            "tenant whose meta_phone_number_id matches META_PHONE_NUMBER_ID. "
            "This is not general multi-tenant production onboarding."
        )

    env_pnid = (settings.META_PHONE_NUMBER_ID or "").strip()
    if not env_pnid:
        print("No META_PHONE_NUMBER_ID; nothing to migrate.")
        return 1
    db = get_db()
    users = await db.users.find({"meta_phone_number_id": env_pnid}).to_list(length=5)
    if not users:
        print("No user with matching meta_phone_number_id.")
        return 1
    if len(users) != 1:
        print("Refusing: META_PHONE_NUMBER_ID matches more than one tenant.")
        return 1
    user = users[0]
    from app.services.meta_credentials import _sync_db

    result = migrate_legacy_poc_user(user, dry_run=dry_run, db=_sync_db())
    if dry_run:
        print(
            f"dry-run: would encrypt env token for user_id={user['_id']} pnid={env_pnid} "
            f"eligible={result.get('eligible')}"
        )
        print("No history/messages would be modified. Token is not printed.")
        return 0 if result.get("eligible") else 1
    if not result.get("updated"):
        print(f"migrate aborted eligible={result.get('eligible')} reason={result.get('reason')}")
        return 1
    print(f"migrated user_id={user['_id']} status=legacy_poc (messages unchanged)")
    print({k: v for k, v in result.items() if k != "token"})
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate env Meta POC token into encrypted tenant credentials"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write encrypted credentials (default is dry-run)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show actions only (default)")
    parser.add_argument(
        "--allow-production-poc-tenant",
        action="store_true",
        help=(
            "Required in staging/production. Migrates only the unique tenant whose "
            "PNID matches META_PHONE_NUMBER_ID (original POC tenant)."
        ),
    )
    args = parser.parse_args()
    dry = not args.apply
    raise SystemExit(
        asyncio.run(
            _run(dry_run=dry, allow_production_poc_tenant=args.allow_production_poc_tenant)
        )
    )


if __name__ == "__main__":
    main()
