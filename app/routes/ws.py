import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from app.middleware.auth import ws_user
from app.services.ws_manager import ws_manager
from app.workers.queue import get_redis

router = APIRouter()


@router.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket, token: str = Query(default="")):
    user = await ws_user(token)
    if not user:
        await websocket.close(code=4401)
        return
    user_id = str(user["_id"])
    await ws_manager.connect(user_id, websocket)
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_text(json.dumps({"event": "ping"}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(user_id, websocket)


async def redis_pubsub_loop():
    """Bridge worker → API: workers publish to redis, this fans out via WSManager."""
    r = get_redis()
    pubsub = r.pubsub()
    pubsub.subscribe("ws:events")
    loop = asyncio.get_event_loop()
    while True:
        try:
            msg = await loop.run_in_executor(None, lambda: pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0))
            if msg and msg.get("type") == "message":
                try:
                    data = json.loads(msg["data"])
                except Exception:
                    continue
                user_id = data.get("user_id")
                event = data.get("event")
                payload = data.get("data")
                if user_id and event:
                    await ws_manager.push(user_id, event, payload)
            await asyncio.sleep(0)
        except Exception:
            await asyncio.sleep(1)
