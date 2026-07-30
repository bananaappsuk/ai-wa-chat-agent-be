"""Seed an admin and a normal user. Usage:  python -m scripts.seed_users

Creates (or updates) two accounts and prints the login credentials.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db.mongo import get_db, init_indexes  # noqa: E402
from app.middleware.auth import hash_password  # noqa: E402
from app.models.common import utcnow  # noqa: E402


USERS = [
    {
        "email": "admin@aitelechat.com",
        "password": "AdminPass123!",
        "full_name": "Admin User",
        "company_name": "NextGen Techs",
        "phone": "+447453289655",
        "role": "admin",
        "plan": "free",
    },
    {
        "email": "user@aitelechat.com",
        "password": "UserPass123!",
        "full_name": "Test User",
        "company_name": "Acme Ltd",
        "phone": "+447000000000",
        "role": "user",
        "plan": "free",
    },
]


async def main() -> None:
    await init_indexes()
    db = get_db()
    for u in USERS:
        existing = await db.users.find_one({"email": u["email"].lower()})
        doc = {
            "email": u["email"].lower(),
            "password_hash": hash_password(u["password"]),
            "full_name": u["full_name"],
            "company_name": u["company_name"],
            "phone": u["phone"],
            "role": u["role"],
            "plan": u["plan"],
            "banned": False,
            "updated_at": utcnow(),
        }
        if existing:
            # Do not overwrite billing plan on re-seed (Stripe webhooks own paid plans).
            doc.pop("plan", None)
            await db.users.update_one({"_id": existing["_id"]}, {"$set": doc})
            action = "updated"
        else:
            doc["created_at"] = utcnow()
            await db.users.insert_one(doc)
            action = "created"
        print(f"[{action}] {u['role']:5}  email={u['email']}  password={u['password']}")

    print()
    print("Login at:  /login")
    print("Admin:  admin@aitelechat.com  /  AdminPass123!")
    print("User :  user@aitelechat.com   /  UserPass123!")


if __name__ == "__main__":
    asyncio.run(main())
