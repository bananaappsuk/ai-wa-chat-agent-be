from fastapi import APIRouter, HTTPException
from bson import ObjectId

from app.db.mongo import get_db
from app.models.user import UserCreate, UserLogin, TokenOut, UserOut
from app.models.common import utcnow, serialize
from app.middleware.auth import hash_password, verify_password, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_out(doc: dict) -> UserOut:
    s = serialize(doc)
    return UserOut(
        id=s["id"],
        email=s["email"],
        full_name=s.get("full_name") or "",
        company_name=s.get("company_name"),
        phone=s.get("phone"),
        plan=s.get("plan", "free"),
        role=s.get("role", "user"),
        banned=s.get("banned", False),
        created_at=s.get("created_at"),
    )


@router.post("/signup", response_model=TokenOut)
async def signup(payload: UserCreate) -> TokenOut:
    db = get_db()
    if await db.users.find_one({"email": payload.email.lower()}):
        raise HTTPException(status_code=409, detail="Email already registered")
    is_first = (await db.users.estimated_document_count()) == 0
    doc = {
        "email": payload.email.lower(),
        "password_hash": hash_password(payload.password),
        "full_name": payload.full_name.strip(),
        "company_name": (payload.company_name or "").strip() or None,
        "phone": (payload.phone or "").strip() or None,
        "plan": "free",
        "role": "admin" if is_first else "user",
        "banned": False,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    res = await db.users.insert_one(doc)
    doc["_id"] = res.inserted_id
    token = create_access_token(str(res.inserted_id), doc["role"])
    return TokenOut(access_token=token, user=_user_out(doc))


@router.post("/login", response_model=TokenOut)
async def login(payload: UserLogin) -> TokenOut:
    db = get_db()
    user = await db.users.find_one({"email": payload.email.lower()})
    if not user or not verify_password(payload.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user.get("banned"):
        raise HTTPException(status_code=403, detail="Account disabled")
    token = create_access_token(str(user["_id"]), user.get("role", "user"))
    return TokenOut(access_token=token, user=_user_out(user))


@router.get("/me", response_model=UserOut)
async def me(user_id: str | None = None):
    raise HTTPException(status_code=404, detail="Use /api/profile/me")
