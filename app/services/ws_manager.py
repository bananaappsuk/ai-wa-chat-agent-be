import asyncio
import json
from typing import Any
from fastapi import WebSocket


class WSManager:
    def __init__(self) -> None:
        self._conns: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._conns.setdefault(user_id, set()).add(ws)

    async def disconnect(self, user_id: str, ws: WebSocket) -> None:
        async with self._lock:
            conns = self._conns.get(user_id)
            if conns and ws in conns:
                conns.remove(ws)
                if not conns:
                    self._conns.pop(user_id, None)

    async def push(self, user_id: str, event: str, payload: Any) -> None:
        msg = json.dumps({"event": event, "data": payload})
        async with self._lock:
            conns = list(self._conns.get(user_id, ()))
        for ws in conns:
            try:
                await ws.send_text(msg)
            except Exception:
                pass


ws_manager = WSManager()
