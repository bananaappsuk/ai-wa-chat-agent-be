"""
D6: Report duplicate (user_id, phone) lead groups.

This is a read-only reporting tool — it never deletes or merges leads.
Use it to review duplicates before running a manual cleanup migration and
enabling the unique (user_id, phone) index (see app/db/mongo.py).

Usage:
    python -m scripts.report_duplicate_leads
    python -m scripts.report_duplicate_leads --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging

from app.observability.logging_setup import configure_logging

logger = logging.getLogger(__name__)


async def find_duplicate_groups() -> list[dict]:
    """Return duplicate (user_id, phone) groups with their lead ids, sorted by group size desc."""
    from app.db.mongo import get_db

    db = get_db()
    pipeline = [
        {"$match": {"phone": {"$type": "string"}}},
        {
            "$group": {
                "_id": {"user_id": "$user_id", "phone": "$phone"},
                "count": {"$sum": 1},
                "lead_ids": {"$push": {"$toString": "$_id"}},
                "created_ats": {"$push": "$created_at"},
            }
        },
        {"$match": {"count": {"$gt": 1}}},
        {"$sort": {"count": -1}},
    ]
    groups: list[dict] = []
    async for row in db.leads.aggregate(pipeline):
        groups.append(
            {
                "user_id": row["_id"]["user_id"],
                "phone": row["_id"]["phone"],
                "count": row["count"],
                "lead_ids": row["lead_ids"],
            }
        )
    return groups


async def main(as_json: bool) -> int:
    configure_logging()
    from app.db.mongo import close_client

    groups = await find_duplicate_groups()
    close_client()

    if as_json:
        print(json.dumps({"duplicate_groups": groups, "total_groups": len(groups)}, default=str))
        return 0

    if not groups:
        print("No duplicate (user_id, phone) lead groups found.")
        return 0

    print(f"Found {len(groups)} duplicate (user_id, phone) group(s):\n")
    for g in groups:
        print(f"  user_id={g['user_id']} phone={g['phone']} count={g['count']}")
        print(f"    lead_ids: {', '.join(g['lead_ids'])}")
    print(
        "\nThis tool is read-only. Resolve duplicates manually (merge/delete) "
        "before the unique index can be enabled."
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(args.json))
