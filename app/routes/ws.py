import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from app.config import settings
from app.middleware.auth import ws_user
from app.services.ws_manager import ws_manager
from app.workers.queue import get_redis

router = APIRouter()

_ALLOWED_CLIENT_EVENTS = frozenset({"ping", "pong"})


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket, token: str = Query(default="")):
    user = await ws_user(token)
    if not user:
        await websocket.close(code=4401)
        return
    user_id = str(user["_id"])

    # Connection limit per tenant
    limit = max(1, int(settings.RATE_LIMIT_WS_CONNECTIONS_PER_USER))
    if ws_manager.connection_count(user_id) >= limit:
        await websocket.close(code=4429)
        return

    await ws_manager.connect(user_id, websocket)
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_text(json.dumps({"event": "ping"}))
                except Exception:
                    break
                continue

            if raw is None:
                continue
            if len(raw.encode("utf-8", errors="ignore")) > settings.WS_MAX_MESSAGE_BYTES:
                await websocket.close(code=1009)
                break
            try:
                data = json.loads(raw)
            except Exception:
                # Ignore non-JSON keepalives
                continue
            if not isinstance(data, dict):
                continue
            event = data.get("event")
            if event not in _ALLOWED_CLIENT_EVENTS:
                continue
            # Never accept tenant/user overrides from client payloads
            if event == "ping":
                await websocket.send_text(json.dumps({"event": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(user_id, websocket)


async def redis_pubsub_loop():
    """Bridge worker → API: workers publish to redis, this fans out via WSManager."""
    r = get_redis()
    pubsub = r.pubsub()
    pubsub.subscribe("ws:events")
    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                msg = await loop.run_in_executor(
                    None,
                    lambda: pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)
                continue
            if msg and msg.get("type") == "message":
                try:
                    data = json.loads(msg["data"])
                except Exception:
                    continue
                user_id = data.get("user_id")
                event = data.get("event")
                payload = data.get("data")
                # Only deliver to the authenticated tenant room matching the event user_id
                if user_id and event and isinstance(user_id, str) and ObjectId_is_hex(user_id):
                    await ws_manager.push(user_id, event, payload)
    finally:
        try:
            pubsub.unsubscribe("ws:events")
        except Exception:
            pass
        try:
            pubsub.close()
        except Exception:
            pass


def ObjectId_is_hex(value: str) -> bool:
    from bson import ObjectId

    return ObjectId.is_valid(value)
